# BUILD-REPORT: Canonical Work-State Store v3.1

Greenfield implementation of the immutable revision ledger specified in
`brief.md` (sections 0-9), replacing the former JSON queue file. Python
stdlib only, SQLite on disk, WAL mode, `BEGIN IMMEDIATE` transactions.

## What was built

- `work_store/schema.py` -- exact schema-3 DDL (9 canonical tables, 2 policy
  seed tables, append-only triggers, capability-bound insert contracts).
- `work_store/migrate.py` -- checksum-verified schema application.
- `work_store/db.py` -- `sqlite3.set_authorizer`-based deny-by-default
  boundary, the `op_*()`/`actor_context()`/`event_detail_context()` private
  operation-context channel, `db.operation()` (mints a nonce and samples
  the server transaction time `op_now()` AFTER `BEGIN IMMEDIATE`),
  the service-owned owner-capability issuer, `db.checkpoint()`.
- `work_store/capability.py` -- HMAC-signed, transaction-scoped,
  reserve/finalize/release owner capability for `resolve_block` and the
  `owner-decision` completion arm. One issuer per service connection,
  constructed by `db.connect()`; the typed API takes tokens only, never an
  issuer object.
- `work_store/api.py` -- typed operations: `create_item`, `add_dependency`,
  `transition`, `claim`, `append_proof_observation`, `request_block`,
  `resolve_block`, `add_note`, `complete`, `snapshot`.
- `work_store/invariants.py` -- P001-P003, I001-I017, D000 diagnostic
  queries plus the dependency-cycle query.
- `work_store/heartbeat.py` -- two-pass computed-silence predicate
  (`compute_quiet`), exit codes 0/20/30.
- `work_store/legacy.py` -- legacy (schema=2) projection export/import,
  watermark computation, canonical field-by-field comparison.
- `work_store/fixtures.py` -- builds the one rich baseline database (every
  status, transition, completion arm, proof observation, resolution,
  dependency, note, Unicode string, opaque payload object) the downgrade
  fixtures are generated from.
- `work_store/cli.py` -- `prepare-legacy-downgrade`, `legacy-validate`,
  `import-legacy`, `compare-canonical`, `heartbeat`, `fixture-run`, plus an
  internal `generate-fixtures` used to produce the checked-in fixtures.
- `tests/` -- the full falsification suite (below).

## Test results (exact counts, all green)

Interpreter: `/Applications/Xcode.app/Contents/Developer/usr/bin/python3`
(Python 3.9.6). Verbatim output of the exact commands is in
`REMEDIATION-RECEIPT-v3r1.md`; summary:

```
$ python3 -m unittest discover -s tests -p "test_*.py"
Ran 114 tests ... OK
$ python3 -m work_store.cli fixture-run --fixtures tests/fixtures/downgrade
5x [PASS] (expected/actual exit 0, 40, 41, 42, 43); exit 0
```

| File | Count | Acceptance criterion |
|---|---:|---|
| `test_successor_binding.py` | 12 | A -- successor binding (T-SUCC-01..08 + both exception-arm bypasses) |
| `test_block_authority.py` | 11 | B -- block authority (T-BLOCK-01 + capability matrix, real reserve->rollback->release, caller-constructed issuer probes) |
| `test_dispatch_proof.py` | 9 | C -- dispatch proof (T-CLAIM-01, T-CLAIM-02, backdated-claim probe on typed and raw paths, server-clock expiry) |
| `test_append_only_sealer.py` | 9 | D -- append-only sealer matrix (9 canonical tables) |
| `test_invariant_falsification.py` | 26 | E -- computed silence (P001-P003, I001-I017, D000 x2, 2 end-to-end) |
| `test_downgrade_fixtures.py` | 9 | F -- forward-only downgrade (5 fixtures + 4 end-to-end chain tests) |
| `test_v3r1_remediation.py` | 29 | v3.1 probes: E (QUEUED_WORK/ACTIVE_WORK, CLI fields, mandatory P001, one snapshot), F (import trigger set, post-import queued->active, atomic roundtrip.db, canonical JSON/timestamps), A (forged owner-decision import, NULL_SUCCESSOR_OWNERSHIP, I007 scope) |
| `test_happy_path.py` | 6 | smoke / non-adversarial lifecycle |
| `test_heartbeat_smoke.py` | 3 | smoke |
| **Total** | **114** | |

