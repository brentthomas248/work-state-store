# BUILD: Canonical Work-State Store v3.1 (immutable revision ledger)

## What this is

You are building the system's own canonical work-state store — the durable fix
for silent stalls. It replaces a JSON queue file with a SQLite-backed immutable
revision ledger (Python standard library only). This is greenfield: there is no
existing code. The full design follows verbatim between `--- DESIGN BEGINS ---`
and `--- DESIGN ENDS ---`.

## Environment

- Workdir: /Users/brentthomas1/claude-workers/work-state-store-v3-build
- FORBIDDEN: `git clone`, `git fetch`, or any network GitHub read. This is a
  greenfield build; there is nothing to fetch.
- `git init` the workdir locally. Remote: https://github.com/brentthomas248/work-state-store.git
  (empty repo, created for this build). Push your branch when done (see below).
- Python: use /opt/homebrew/bin/python3. No Docker needed. SQLite via stdlib.
- Before any BLOCKED verdict, run the exact verification commands and quote
  verbatim diagnostics. Do not report BLOCKED without a real attempt.

## Deliverable

1. The complete v3.1 store implementation: schema, triggers, typed API
   (create_item, transition, claim, append_proof_observation, request_block,
   resolve_block, add_note, snapshot, invariant_violations, export_legacy,
   prepare_legacy_downgrade), migration tooling, heartbeat query module.
2. The FULL falsification test suite from the design, IMPLEMENTED AND EXECUTED.
   Every test must run; report pass/fail counts. A test that is described but
   not executed is a failure of this build.
3. A BUILD-REPORT.md at the repo root: what was built, test results (exact
   counts), any design deviations with justification, and known limitations.

## Acceptance criteria (from independent review — ALL must hold)

The design went through 4 review rounds. These reviewer findings are your
acceptance bar; the review of your implementation will check each one:

A. **Successor binding (was the blocking defect):** a required gate can be
   completed ONLY with a distinct successor revision that is queued, created
   atomically in the same transaction as the completion, and non-reusable.
   Exception arms (`final-phase`, `owner-decision`) are mechanically limited:
   `final-phase` only for actually-final phases; `owner-decision` only via a
   consumed owner capability. No syntactically-valid exception may bypass a
   required gate without the invariant set diagnosing it. Negative tests:
   cancelled / done / active / revisionless / pre-existing / shared
   successors; both exception-arm bypasses.
B. **Block authority:** capability consumption is transaction-scoped and
   unforgeable, bound to item + prior revision + exact decision reference +
   operation, with rollback/replay semantics. A caller knowing the stored
   reference must NOT be able to forge resolution. Test the generic
   transition path.
C. **Dispatch proof:** queued -> active ONLY via the claim capability; the
   insert contract validates the unique current tip (identity, live, time
   validity) against a server-minted transaction `now` (never a
   caller-supplied timestamp). Negative tests: revoked-tip and expired-tip
   transitions through every typed operation.
D. **Append-only everywhere:** every canonical table whose mutation can
   change state/eligibility/policy/audit history denies UPDATE/DELETE
   (triggers or tested authorizer policy). Dependency changes are
   append-only.
E. **Computed silence:** the silence evaluator demonstrably returns
   quiet=false for every counterexample above, end to end. Every diagnostic
   has a unique stable ID; the test matrix is one-to-one.
F. **Rollback:** forward-only downgrade procedure is executable: concrete
   schemas, canonical serialization, exact commands with exit semantics,
   atomic watermark check, runnable round-trip/refusal fixtures — EXECUTED,
   with captured exit codes/digests in the build report.
G. **Graduation honesty:** any property not executably enforced and
   negatively tested is labeled FUTURE/STAYS PROSE, not GRADUATE.

## Done criteria

- All acceptance criteria A–G hold; full test suite green (quote counts).
- `git init`, commit, push branch `feature/work-state-store/v3` to the repo,
  open a PR. Report the PR URL and the exact test summary in your final
  message.
- Do NOT merge the PR. Merge is a separate pipeline step.

## The design (verbatim)

--- DESIGN BEGINS ---
# Canonical Work-State Store v3.1 — Bounded Remediation

**Status:** reviewable design; no operational graduation is claimed  
**Date:** 2026-10-05  
**Scope:** SQLite/Python-stdlib work-state store  
**Relationship to v3:** this document preserves the immutable revision ledger,
append-CAS, sealed terminal states, capability boundary, and forward-only downgrade.
It changes only the findings in rethink review REJECT #1.

## 0. Decision record and bounded experiment

The rejected hypothesis was that immutable item/proof rows plus phase validation were
sufficient to make accepted work non-silent. The demonstrated counterexample was a
`design-accepted` completion pointing to a previously cancelled build. The smallest
decisive experiment for this remediation is `T-SUCC-02`: create and cancel a correctly
phased build, then try to complete a design with it. Acceptance evidence is a
synchronous `successor-not-fresh-queued` abort, an unchanged revision/event watermark,
and a non-quiet heartbeat if the same shape is injected with migration authority.
The attempt allowance is one implementation attempt and 90 minutes. Any terminal,
active, revisionless, pre-existing, or shared successor that commits stops the attempt.

The improvement lane considered a separate successor table, mutable dependency
checksums, and transaction-scoped SQL functions. A successor table is rejected as an
unnecessary second source of truth. Mutable checksum rows are rejected because they
reintroduce ordering. The adopted bounded change puts immutable ownership on successor
revision 1, emits an immutable dependency event, and makes private operation context
visible to triggers. Benefit: the admitted sequences fail at insertion while the
ledger model remains unchanged. Rollback is the existing forward-only model.

## 1. Trust and operation boundary

Only the `work-store` OS user can open the database or its parent directory. Database,
WAL, and SHM are mode `0600`; the socket directory is not caller-writable; peer
credentials are checked. Clients receive typed RPC, never SQL:

```text
create_item(item, actor)
add_dependency(item_id, depends_on_id, actor)
transition(item_id, expected_revision, new_status, evidence, actor)
claim(item_id, expected_revision, proof, actor)
append_proof_observation(item_id, previous_proof_id, observation, actor)
request_block(item_id, expected_revision, decision_ref, text, actor)
resolve_block(item_id, expected_revision, exact_ref, resolution, owner_capability)
complete(item_id, expected_revision, completion, actor)
add_note(item_id, expected_revision, note, actor)
snapshot(); invariant_violations(now); prepare_legacy_downgrade()
```

Every mutation is `BEGIN IMMEDIATE` and installs a server-created `OperationContext`
in connection-private Python memory. It contains `{kind,item_id,prior_revision,
decision_ref,successor_id,nonce,actor}`. `nonce` is 32 random bytes rendered as 64
lowercase hex characters and is never accepted from request JSON. Registered SQL
functions `op_kind()`, `op_item()`, `op_prior()`, `op_decision_ref()`,
`op_successor()`, `op_nonce()`, `actor_context()`, and `event_detail_context()` read
that object. When no context exists they return NULL. A context is installed only
after request validation and, for resolution, successful verification of the named
owner capability. It is cleared in a `finally` block after `COMMIT` or `ROLLBACK`.
Nested contexts are refused. A connection is discarded if cleanup cannot be proven.

