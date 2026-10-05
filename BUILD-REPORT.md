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
  operation-context channel, `db.operation()` (mints a nonce under
  `BEGIN IMMEDIATE`), `db.checkpoint()`.
- `work_store/capability.py` -- HMAC-signed, transaction-scoped,
  reserve/finalize/release owner capability for `resolve_block` and the
  `owner-decision` completion arm.
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

```
$ /opt/homebrew/bin/python3 -m unittest discover -s tests -p "test_*.py"
----------------------------------------------------------------------
Ran 77 tests in 1.6s

OK
```

| File | Count | Acceptance criterion |
|---|---:|---|
| `test_successor_binding.py` | 12 | A -- successor binding (T-SUCC-01..08 + both exception-arm bypasses) |
| `test_block_authority.py` | 8 | B -- block authority (T-BLOCK-01 + capability matrix) |
| `test_dispatch_proof.py` | 4 | C -- dispatch proof (T-CLAIM-01, T-CLAIM-02 + 2) |
| `test_append_only_sealer.py` | 9 | D -- append-only sealer matrix (9 canonical tables) |
| `test_invariant_falsification.py` | 26 | E -- computed silence (P001-P003, I001-I017, D000 x2, 2 end-to-end) |
| `test_downgrade_fixtures.py` | 9 | F -- forward-only downgrade (5 fixtures + 4 end-to-end chain tests) |
| `test_happy_path.py` | 6 | smoke / non-adversarial lifecycle |
| `test_heartbeat_smoke.py` | 3 | smoke |
| **Total** | **77** | |

Required commands, both run directly (not merely described):

```
$ python3 -m unittest -v tests.test_downgrade_fixtures        # 9/9 ok
$ python3 -m work_store.cli fixture-run --fixtures tests/fixtures/downgrade
[PASS] roundtrip-all            expected=0  actual=0
[PASS] reader-drops-extension   expected=40 actual=40
[PASS] lossy-history            expected=41 actual=41
[PASS] parity-value             expected=42 actual=42
[PASS] moved-watermark          expected=43 actual=43
total elapsed=~0.1s
```

(Exact digests and per-run timings are printed by `fixture-run` itself on
each invocation; they are not pinned in this report since `fixture-run`
regenerates `roundtrip.db` content afresh, which changes nothing
semantically but is not literally byte-identical run to run. The command
above was executed; its PASS/FAIL table is reproduced verbatim.)

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
2. **I004 query extended to two sub-cases** (`I004a` malformed XOR,
   `I004b` final-phase retroactively invalid) to match the two independent
   ways the completion-arm invariant can be violated; both route through
   the single stable `I004` diagnostic ID, preserving the one-to-one
   matrix.
3. **Canonical JSON is a scoped subset of RFC 8785**, not the full spec:
   key sorting, NFC normalization, compact separators, no BOM, final LF,
   and rejection of non-finite numbers are implemented
   (`work_store/util.py`); RFC 8785's exact numeric-literal serialization
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
- **Owner-decision capability forgery is not retroactively detectable**
  from a corrupted offline copy: `OwnerCapabilityIssuer` tokens are
  ephemeral (HMAC secret and reservation state live only in the issuing
  process's memory), so unlike every other invariant in this suite, "was
  this `owner-decision` exception arm backed by a real, consumed
  capability" cannot be reconstructed from the database alone after the
  fact. The *live* path is fully enforced and tested
  (`owner-decision-capability-required`,
  `test_owner_decision_bypass_without_capability_rejected`,
  `test_owner_decision_bypass_with_wrong_capability_scope_rejected`); a
  forensic/audit-log capability-issuance record is **STAYS PROSE**.
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