`fixture-run` prints digests and timings on every invocation. They are not
pinned here because `roundtrip.db` is regenerated each run with fresh
timestamps.

### What each previously over-claimed property now rests on

| Property | Status | Executable evidence |
|---|---|---|
| Proof expiry judged against server-controlled time (C) | Implemented | `op_now()` sampled after `BEGIN IMMEDIATE`; triggers require `occurred_at = recorded_at = op_now()` and `valid_until >= op_now()`; no API takes a caller time. `DefectOneProbeTests` (5) |
| Computed silence requires zero counts + zero diagnostics, P001 always on, one snapshot (E) | Implemented | `HeartbeatSilenceTests` (7), `HeartbeatCliTests` (4) |
| Owner capability rollback/replay (B) | Implemented, in-process | `test_capability_released_on_rollback_is_usable_again` (spy: reserve=1, release>0, finalize=0, nothing committed, token reusable), `test_capability_single_use_after_commit` |
| Service-owned issuer (B) | Implemented, in-process (see STAYS PROSE) | `test_caller_constructed_issuer_token_rejected_by_*`, `test_api_takes_no_issuer_parameter` |
| Rebuild preserves enforcement (F) | Implemented | `ImportTriggerPreservationTests` (2): full trigger set equals a fresh schema-3 artifact, P001 fingerprint matches, queued->active without proof rejected on typed and raw paths, event triggers record again |
| Non-finite rejection, key NFC, timestamp canonicalization (F) | Implemented | `CanonicalJsonTests` (5) |
| Durable rename of `roundtrip.db` (F) | Implemented | `AtomicRoundtripWriteTests` (3) |
| Forged owner-decision completion diagnosed (A) | Implemented for a missing or mismatched capability record; a fully fabricated record STAYS PROSE (below) | `ForgedOwnerDecisionImportTests` (4), `ForgedOwnerDecisionLedgerExtensionTests` (1) |

## Design deviations, with justification

1. **Two mechanical limits added to `revision_insert_contract`** beyond the
   literal schema template, so the `final-phase` and `owner-decision`
   exception arms are *actually* bounded rather than merely documented:
   `exception-not-actually-final-phase` (refuses `final-phase` when a
   required phase gate exists) and `owner-decision-capability-required`
   (refuses `owner-decision` unless `op_kind()='complete'` and
   `op_decision_ref()` matches `NEW.exception_decision_ref`, mirroring the
   existing `resolve_block` binding). Without these, acceptance criterion A
   ("exception arms mechanically limited") would be prose, not an
   enforced/tested invariant.
2. **I004 query extended to three sub-cases**: (a) malformed XOR, (b)
   final-phase retroactively invalid, and (c) (v3.1) an `owner-decision`
   completion without a matching capability-consumption record. All three
   route through the single stable `I004` diagnostic ID, preserving the
   one-to-one matrix. For (c), `api.complete` writes an
   `item_events` row in the same transaction that consumes the capability:
   `event_type='verdict'`, `detail='owner-capability-consumed'`, metadata
   `{capability, decision_ref, operation_nonce, token_sha256}`. I004(c)
   requires that row with the revision's nonce and decision ref.
3. **Canonical JSON is a scoped subset of RFC 8785**, not the full spec:
   key sorting, NFC normalization of keys and values (keys colliding after
   normalization and non-string keys are rejected), compact separators, no
   BOM, final LF, and rejection of non-finite numbers (`allow_nan=False`)
   are implemented (`work_store/util.py`). Caller-supplied timestamps
   (`valid_until`, `decision_date`) are canonicalized at the API boundary
   by `canonical_timestamp` (RFC 3339 with an explicit offset → UTC,
   6-digit microseconds, `Z`); naive timestamps are rejected. RFC 8785's exact numeric-literal serialization
   algorithm (shortest round-trippable form per ECMA-262) is not
   separately implemented since this project never serializes floats (all
   values are strings, integers, or booleans) -- the distinction is
   unobservable here but is not a general-purpose JCS implementation.