The SQLite authorizer denies all direct table INSERT/UPDATE/DELETE unless the table
and action are on the fixed allowlist for the current operation. It always denies
client SQL, ATTACH, writable pragmas, trigger/view creation, and DDL. `transition`
may insert one revision and its trigger event, but cannot write resolution columns,
create active state, or complete a gated phase. `claim` may insert one proof root,
one active revision, and generated events. `resolve_block` may insert exactly one
revision with resolution fields. `complete` may insert a successor identity, its
owned revision 1, and the predecessor done revision. Migration uses a separate
offline executable, absent from the service image, under an exclusive file lock.

The nonce is evidence that the trigger observed the private operation context; it is
not reusable authority. `operation_nonce` is unique in the ledger. Rollback removes
all rows bearing it and clears memory. After commit, replay fails both CAS and nonce
uniqueness. Reusing an owner capability is independently rejected by the owner
capability verifier before SQL. Knowing a decision reference or an old nonce cannot
create the matching live connection context.

## 2. Exact schema 3 migration

Timestamps are UTC RFC 3339 with six fractional digits and `Z`. JSON is canonical
UTF-8: objects have lexicographically sorted keys, separators are `,` and `:`, strings
use JSON escapes, numbers are finite base-10 JSON numbers, and no insignificant
whitespace is emitted. The following is the complete schema script. Seed statements
run where shown, then all sealers are installed. `schema_version` is inserted last.

