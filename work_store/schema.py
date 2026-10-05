"""Exact schema-3 DDL template for the v3.1 immutable revision ledger.

The template is the literal SQL text from the design document (section 2),
with three bind tokens left in place for the migration runner: the
migration checksum, the applied-at timestamp, and the migration actor.
The checksum covers this exact template text (LF newlines, tokens
unsubstituted).
"""

SCHEMA_SQL_TEMPLATE = """\
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

 /* Mechanical limits on the two exception arms (acceptance criterion A):
    final-phase is admitted only when the predecessor phase truly has no
    required gate, and owner-decision is admitted only through a consumed
    owner capability bound to this exact decision reference, exactly as
    resolve_block binds block-resolution capability above. */
 SELECT CASE WHEN NEW.status='done' AND NEW.exception_kind='final-phase' AND EXISTS(
  SELECT 1 FROM phase_gates g JOIN work_items w ON w.id=NEW.item_id
  WHERE g.predecessor_phase=w.phase AND g.required=1)
  THEN RAISE(ABORT,'exception-not-actually-final-phase') END;
 SELECT CASE WHEN NEW.status='done' AND NEW.exception_kind='owner-decision' AND
  (op_kind()<>'complete' OR op_decision_ref() IS NULL OR
   NEW.exception_decision_ref<>op_decision_ref())
  THEN RAISE(ABORT,'owner-decision-capability-required') END;
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
"""