4. **`PRAGMA ignore_check_constraints=ON`** is used in
   `tests.helpers.corrupted_copy` to construct three invariant-violation
   fixtures (I004a, I007, I009) that are *also*, redundantly, blocked by a
   row-local `CHECK` constraint on the `STRICT` table. This is
   intentional defense in depth (the CHECK is a second, independent layer
   below the trigger/invariant-query layer for those three invariants),
   not a gap -- documented inline where used.
5. **Dependency fidelity at the legacy top level**: `brief.md` 6.1's shown
   example renders `"dependencies":[]` with no further detail. Since the
   comparison semantics explicitly require "every dependency tuple and
   nonce" and `_canonical_v3`'s shown sub-keys do not include a
   dependencies array, this implementation carries the full tuple
   (`item_id`, `depends_on_id`, `created_at`, `created_actor`,
   `operation_nonce`) at the top-level `dependencies` field rather than a
   bare ID list, so round-trip fidelity holds without inventing an
   undocumented `_canonical_v3` sub-key. `notes` and `history` are
   similarly my own reasonable rendering (operator-note events, and a
   revision-by-revision status timeline) beyond what the one-line example
   shows -- full fidelity for both lives in `_canonical_v3.events` and
   `_canonical_v3.revisions` regardless, so these two top-level fields are
   legacy-reader convenience, not the fidelity-bearing representation.
6. **(v3.1) Server-minted time is enforced at the trigger layer**, not only
   in `api.py`. `revision_insert_contract` and `proof_insert_contract` gain
   final clauses: `occurred-at-not-server-minted` and
   `recorded-at-not-server-minted`, which require `NEW.occurred_at` /
   `NEW.recorded_at` to equal `op_now()`. The claim clause compares
   `valid_until >= op_now()` instead of `NEW.occurred_at`. This changes the
   schema template text, so the migration checksum and the P001 reference
   fingerprint differ from 3c104dc. No checksum or fingerprint is pinned in
   tests; P001 is now computed from the compiled schema
   (`invariants.reference_fingerprint`). The typed API no longer accepts
   `occurred_at`/`observed_at` parameters. A `claim` proof dict containing
   `occurred_at`/`recorded_at`/`observed_at` is rejected with
   `caller-supplied-time-forbidden`.
7. **(v3.1) I007 / I008 scoping.** I007 now flags a successor revision >1
   only if it predates (or shares the operation nonce of) the binding
   completion; later legitimate advancement such as `api.claim` of the child
   no longer trips it. I008 treats NULL in any of
   `successor_owner_item_id/revision/nonce` or the completing revision's
   `operation_nonce` as a violation (unknown is not satisfied).
8. **(v3.1) Heartbeat semantics.** `quiet` requires zero diagnostics, both
   passes equal, and every numeric predicate zero. Both passes run in one
   deferred read transaction. Exit 20 now covers outstanding work as well
   as diagnostics; the CLI prints
   `not quiet: active=.. eligible_queued=.. blocked=.. pollable_external=..`.
   Consequently the end-to-end chain test's heartbeat on `roundtrip.db` (a
   baseline with queued work) asserts exit 20 with no diagnostic lines; at
   3c104dc it asserted exit 0 "all quiet", which was the false silence.
9. **(v3.1) `legacy.import_legacy`** still suspends the six
   insert-contract/event triggers for the literal offline replay, but now
   recreates them from their exact stored SQL before commit. It then
   verifies that the full trigger set equals the fresh schema's, and raises
   `ImportIntegrityError` otherwise. `import_legacy_to_file` and
   `prepare-legacy-downgrade` write via `legacy.atomic_backup` (temp file,
   fsync, `os.replace`, directory fsync).

## Known limitations / FUTURE / STAYS PROSE (acceptance criterion G)

The following are **not** claimed as GRADUATE; they are either out of
scope for this sandbox or not independently, adversarially tested here:

- **Multi-process OS-level atomic cutover** (`brief.md` 6.3): the exclusive
  *service lease* held by a second process, the *read-only consumer config*
  rename as a file distinct from the projection/watermark, directory
  fsync of that second rename, and the legacy-writer unit's unconditional
  `ExecCondition` schema-3 failure are **STAYS PROSE**. This sandbox is a
  single process with no second "legacy service" to hold a lease against
  or redirect. What **is** implemented and tested
  (`cmd_prepare_legacy_downgrade` in `work_store/cli.py`): opening
  `BEGIN IMMEDIATE` on the source connection, reading the watermark,
  doing all export/validate/round-trip/compare work inside that single
  transaction, rereading and comparing the watermark's canonical SHA-256
  **immediately before** the atomic rename, rolling back and exiting 43 on
  any mismatch, and only committing after the projection/watermark/db
  files are durably renamed with `os.replace` plus directory `fsync`. This
  is the core atomic-check arithmetic the design specifies; the
  surrounding multi-process orchestration is not.
- **Content-addressed artifact lifecycle** (`brief.md` 6.2): verifying that
  a release manifest's recorded `sha256` matches a referenced artifact
  file is implemented and tested (`test_tampered_release_manifest_is_a_tool_integrity_failure`).
  Actually copying the artifact into `releases/sha256/<sha256>/work-store.pyz`,
  preserving the currently-running binary before every upgrade, and
  running `self-test --schema 3` against an SQLite backup as a gating
  step are **STAYS PROSE** -- there is no second, separately-versioned
  compatible binary artifact in this single-build sandbox to exercise
  that lifecycle against.
- **`legacy-validate`'s executable-identity report** is implemented as a
  stderr line (path, SHA-256 of `cli.py`, a literal version string); there
  is no separate `--version` subcommand flag wired to argparse. Scoped
  simplification, not tested as its own contract.
- **Owner-decision forgery: partially detectable offline.** I004(c)
  diagnoses an `owner-decision` completion that has no matching
  capability-consumption record or a mismatched one (tested, including the
  reviewer's ledger-extension probe through `legacy.import_legacy`). It does
  **not** detect a forger who also fabricates a consistent `verdict` event
  with any 64-hex `token_sha256`. The HMAC secret is per-connection and
  ephemeral, so the record cannot be cryptographically verified after the
  fact. Cryptographically verifiable issuance (a persistent or asymmetric
  key) is **STAYS PROSE**.
- **The owner channel is in-process.** The issuer is constructed and owned
  by the service connection (`db.connect`). The typed API verifies only
  against it, and tokens from any caller-constructed
  `OwnerCapabilityIssuer` fail verification (tested). Issuance happens
  through `db.owner_capability_issuer(conn)`, an in-process function. No
  separate authenticated owner socket or process boundary exists, so code
  running inside the service process can call it. That boundary is
  **STAYS PROSE**. Reservation state is likewise in-process memory; a
  process restart regenerates the secret, which invalidates outstanding
  tokens rather than replaying them.
- **The service clock is the host wall clock** (`db._server_now`). Server
  time is minted inside the transaction and never taken from a caller
  (tested), but nothing in this build defends against the host clock itself
  stepping backwards. **STAYS PROSE**.
- **Noted, not changed (outside the verdict's quoted findings):** I005 and
  I010 contain the same `<>`-against-possibly-NULL comparison shape that
  was fixed in I008; the weak-update probes noted under criterion D
  (nonexistent columns) are likewise noted only.
- **Tenant/document scoping:** this codebase contains no tenant or
  document scoping logic; v3.1 neither adds nor alters any.
- **`legacy_unmapped` is always `{}`**: this build has no real predecessor
  legacy v2 store to migrate *from*, so the field exists for schema
  compliance and round-trip fidelity (it participates in every hash/diff
  the same as any other field) but its intended purpose -- carrying
  opaque fields from an actual legacy reader/writer this store doesn't
  understand -- is unexercised. **STAYS PROSE** until a real legacy
  source exists.
- **`generation` is hard-coded to `1`** on every `export_legacy` call; a
  real deployment would persist and increment a generation counter across
  repeated downgrade attempts. Not implemented.

## Scope boundary reminder

Per `brief.md` section 9, this remains a local immutable-ledger store, not
a scheduler, release controller, network authority, or capability issuer
of record. No row in this report claims deployed/operational behavior;
"GRADUATE" above means executably enforced and adversarially tested in
this repository, not reviewed-and-deployed.