```sql
PRAGMA foreign_keys=ON;

CREATE TABLE schema_version(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL CHECK(version=3),
 applied_at TEXT NOT NULL, migration_name TEXT NOT NULL
) STRICT;
CREATE TABLE schema_migrations(
 version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
 checksum TEXT NOT NULL, applied_at TEXT NOT NULL, actor TEXT NOT NULL
) STRICT;
CREATE TABLE status_transitions(
 from_status TEXT NOT NULL, to_status TEXT NOT NULL,
 requires_block_resolution INTEGER NOT NULL CHECK(requires_block_resolution IN(0,1)),
 PRIMARY KEY(from_status,to_status)
) STRICT;
CREATE TABLE phase_gates(
 predecessor_phase TEXT PRIMARY KEY, successor_phase TEXT NOT NULL,
 required INTEGER NOT NULL CHECK(required IN(0,1)), description TEXT NOT NULL,
 CHECK(predecessor_phase<>successor_phase)
) STRICT;

CREATE TABLE work_items(
 id TEXT PRIMARY KEY, title TEXT NOT NULL CHECK(length(trim(title))>0),
 phase TEXT NOT NULL,
 lane TEXT NOT NULL CHECK(lane IN
  ('codex-cloud','claude-code','claude-mac','orchestrator','operator')),
 env TEXT NOT NULL, brief_ref TEXT,
 created_at TEXT NOT NULL, created_actor TEXT NOT NULL CHECK(length(trim(created_actor))>0)
) STRICT;

CREATE TABLE item_revisions(
 item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL CHECK(revision>=1),
 prior_revision INTEGER, status TEXT NOT NULL CHECK(status IN
  ('queued','active','blocked','done','superseded','cancelled')),
 payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload) AND json_type(payload)='object'),
 occurred_at TEXT NOT NULL, actor TEXT NOT NULL CHECK(length(trim(actor))>0), detail TEXT,
 operation_nonce TEXT CHECK(operation_nonce IS NULL OR
  (length(operation_nonce)=64 AND operation_nonce=lower(operation_nonce))),

 dispatch_root_id INTEGER REFERENCES dispatch_proofs(proof_id)
  DEFERRABLE INITIALLY DEFERRED,
 block_decision_ref TEXT, block_decision_text TEXT, block_requested_on TEXT,
 resolved_decision_ref TEXT, resolved_on TEXT, resolution_text TEXT,
 resolution_actor TEXT,

 completion_successor_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
 completion_successor_phase TEXT,
 exception_kind TEXT CHECK(exception_kind IN('final-phase','owner-decision')),
 exception_reason TEXT, exception_decision_date TEXT, exception_decision_ref TEXT,

 /* Present only on a successor's revision 1. The deferred back-reference makes
    successor revision 1 and predecessor done revision one atomic unit. */
 successor_owner_item_id TEXT,
 successor_owner_revision INTEGER,
 successor_owner_nonce TEXT,

 PRIMARY KEY(item_id,revision),
 UNIQUE(item_id,prior_revision),
 UNIQUE(successor_owner_item_id,successor_owner_revision),
 FOREIGN KEY(successor_owner_item_id,successor_owner_revision)
  REFERENCES item_revisions(item_id,revision) DEFERRABLE INITIALLY DEFERRED,
 CHECK((revision=1 AND prior_revision IS NULL) OR
       (revision>1 AND prior_revision=revision-1)),
 CHECK(status='active' OR dispatch_root_id IS NULL),
 CHECK(status='blocked' OR
  (block_decision_ref IS NULL AND block_decision_text IS NULL AND block_requested_on IS NULL)),
 CHECK((status='blocked' AND length(trim(block_decision_ref))>0
       AND length(trim(block_decision_text))>0 AND block_requested_on IS NOT NULL) OR
      (status<>'blocked' AND block_decision_ref IS NULL)),
 CHECK((resolved_decision_ref IS NULL AND resolved_on IS NULL AND
        resolution_text IS NULL AND resolution_actor IS NULL) OR
       (length(trim(resolved_decision_ref))>0 AND resolved_on IS NOT NULL AND
        length(trim(resolution_text))>0 AND length(trim(resolution_actor))>0)),
 CHECK(status='done' OR
  (completion_successor_id IS NULL AND completion_successor_phase IS NULL AND
   exception_kind IS NULL AND exception_reason IS NULL AND
   exception_decision_date IS NULL AND exception_decision_ref IS NULL)),
 CHECK(status<>'done' OR
  ((completion_successor_id IS NOT NULL AND completion_successor_phase IS NOT NULL AND
    exception_kind IS NULL AND exception_reason IS NULL AND
    exception_decision_date IS NULL AND exception_decision_ref IS NULL)
   OR
   (completion_successor_id IS NULL AND completion_successor_phase IS NULL AND
    exception_kind IS NOT NULL AND length(trim(exception_reason))>0 AND
    ((exception_kind='final-phase' AND exception_decision_date IS NULL AND
      exception_decision_ref IS NULL) OR
     (exception_kind='owner-decision' AND exception_decision_date IS NOT NULL AND
      length(trim(exception_decision_ref))>0))))),
 CHECK((successor_owner_item_id IS NULL AND successor_owner_revision IS NULL AND
        successor_owner_nonce IS NULL) OR
       (revision=1 AND status='queued' AND successor_owner_item_id IS NOT NULL AND
        successor_owner_revision IS NOT NULL AND length(successor_owner_nonce)=64))
) STRICT;

CREATE UNIQUE INDEX completion_successor_once
 ON item_revisions(completion_successor_id)
 WHERE completion_successor_id IS NOT NULL;

CREATE TABLE dispatch_proofs(
 proof_id INTEGER PRIMARY KEY,
 item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
 previous_proof_id INTEGER REFERENCES dispatch_proofs(proof_id) ON DELETE RESTRICT,
 root_proof_id INTEGER NOT NULL REFERENCES dispatch_proofs(proof_id)
  DEFERRABLE INITIALLY DEFERRED,
 proof_kind TEXT NOT NULL CHECK(proof_kind IN('worker-pid','codex-task-url')),
 proof_value TEXT NOT NULL CHECK(length(trim(proof_value))>0), host_id TEXT,
 issued_actor TEXT NOT NULL CHECK(length(trim(issued_actor))>0),
 recorded_at TEXT NOT NULL, observed_at TEXT NOT NULL, valid_until TEXT NOT NULL,
 observation TEXT NOT NULL CHECK(observation IN('live','dead','unknown','revoked')),
 observer_actor TEXT NOT NULL CHECK(length(trim(observer_actor))>0),
 operation_nonce TEXT UNIQUE,
 UNIQUE(previous_proof_id),
 CHECK(recorded_at<=observed_at AND observed_at<=valid_until),
 CHECK((proof_kind='worker-pid' AND host_id IS NOT NULL AND length(trim(host_id))>0)
       OR proof_kind='codex-task-url')
) STRICT;

CREATE TABLE work_item_dependencies(
 item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
 depends_on_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL, created_actor TEXT NOT NULL,
 operation_nonce TEXT NOT NULL UNIQUE,
 PRIMARY KEY(item_id,depends_on_id), CHECK(item_id<>depends_on_id)
) STRICT;

CREATE TABLE item_events(
 event_id INTEGER PRIMARY KEY,
 item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
 revision INTEGER, occurred_at TEXT NOT NULL,
 actor TEXT NOT NULL CHECK(length(trim(actor))>0),
 event_type TEXT NOT NULL CHECK(event_type IN
 ('created','state-change','dispatch-proof','dependency-added','verdict',
  'operator-note','legacy-history','migration')),
 detail TEXT, metadata TEXT NOT NULL DEFAULT '{}'
  CHECK(json_valid(metadata) AND json_type(metadata)='object'),
 UNIQUE(item_id,revision,event_type)
) STRICT;

CREATE INDEX revisions_current ON item_revisions(item_id,revision DESC);
CREATE INDEX revisions_status ON item_revisions(status,item_id,revision DESC);
CREATE INDEX proof_item ON dispatch_proofs(item_id,proof_id);
CREATE INDEX proof_previous ON dispatch_proofs(previous_proof_id);
CREATE INDEX dependency_reverse ON work_item_dependencies(depends_on_id,item_id);
CREATE INDEX events_item_time ON item_events(item_id,event_id);

CREATE VIEW current_items AS
SELECT w.id,w.title,w.phase,w.lane,w.env,w.brief_ref,w.created_at,
 r.revision,r.status,r.payload,r.occurred_at,r.actor,r.dispatch_root_id,
 r.block_decision_ref,r.block_decision_text,r.block_requested_on
FROM work_items w JOIN item_revisions r ON r.item_id=w.id
WHERE r.revision=(SELECT max(x.revision) FROM item_revisions x WHERE x.item_id=w.id);

INSERT INTO status_transitions VALUES
 ('queued','active',0),('queued','blocked',0),('active','done',0),
 ('active','blocked',0),('blocked','queued',1),('queued','superseded',0),
 ('queued','cancelled',0),('active','superseded',0),('active','cancelled',0),
 ('blocked','superseded',1),('blocked','cancelled',1);
INSERT INTO phase_gates VALUES
 ('design-accepted','implementation-build',1,'Accepted design must produce build work'),
 ('build-complete','independent-review',1,'Completed build requires non-author review'),
 ('review-accepted','merge-or-PR',1,'Accepted review produces integration work');

/* Every canonical row is permanently immutable. Policy also forbids post-seed insert. */
CREATE TRIGGER schema_version_no_insert BEFORE INSERT ON schema_version
 WHEN EXISTS(SELECT 1 FROM schema_version)
 BEGIN SELECT RAISE(ABORT,'immutable-schema-version'); END;
CREATE TRIGGER schema_version_no_update BEFORE UPDATE ON schema_version
 BEGIN SELECT RAISE(ABORT,'immutable-schema-version'); END;
CREATE TRIGGER schema_version_no_delete BEFORE DELETE ON schema_version
 BEGIN SELECT RAISE(ABORT,'immutable-schema-version'); END;
CREATE TRIGGER schema_migrations_no_update BEFORE UPDATE ON schema_migrations
 BEGIN SELECT RAISE(ABORT,'append-only-schema-migration'); END;
CREATE TRIGGER schema_migrations_no_delete BEFORE DELETE ON schema_migrations
 BEGIN SELECT RAISE(ABORT,'append-only-schema-migration'); END;
CREATE TRIGGER status_transitions_no_insert BEFORE INSERT ON status_transitions
 BEGIN SELECT RAISE(ABORT,'immutable-status-policy'); END;
CREATE TRIGGER status_transitions_no_update BEFORE UPDATE ON status_transitions
 BEGIN SELECT RAISE(ABORT,'immutable-status-policy'); END;
CREATE TRIGGER status_transitions_no_delete BEFORE DELETE ON status_transitions
 BEGIN SELECT RAISE(ABORT,'immutable-status-policy'); END;
CREATE TRIGGER phase_gates_no_insert BEFORE INSERT ON phase_gates
 BEGIN SELECT RAISE(ABORT,'immutable-phase-policy'); END;
CREATE TRIGGER phase_gates_no_update BEFORE UPDATE ON phase_gates
 BEGIN SELECT RAISE(ABORT,'immutable-phase-policy'); END;
CREATE TRIGGER phase_gates_no_delete BEFORE DELETE ON phase_gates
 BEGIN SELECT RAISE(ABORT,'immutable-phase-policy'); END;
CREATE TRIGGER work_items_no_update BEFORE UPDATE ON work_items
 BEGIN SELECT RAISE(ABORT,'append-only-item-identity'); END;
CREATE TRIGGER work_items_no_delete BEFORE DELETE ON work_items
 BEGIN SELECT RAISE(ABORT,'append-only-item-identity'); END;
CREATE TRIGGER revisions_no_update BEFORE UPDATE ON item_revisions
 BEGIN SELECT RAISE(ABORT,'append-only-item-revision'); END;
CREATE TRIGGER revisions_no_delete BEFORE DELETE ON item_revisions
 BEGIN SELECT RAISE(ABORT,'append-only-item-revision'); END;
CREATE TRIGGER proofs_no_update BEFORE UPDATE ON dispatch_proofs
 BEGIN SELECT RAISE(ABORT,'append-only-dispatch-proof'); END;
CREATE TRIGGER proofs_no_delete BEFORE DELETE ON dispatch_proofs
 BEGIN SELECT RAISE(ABORT,'append-only-dispatch-proof'); END;
CREATE TRIGGER dependencies_no_update BEFORE UPDATE ON work_item_dependencies
 BEGIN SELECT RAISE(ABORT,'append-only-dependency'); END;
CREATE TRIGGER dependencies_no_delete BEFORE DELETE ON work_item_dependencies
 BEGIN SELECT RAISE(ABORT,'append-only-dependency'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON item_events
 BEGIN SELECT RAISE(ABORT,'append-only-event-log'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON item_events
 BEGIN SELECT RAISE(ABORT,'append-only-event-log'); END;

CREATE TRIGGER dependency_insert_contract BEFORE INSERT ON work_item_dependencies BEGIN
 SELECT CASE WHEN op_kind()<>'add_dependency' OR op_item()<>NEW.item_id OR
  op_nonce()<>NEW.operation_nonce THEN RAISE(ABORT,'dependency-capability-required') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM current_items
  WHERE id=NEW.item_id AND revision=1 AND status='queued') OR NOT EXISTS(
  SELECT 1 FROM current_items WHERE id=NEW.depends_on_id AND revision=1 AND status='queued')
  THEN RAISE(ABORT,'dependency-window-closed') END;
END;
CREATE TRIGGER dependency_insert_event AFTER INSERT ON work_item_dependencies BEGIN
 INSERT INTO item_events(item_id,occurred_at,actor,event_type,detail,metadata)
 VALUES(NEW.item_id,NEW.created_at,actor_context(),'dependency-added',
  event_detail_context(),json_object('depends_on_id',NEW.depends_on_id,
  'operation_nonce',NEW.operation_nonce));
END;

CREATE TRIGGER revision_insert_contract BEFORE INSERT ON item_revisions BEGIN
 SELECT CASE WHEN op_nonce() IS NULL OR NEW.operation_nonce<>op_nonce() OR
  (NEW.item_id<>op_item() AND NOT (op_kind()='complete' AND
   NEW.item_id=op_successor()))
  THEN RAISE(ABORT,'revision-capability-required') END;
 SELECT CASE WHEN NEW.revision=1 AND NEW.status<>'queued'
  THEN RAISE(ABORT,'new-item-must-start-queued') END;
 SELECT CASE WHEN NEW.revision=1 AND EXISTS(
  SELECT 1 FROM item_revisions WHERE item_id=NEW.item_id)
  THEN RAISE(ABORT,'duplicate-initial-revision') END;
 SELECT CASE WHEN NEW.revision>1 AND (NEW.prior_revision<>op_prior() OR NOT EXISTS(
  SELECT 1 FROM item_revisions p WHERE p.item_id=NEW.item_id
   AND p.revision=NEW.prior_revision AND p.revision=(SELECT max(x.revision)
    FROM item_revisions x WHERE x.item_id=NEW.item_id)))
  THEN RAISE(ABORT,'stale-version') END;
 SELECT CASE WHEN NEW.revision>1 AND NOT EXISTS(
  SELECT 1 FROM item_revisions p JOIN status_transitions t
   ON t.from_status=p.status AND t.to_status=NEW.status
  WHERE p.item_id=NEW.item_id AND p.revision=NEW.prior_revision)
  THEN RAISE(ABORT,'illegal-status-transition') END;
 SELECT CASE WHEN NEW.revision>1 AND NEW.occurred_at<=(SELECT occurred_at
  FROM item_revisions WHERE item_id=NEW.item_id AND revision=NEW.prior_revision)
  THEN RAISE(ABORT,'occurred-at-not-advanced') END;

 /* queued -> active is exclusively claim. The referenced root must also be the
    unique current tip, live, same-item, and time-valid in this transaction. */
 SELECT CASE WHEN NEW.status='active' AND (op_kind()<>'claim' OR NOT EXISTS(
  SELECT 1 FROM dispatch_proofs p WHERE p.proof_id=NEW.dispatch_root_id
   AND p.item_id=NEW.item_id AND p.proof_id=p.root_proof_id
   AND p.previous_proof_id IS NULL AND p.observation='live'
   AND p.valid_until>=NEW.occurred_at AND p.operation_nonce=op_nonce()
   AND NOT EXISTS(SELECT 1 FROM dispatch_proofs n
    WHERE n.previous_proof_id=p.proof_id)
   AND 1=(SELECT count(*) FROM dispatch_proofs tip
    WHERE tip.root_proof_id=p.root_proof_id AND NOT EXISTS(
     SELECT 1 FROM dispatch_proofs child WHERE child.previous_proof_id=tip.proof_id))))
  THEN RAISE(ABORT,'claim-current-live-tip-required') END;

 SELECT CASE WHEN NEW.revision>1 AND EXISTS(
  SELECT 1 FROM item_revisions p JOIN status_transitions t
   ON t.from_status=p.status AND t.to_status=NEW.status
  WHERE p.item_id=NEW.item_id AND p.revision=NEW.prior_revision
   AND t.requires_block_resolution=1) AND
  (op_kind()<>'resolve_block' OR op_decision_ref() IS NULL OR
   NEW.resolved_decision_ref<>op_decision_ref() OR
   NEW.resolved_decision_ref<>(SELECT p.block_decision_ref FROM item_revisions p
    WHERE p.item_id=NEW.item_id AND p.revision=NEW.prior_revision))
  THEN RAISE(ABORT,'block-resolution-capability-required') END;
 SELECT CASE WHEN op_kind()<>'resolve_block' AND
  (NEW.resolved_decision_ref IS NOT NULL OR NEW.resolved_on IS NOT NULL OR
   NEW.resolution_text IS NOT NULL OR NEW.resolution_actor IS NOT NULL)
  THEN RAISE(ABORT,'resolution-fields-forbidden') END;

 SELECT CASE WHEN NEW.status='done' AND EXISTS(
  SELECT 1 FROM phase_gates g JOIN work_items w ON w.id=NEW.item_id
  WHERE g.predecessor_phase=w.phase AND g.required=1) AND
  NEW.completion_successor_id IS NULL AND NEW.exception_kind IS NULL
  THEN RAISE(ABORT,'missing-required-successor') END;
 SELECT CASE WHEN NEW.status='done' AND NEW.completion_successor_id IS NOT NULL AND
  (op_kind()<>'complete' OR op_successor()<>NEW.completion_successor_id OR NOT EXISTS(
   SELECT 1 FROM item_revisions c JOIN work_items cw ON cw.id=c.item_id
   JOIN work_items pw ON pw.id=NEW.item_id JOIN phase_gates g
    ON g.predecessor_phase=pw.phase AND g.required=1
   WHERE c.item_id=NEW.completion_successor_id AND c.revision=1
    AND c.status='queued' AND c.prior_revision IS NULL
    AND c.successor_owner_item_id=NEW.item_id
    AND c.successor_owner_revision=NEW.revision
    AND c.successor_owner_nonce=op_nonce()
    AND c.operation_nonce=op_nonce() AND cw.phase=g.successor_phase
    AND NEW.completion_successor_phase=cw.phase
    AND NOT EXISTS(SELECT 1 FROM item_revisions c2
     WHERE c2.item_id=c.item_id AND c2.revision>1)))
  THEN RAISE(ABORT,'successor-not-fresh-queued') END;
 SELECT CASE WHEN NEW.successor_owner_item_id IS NOT NULL AND
  (op_kind()<>'complete' OR NEW.successor_owner_item_id=NEW.item_id OR
   op_successor()<>NEW.item_id OR NEW.successor_owner_nonce<>op_nonce())
  THEN RAISE(ABORT,'invalid-successor-ownership') END;
END;

CREATE TRIGGER revision_insert_event AFTER INSERT ON item_revisions BEGIN
 INSERT INTO item_events(item_id,revision,occurred_at,actor,event_type,detail)
 VALUES(NEW.item_id,NEW.revision,NEW.occurred_at,actor_context(),
  CASE WHEN NEW.revision=1 THEN 'created' ELSE 'state-change' END,
  event_detail_context());
END;

CREATE TRIGGER proof_insert_contract BEFORE INSERT ON dispatch_proofs BEGIN
 SELECT CASE WHEN op_kind() NOT IN('claim','append_proof_observation') OR
  op_item()<>NEW.item_id OR op_nonce()<>NEW.operation_nonce
  THEN RAISE(ABORT,'proof-capability-required') END;
 SELECT CASE WHEN NEW.previous_proof_id IS NULL AND NEW.root_proof_id<>NEW.proof_id
  THEN RAISE(ABORT,'invalid-proof-root') END;
 SELECT CASE WHEN NEW.previous_proof_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM dispatch_proofs p WHERE p.proof_id=NEW.previous_proof_id
   AND p.item_id=NEW.item_id AND p.root_proof_id=NEW.root_proof_id
   AND p.proof_kind=NEW.proof_kind AND p.proof_value=NEW.proof_value
   AND p.host_id IS NEW.host_id AND p.issued_actor=NEW.issued_actor
   AND NOT EXISTS(SELECT 1 FROM dispatch_proofs n WHERE n.previous_proof_id=p.proof_id))
  THEN RAISE(ABORT,'proof-chain-or-identity-mismatch') END;
 SELECT CASE WHEN NEW.previous_proof_id IS NULL AND EXISTS(
  SELECT 1 FROM current_items c WHERE c.id=NEW.item_id AND c.status='active')
  THEN RAISE(ABORT,'active-item-proof-root-substitution') END;
END;
CREATE TRIGGER proof_insert_event AFTER INSERT ON dispatch_proofs BEGIN
 INSERT INTO item_events(item_id,occurred_at,actor,event_type,detail,metadata)
 VALUES(NEW.item_id,NEW.observed_at,actor_context(),'dispatch-proof',
  event_detail_context(),json_object('proof_id',NEW.proof_id,
  'root_proof_id',NEW.root_proof_id,'observation',NEW.observation));
END;

INSERT INTO schema_migrations VALUES
 (3,'immutable-ledger-v3.1',:migration_sha256,:applied_at,:migration_actor);
INSERT INTO schema_version VALUES
 (1,3,:applied_at,'immutable-ledger-v3.1');
```

The migration runner binds the final three values; `:migration_sha256` is SHA-256 of
the UTF-8 SQL template with LF newlines and those three bind tokens unchanged.
`schema_migrations` remains append-only so a future exclusive migration may append a
row, but cannot alter history. All other listed canonical tables deny both UPDATE and
DELETE in DDL. Dependencies are permanently sealed after insertion; changes are new
work-item revisions/items, never edge mutation. The generated `dependency-added`
event is an immutable second commitment to the exact edge and nonce.

### Atomic typed operations

`create_item` uses kind `create_item`; the context item is the new ID, prior is NULL,
and identity plus revision 1 commit together. `add_dependency` is admitted only while
both tips remain queued revision 1. `claim` creates one fresh live root and then the
active revision with the same nonce. It cannot reuse an older root.

For gated `complete(P,N)`, the service selects a fresh successor UUID itself, installs
context `{complete,P,N,successor,nonce}`, inserts successor `work_items`, inserts its
queued revision 1 with owner `(P,N+1,nonce)`, then inserts predecessor revision N+1
pointing to it. The circular deferred FK is checked at commit. The partial unique
index prevents any successor identity from serving two completions. No caller may
nominate a pre-existing successor. Ungated completion uses the same operation without
a successor, or the already specified exception arm. Distinctness is enforced by the
ownership CHECK and trigger.

`resolve_block` verifies the external owner capability before opening the transaction,
binds its exact decision reference to private context, rereads the blocked tip, and
inserts the exit. Generic `transition` authorizer and trigger both reject any
resolution field and all queued-to-active attempts. CAS remains append-only
`revision=expected+1, prior_revision=expected`; conflict is never retried.

## 3. Stable invariant inventory

All checks run in one read transaction. Prepare, bind, step, decode, or commit failure
is `D000 invariant-query-failed`, suppresses silence, alerts, and exits nonzero. IDs
below are permanent API values; labels may be elaborated but IDs never move.

| ID | Diagnostic |
|---|---|
| P001 | schema-artifact-drift |
| P002 | status-transition-policy-drift |
| P003 | phase-gate-policy-drift |
| I001 | revision-gap-or-fork |
| I002 | item-without-revision |
| I003 | illegal-ledger-transition |
| I004 | done-with-invalid-completion-evidence |
| I005 | successor-phase-mismatch |
| I006 | successor-revision-missing |
| I007 | successor-not-queued-revision-one |
| I008 | successor-ownership-mismatch-or-reused |
| I009 | blocked-without-owner-decision |
| I010 | block-exit-without-capability-evidence |
| I011 | malformed-proof-chain |
| I012 | proof-identity-substitution |
| I013 | malformed-proof-time-or-identity |
| I014 | active-without-current-live-proof-tip |
| I015 | state-event-mismatch |
| I016 | dependency-commitment-mismatch |
| I017 | dependency-cycle |
| D000 | invariant-query-failed |

P001 hashes `type,name,tbl_name,sql` for every non-`sqlite_%` schema artifact ordered
by type/name, with CRLF changed to LF, trailing whitespace stripped per line, fields
joined by NUL, and SHA-256 over UTF-8. P002 and P003 use compiled `VALUES` plus EXCEPT
in both directions for all seeded tuples. The executable state query is:

```sql
WITH latest AS (
 SELECT r.* FROM item_revisions r WHERE r.revision=(SELECT max(x.revision)
  FROM item_revisions x WHERE x.item_id=r.item_id)),
tip AS (
 SELECT p.* FROM dispatch_proofs p WHERE NOT EXISTS
  (SELECT 1 FROM dispatch_proofs n WHERE n.previous_proof_id=p.proof_id))
SELECT 'I001',r.item_id,CAST(r.revision AS TEXT) FROM item_revisions r
WHERE (r.revision=1 AND r.prior_revision IS NOT NULL) OR
 (r.revision>1 AND NOT EXISTS(SELECT 1 FROM item_revisions p
  WHERE p.item_id=r.item_id AND p.revision=r.revision-1)) OR EXISTS(
  SELECT 1 FROM item_revisions n WHERE n.item_id=r.item_id
  AND n.prior_revision=r.revision GROUP BY n.prior_revision HAVING count(*)<>1)
UNION ALL
SELECT 'I002',w.id,'no revision' FROM work_items w WHERE NOT EXISTS
 (SELECT 1 FROM item_revisions r WHERE r.item_id=w.id)
UNION ALL
SELECT 'I003',n.item_id,CAST(n.revision AS TEXT) FROM item_revisions n
JOIN item_revisions p ON p.item_id=n.item_id AND p.revision=n.prior_revision
LEFT JOIN status_transitions t ON t.from_status=p.status AND t.to_status=n.status
WHERE t.from_status IS NULL
UNION ALL
SELECT 'I004',r.item_id,CAST(r.revision AS TEXT) FROM item_revisions r
WHERE r.status='done' AND NOT ((r.completion_successor_id IS NOT NULL AND
 r.exception_kind IS NULL) OR (r.completion_successor_id IS NULL AND
 r.exception_kind IS NOT NULL))
UNION ALL
SELECT 'I005',r.item_id,coalesce(r.completion_successor_id,'NULL')
FROM item_revisions r JOIN work_items p ON p.id=r.item_id
JOIN phase_gates g ON g.predecessor_phase=p.phase AND g.required=1
LEFT JOIN work_items c ON c.id=r.completion_successor_id
WHERE r.status='done' AND r.completion_successor_id IS NOT NULL AND
 (c.id IS NULL OR c.phase<>g.successor_phase OR r.completion_successor_phase<>c.phase)
UNION ALL
SELECT 'I006',r.item_id,r.completion_successor_id FROM item_revisions r
WHERE r.status='done' AND r.completion_successor_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM item_revisions c WHERE c.item_id=r.completion_successor_id
  AND c.revision=1)
UNION ALL
SELECT 'I007',r.item_id,r.completion_successor_id FROM item_revisions r
JOIN item_revisions c ON c.item_id=r.completion_successor_id AND c.revision=1
WHERE r.status='done' AND (c.status<>'queued' OR c.prior_revision IS NOT NULL OR
 EXISTS(SELECT 1 FROM item_revisions c2 WHERE c2.item_id=c.item_id AND c2.revision>1))
UNION ALL
SELECT 'I008',r.item_id,r.completion_successor_id FROM item_revisions r
JOIN item_revisions c ON c.item_id=r.completion_successor_id AND c.revision=1
WHERE r.status='done' AND (c.successor_owner_item_id<>r.item_id OR
 c.successor_owner_revision<>r.revision OR
 c.successor_owner_nonce<>r.operation_nonce OR EXISTS(
  SELECT 1 FROM item_revisions other WHERE other.completion_successor_id=c.item_id
   AND (other.item_id<>r.item_id OR other.revision<>r.revision)))
UNION ALL
SELECT 'I009',item_id,CAST(revision AS TEXT) FROM latest WHERE status='blocked'
 AND (block_decision_ref IS NULL OR block_decision_text IS NULL)
UNION ALL
SELECT 'I010',n.item_id,CAST(n.revision AS TEXT) FROM item_revisions n
JOIN item_revisions p ON p.item_id=n.item_id AND p.revision=n.prior_revision
JOIN status_transitions t ON t.from_status=p.status AND t.to_status=n.status
WHERE t.requires_block_resolution=1 AND (n.resolved_decision_ref IS NULL OR
 n.resolved_decision_ref<>p.block_decision_ref OR n.operation_nonce IS NULL)
UNION ALL
SELECT 'I011',p.item_id,CAST(p.proof_id AS TEXT) FROM dispatch_proofs p
LEFT JOIN dispatch_proofs q ON q.proof_id=p.previous_proof_id
WHERE (p.previous_proof_id IS NULL AND p.root_proof_id<>p.proof_id) OR
 (p.previous_proof_id IS NOT NULL AND (q.proof_id IS NULL OR
 q.item_id<>p.item_id OR q.root_proof_id<>p.root_proof_id))
UNION ALL
SELECT 'I012',p.item_id,CAST(p.proof_id AS TEXT) FROM dispatch_proofs p
JOIN dispatch_proofs q ON q.proof_id=p.previous_proof_id
WHERE p.proof_kind<>q.proof_kind OR p.proof_value<>q.proof_value OR
 p.host_id IS NOT q.host_id OR p.issued_actor<>q.issued_actor
UNION ALL
SELECT 'I013',p.item_id,CAST(p.proof_id AS TEXT) FROM dispatch_proofs p
WHERE p.recorded_at>p.observed_at OR p.observed_at>p.valid_until OR
 length(trim(p.proof_value))=0 OR length(trim(p.issued_actor))=0 OR
 (p.proof_kind='worker-pid' AND (p.host_id IS NULL OR length(trim(p.host_id))=0))
UNION ALL
SELECT 'I014',l.item_id,coalesce(CAST(l.dispatch_root_id AS TEXT),'NULL')
FROM latest l LEFT JOIN tip p ON p.root_proof_id=l.dispatch_root_id
WHERE l.status='active' AND (p.proof_id IS NULL OR p.item_id<>l.item_id OR
 p.observation<>'live' OR p.valid_until<:now OR EXISTS(
  SELECT 1 FROM tip p2 WHERE p2.root_proof_id=l.dispatch_root_id
  AND p2.proof_id<>p.proof_id))
UNION ALL
SELECT 'I015',r.item_id,CAST(r.revision AS TEXT) FROM item_revisions r
LEFT JOIN item_events e ON e.item_id=r.item_id AND e.revision=r.revision
 AND e.event_type IN('created','state-change') WHERE e.event_id IS NULL
UNION ALL
SELECT 'I016',d.item_id,d.depends_on_id FROM work_item_dependencies d
LEFT JOIN item_events e ON e.item_id=d.item_id AND e.event_type='dependency-added'
 AND json_extract(e.metadata,'$.depends_on_id')=d.depends_on_id
 AND json_extract(e.metadata,'$.operation_nonce')=d.operation_nonce
WHERE e.event_id IS NULL
UNION ALL
SELECT 'I016',e.item_id,json_extract(e.metadata,'$.depends_on_id')
FROM item_events e LEFT JOIN work_item_dependencies d ON d.item_id=e.item_id
 AND d.depends_on_id=json_extract(e.metadata,'$.depends_on_id')
 AND d.operation_nonce=json_extract(e.metadata,'$.operation_nonce')
WHERE e.event_type='dependency-added' AND d.item_id IS NULL;
```

Cycles are a separate, stable diagnostic:

```sql
WITH RECURSIVE reach(origin,node,path) AS (
 SELECT item_id,depends_on_id,','||item_id||','||depends_on_id||','
 FROM work_item_dependencies
 UNION ALL
 SELECT r.origin,d.depends_on_id,r.path||d.depends_on_id||',' FROM reach r
 JOIN work_item_dependencies d ON d.item_id=r.node
 WHERE instr(r.path,','||d.depends_on_id||',')=0)
SELECT 'I017',d.item_id,'cycle' FROM work_item_dependencies d
JOIN reach r ON r.origin=d.depends_on_id AND r.node=d.item_id GROUP BY d.item_id;
```

`PRAGMA integrity_check`, `foreign_key_check`, P001–P003, and I001–I017 are mandatory.
P001 proves sealing artifacts have their compiled identity; I016 separately proves
edge contents still equal their immutable event commitments, including deletion.

## 4. Eligibility, silence, and heartbeat

Eligibility remains computed:

```sql
SELECT c.id,c.revision,c.lane,c.env,c.brief_ref,c.payload FROM current_items c
WHERE c.status='queued' AND NOT EXISTS(
 SELECT 1 FROM work_item_dependencies d JOIN current_items p ON p.id=d.depends_on_id
 WHERE d.item_id=c.id AND p.status<>'done') ORDER BY c.created_at,c.id;
```

The final predicate is the prior four-field query (`active`, eligible queued, blocked,
pollable external). It is evaluated only after every diagnostic is empty, integrity
and foreign keys pass, projection succeeds, and then all work is repeated in a second
snapshot. Any diagnostic or error makes `quiet=false`. Consequently an injected
cancelled successor yields I007, a revisionless successor yields I006, reuse/ownership
yields I008, and dependency deletion yields I016. Each independently suppresses quiet.

The replacement heartbeat body is:

1. Connect through the peer-checked socket. Verify schema 3, foreign keys, WAL,
   `synchronous=FULL`, busy timeout, migration digest, authorizer self-test, and P001.
2. In a snapshot enumerate active items and the unique tip for each recorded root.
   Verify process identity or exact task URL. Append live/dead/unknown/revoked without
   changing identity; timeout, PID reuse, mismatch, and uncertainty are not live.
3. Run P001–P003, I001–I017, integrity and foreign-key checks. Alert each stable ID.
   Query failure is D000 and a nonzero tick.
4. Reconcile terminal workers by typed CAS. Gated completion calls only the atomic
   `complete` operation, which creates the owned queued successor in that transaction.
5. Reverify blocked decision references. Only `resolve_block` receives an in-memory
   context minted after external owner-capability verification.
6. Run eligibility in stable order. Lane preflight precedes `claim`; claim makes a
   fresh root and active revision atomically. Stale CAS is reread, never overwritten.
7. Export the read-only projection from the same or later snapshot and atomically
   replace it only after parity. Projection failure cannot enable a legacy writer.
8. Repeat diagnostics and predicates. Emit `all quiet` only when both passes are clean
   and every numeric predicate is zero.

The cancelled-successor end-to-end fixture first demonstrates that the public API and
service-authorized SQL both abort before commit. Its corruption half disables triggers
only in an offline copy, inserts the reviewed counterexample, and runs `heartbeat
--once`; expected result is exit 20, `quiet=false`, and I007. Thus both prevention and
fail-closed detection cover the original incident.

## 5. Migration contract

The importer freezes legacy writers, locks and hashes every input, and records counts
and the legacy generation. Identity fields map to `work_items`; current status/payload
become an ordered synthetic chain; history and notes become events; dependencies map
one-for-one and receive generated dependency commitment events; active tracks require
a fresh live proof; unknown fields live under `payload.legacy_unmapped`. Phase mapping
is the exact table `{design:design-accepted, build:implementation-build,
review:independent-review, integration:merge-or-PR}`; already canonical phase strings
pass unchanged. Unknown enums, duplicate IDs, orphan edges/tracks, ambiguous paths,
stale active proofs, and gated done records without exactly one completion arm abort.

The input legacy schema is the concrete JSON object defined in §6. No null/missing
normalization is implicit: absent optional values become null; strings are NFC Unicode;
timestamps parse then emit canonical UTC; arrays preserve order except dependencies,
which sort by `(item_id,depends_on_id)`; maps sort keys; integer values remain integers.
Booleans never coerce to integers. Import occurs in one sibling temporary DB
transaction, writes migration rows/version last, and runs the complete diagnostics.
Counts, every normalized field, every edge/event/proof, and all unmapped JSON compare
against the frozen input. Independent review precedes atomic installation. Services
start read-only first; mutation endpoints start only after the watermark is unchanged.

## 6. Executable forward-only downgrade

### 6.1 Concrete interchange schemas

The legacy projection is canonical JSON with this total shape:

```json
{
  "schema": 2,
  "generation": 41,
  "source": "sqlite-v3",
  "source_watermark": {"schema_version":3,"max_event_id":9,"max_proof_id":4,"items":{"A":3}},
  "items": [{
    "id":"A","title":"title","phase":"design-accepted","lane":"operator",
    "env":"local","brief_ref":null,"status":"done","payload":{},
    "created_at":"2026-10-05T00:00:00.000000Z","created_actor":"owner",
    "dependencies":[],"notes":[],"history":[],
    "_canonical_v3": {
      "encoding":"work-state-v3-extension/1",
      "identity_sha256":"64 lowercase hex",
      "revisions":[],"completion":null,"proofs":[],"resolution":null,
      "events":[],"legacy_unmapped":{}
    }
  }]
}
```

Every revision object contains every `item_revisions` column using JSON null for SQL
NULL. Completion is either null, `{"arm":"successor","id":...,"phase":...}` or
`{"arm":"exception","kind":...,"reason":...,"date":...,"ref":...}`. Every proof
contains every proof column. Every event contains every event column. Resolution
contains the four resolution fields or null. `identity_sha256` hashes canonical JSON
of identity fields `{id,title,phase,lane,env,brief_ref,created_at,created_actor}`.
This makes the mapping total rather than relying on a reader's inferred fields.

Canonical serialization is RFC 8785-style JSON Canonicalization Scheme with the
additional restrictions already stated: NFC strings, finite numbers only, UTF-8, no
BOM, and a final LF. Hashes are lowercase SHA-256 of those exact bytes. Comparison
loads both sides, rejects duplicate JSON keys, normalizes timestamps/NFC, removes only
top-level `generated_at`, canonicalizes, and compares hashes plus the following
field-by-field groups: identity; tip status/payload; every revision column; completion
arm; every dependency tuple and nonce; every proof column; every event column;
resolution fields; counts by table/event/status; and watermark.

### 6.2 Commands, identity, and exit semantics

The preserved compatible binary is not a vague earlier release. At installation the
release manifest records:

```json
{"protocol":3,"schema_min":3,"schema_max":3,
 "artifact":"work-store.pyz","sha256":"<measured lowercase SHA-256>",
 "self_test":"schema3-read-write","python_abi":"cp312"}
```

Downgrade may start only the exact content-addressed artifact copied to
`releases/sha256/<sha256>/work-store.pyz`; it recomputes the digest, requires protocol
3/schema range containing 3, and runs `self-test --schema 3` against an SQLite backup.
The currently running artifact qualifies and is preserved before every upgrade, so no
unknown historical binary is assumed.

Exact CLI:

```text
python3 -m work_store.cli prepare-legacy-downgrade --db PATH --out STAGE --legacy-reader PATH --release-manifest PATH
python3 -m work_store.cli legacy-validate --input STAGE/projection.json --preserve-unknown byte-exact
python3 -m work_store.cli import-legacy --input STAGE/projection.json --db STAGE/roundtrip.db
python3 -m work_store.cli compare-canonical --left-db PATH --right-db STAGE/roundtrip.db --watermark STAGE/watermark.json
python3 -m work_store.cli heartbeat --db STAGE/roundtrip.db --once --no-external
```

Exit 0 means staged, parity-proven, and consumer switch complete. Exit 40 is
`REFUSED_UNREPRESENTABLE` (reader does not advertise and demonstrate byte-exact unknown
object preservation or an enum cannot encode). Exit 41 is `REFUSED_LOSSY` (round-trip
omits a row/field/count). Exit 42 is `REFUSED_PARITY` (present but unequal normalized
value/hash). Exit 43 is `REFUSED_MOVED_WATERMARK`. Exit 44 is tool/integrity failure.
On every nonzero result config remains `sqlite-v3` and no legacy writer starts.

`legacy-validate` is validation-only: it parses the exact legacy schema, emits its
normalized canonical bytes to stdout, performs no rename/write/config action, and
exits 0 only if `_canonical_v3` is returned byte-for-byte by an encode/decode cycle.
Its executable identity (path, SHA-256, `--version` output) is included in the report.

### 6.3 Atomic watermark check and switch

Under the exclusive service lease, mutation endpoints stop, WAL checkpoints, and the
store executes `BEGIN IMMEDIATE`. W is read as schema version, max event/proof IDs,
and sorted per-item max revisions and written canonically. Export, legacy validation,
round-trip import, parity, and heartbeat occur while that transaction and lease block
writers (readers may use their snapshot). Immediately before `os.replace` and config
switch, on the **same connection and still inside that transaction**, the process
rereads the four components and compares their canonical SHA-256 with W. Inequality
rolls back and exits 43. Equality permits atomic projection rename and read-only
consumer config rename; directories are fsynced. The transaction commits only after
both durable renames. A compatible canonical store is started and must reread exactly
W before the lease is released. The legacy-writer unit has an unconditional schema-3
`ExecCondition` failure; no token overrides it.

### 6.4 Runnable fixtures required before cutover

Checked-in implementation fixtures are specified exactly: `roundtrip-all.json`
contains every status, transition, completion arm, proof observation, resolution,
dependency, note/history event, null optional, Unicode string, and unknown object;
`reader-drops-extension.json` uses a stub reader that deletes `_canonical_v3` (exit
40); `lossy-history.json` drops one event (41); `parity-value.json` changes lane (42);
`moved-watermark.json` appends a note between initial and final check (43). Commands:

```text
python3 -m unittest -v tests.test_downgrade_fixtures
python3 -m work_store.cli fixture-run --fixtures tests/fixtures/downgrade
```

The first implementation acceptance report must include stdout, exit codes, artifact
digests, and elapsed time. This design does **not** claim those not-yet-authored
fixtures have run; forward-only downgrade remains a design contract until they do.

## 7. One-to-one falsification matrix

Every test uses exact migration SQL in a temporary directory, public typed API plus a
service-authorized SQL path where relevant, and asserts exception/message, rollback,
unchanged revision/proof/event counts, and quiet suppression. Corruption tests use an
offline copy and never weaken production.

| Diagnostic/control | Unique test | Required result |
|---|---|---|
| P001 | T-P001 artifact byte change | P001 |
| P002 | T-P002 transition tuple change | P002 only |
| P003 | T-P003 gate tuple change | P003 only |
| I001 | T-I001 revision gap | I001 only |
| I002 | T-I002 revisionless item | I002 only |
| I003 | T-I003 illegal edge | I003 only |
| I004 | T-I004 malformed completion XOR | I004 only |
| I005 | T-I005 wrong child phase | I005 only |
| I006 | T-I006 revisionless successor | insert rejected; corrupted copy emits I006 |
| I007 | T-I007 cancelled successor | insert rejected; corrupted copy emits I007 |
| I008 | T-I008 shared/owner mismatch | insert/unique rejected; copy emits I008 |
| I009 | T-I009 malformed block | CHECK rejected; copy emits I009 |
| I010 | T-I010 forged reference exit | generic transition rejected; copy emits I010 |
| I011 | T-I011 broken proof chain | rejected; copy emits I011 |
| I012 | T-I012 proof identity change | rejected; copy emits I012 |
| I013 | T-I013 invalid proof time | CHECK rejected; copy emits I013 |
| I014 | T-I014 revoked/expired tip | transition rejected; active copy emits I014 |
| I015 | T-I015 missing generated event | copy emits I015 |
| I016 | T-I016 dependency row/event deletion | UPDATE/DELETE rejected; copy emits I016 |
| I017 | T-I017 dependency cycle | I017 only |
| D000 | T-D000 prepare/step/decode failure | nonzero, never quiet |

Additional successor tests are stable IDs T-SUCC-01 terminal, T-SUCC-02 cancelled,
T-SUCC-03 active, T-SUCC-04 done, T-SUCC-05 revisionless, T-SUCC-06 pre-existing
queued, and T-SUCC-07 shared identity. Each attempts the exact final `complete` API and
authorized insert, expects `successor-not-fresh-queued` (or uniqueness/FK rejection),
and verifies no predecessor done row/event. T-SUCC-08 is the clean atomic child and
must commit with no diagnostics. The complete API never accepts a caller successor ID,
so pre-existing identities are also rejected at decoding.

T-CLAIM-01 appends revoked tip then attempts queued-to-active via `transition`,
`claim` with the revoked root, `request_block`, `resolve_block`, `complete`, and raw
authorized revision insert. None can create active; exact result is
`claim-current-live-tip-required` or operation-shape rejection. T-CLAIM-02 repeats
with an expired tip. A legitimate `claim` creates a new root in its own atomic call
and succeeds. T-BLOCK-01 reads the exact decision reference and supplies it through
generic transition; authorizer/trigger returns `resolution-fields-forbidden` or
`block-resolution-capability-required`. Wrong capability, actor, reference, replay,
and nested context fail; one verified resolution commits once.

For **every canonical table** the matrix performs UPDATE and DELETE of an existing
row and checks its exact sealer: schema version, schema migrations, status policy,
phase policy, items, revisions, proofs, dependencies, and events. It also tests policy
INSERT denial, dependency insertion outside the revision-1 window, schema-migration
append only under the offline migration authorizer, and startup authorizer self-test.

The end-to-end silence test runs T-SUCC-02: API counterexample aborts and leaves the
predecessor active, so quiet is false. In the offline corrupt copy the predecessor is
done and successor cancelled; heartbeat emits I007 and exit 20 and must not print
`all quiet`. Deleting a dependency in a corrupt copy emits I016 and likewise rejects
silence. This directly closes the reviewed clean-silence path.

## 8. Graduation and acceptance

| Rule | Design decision | Executable basis before operational claim |
|---|---|---|
| Phase-gate bridging | **GRADUATE only after implementation tests** | Owned revision-1 child, deferred atomic back-reference, unique use, T-SUCC-01–08 |
| Never claim active without proof | **GRADUATE only after implementation tests** | Claim-only edge, private context, unique live current tip, T-CLAIM-01–02 |
| Only named owner decision unblocks | **GRADUATE only after implementation tests** | Owner verification, scoped context, trigger binding, T-BLOCK-01 |
| Append-only canonical state/policy/audit | **GRADUATE only after implementation tests** | UPDATE/DELETE sealer matrix, P001, dependency commitment/I016 |
| Terminal sinks and transition validity | **GRADUATE (verified v3 design)** | Unchanged transition policy and append contract |
| Concurrent writers do not lose updates | **GRADUATE (verified v3 design)** | Unchanged revision CAS |
| Forward-only downgrade | **DESIGN CONTRACT** | Exact schema/algorithm/CLI/watermark/fixtures; fixtures must execute before cutover |
| Computed all quiet | **DESIGN CONTRACT** | Stable complete diagnostics and end-to-end counterexample; implementation must pass |

No row above claims deployed behavior. Operational graduation requires Claude-authored
implementation under applicable authority, all named fixtures against final DDL/API,
independent non-author review, filesystem/peer-credential tests, manifest comparison,
and one live heartbeat tick. The compatible artifact, reports, and fixture output are
preserved by digest. Failure leaves v3 canonical and blocks cutover.

## 9. Scope boundary

This is still the local immutable-ledger store, not a scheduler, release controller,
network authority, or owner-capability issuer. No verified v3 terminal/CAS mechanism
was redesigned. The only schema additions are columns/indexes and the event commitment
needed to bind successor creation, operation capability, proof tip, and dependency
integrity. Python remains standard-library only. WAL, short immediate transactions,
FULL synchronization, bounded busy jitter, and SQLite snapshot backup remain required.

Implementation is a later gate. Until the final DDL, typed API, all stable diagnostic
tests, downgrade fixtures, and independent review execute successfully, this document
specifies the remediation but does not prove it.
DESIGN-COMPLETE
Word count: 5486

--- DESIGN ENDS ---
