"""Stable invariant inventory (design section 3). All checks run in one read
transaction; prepare/bind/step/decode/commit failure is D000 and suppresses
silence (design section 4). IDs are permanent; never renumber them.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .util import canonical_hash

STATUS_TRANSITIONS_SEED = [
    ("queued", "active", 0), ("queued", "blocked", 0), ("active", "done", 0),
    ("active", "blocked", 0), ("blocked", "queued", 1), ("queued", "superseded", 0),
    ("queued", "cancelled", 0), ("active", "superseded", 0), ("active", "cancelled", 0),
    ("blocked", "superseded", 1), ("blocked", "cancelled", 1),
]
PHASE_GATES_SEED = [
    ("design-accepted", "implementation-build", 1, "Accepted design must produce build work"),
    ("build-complete", "independent-review", 1, "Completed build requires non-author review"),
    ("review-accepted", "merge-or-PR", 1, "Accepted review produces integration work"),
]


@dataclass
class Diagnostic:
    diag_id: str
    subject: str
    detail: str


class InvariantQueryFailed(Exception):
    """D000: prepare/bind/step/decode/commit failure. Never silent."""


_STATE_QUERY = """
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
SELECT 'I004',r.item_id,CAST(r.revision AS TEXT) FROM item_revisions r
JOIN work_items w ON w.id=r.item_id
WHERE r.status='done' AND r.exception_kind='final-phase' AND EXISTS(
 SELECT 1 FROM phase_gates g WHERE g.predecessor_phase=w.phase AND g.required=1)
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
"""

_CYCLE_QUERY = """
WITH RECURSIVE reach(origin,node,path) AS (
 SELECT item_id,depends_on_id,','||item_id||','||depends_on_id||','
 FROM work_item_dependencies
 UNION ALL
 SELECT r.origin,d.depends_on_id,r.path||d.depends_on_id||',' FROM reach r
 JOIN work_item_dependencies d ON d.item_id=r.node
 WHERE instr(r.path,','||d.depends_on_id||',')=0)
SELECT 'I017',d.item_id,'cycle' FROM work_item_dependencies d
JOIN reach r ON r.origin=d.depends_on_id AND r.node=d.item_id GROUP BY d.item_id;
"""


def _p001_hash(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    parts = []
    for type_, name, tbl_name, sql in rows:
        sql_norm = (sql or "").replace("\r\n", "\n").replace("\r", "\n")
        sql_norm = "\n".join(line.rstrip() for line in sql_norm.split("\n"))
        parts.append("\x00".join([type_, name, tbl_name, sql_norm]))
    joined = "\x00".join(parts)
    import hashlib
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def artifact_fingerprint(conn: sqlite3.Connection) -> str:
    return _p001_hash(conn)


def check_policy_tables(conn: sqlite3.Connection) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    actual_st = set(conn.execute("SELECT from_status,to_status,requires_block_resolution FROM status_transitions"))
    expected_st = set(STATUS_TRANSITIONS_SEED)
    if actual_st != expected_st:
        diags.append(Diagnostic("P002", "status_transitions", "drift"))
    actual_pg = set(conn.execute("SELECT predecessor_phase,successor_phase,required,description FROM phase_gates"))
    expected_pg = set(PHASE_GATES_SEED)
    if actual_pg != expected_pg:
        diags.append(Diagnostic("P003", "phase_gates", "drift"))
    return diags


def check_schema_artifact(conn: sqlite3.Connection, expected_fingerprint: str) -> list[Diagnostic]:
    actual = _p001_hash(conn)
    if actual != expected_fingerprint:
        return [Diagnostic("P001", "schema", f"expected={expected_fingerprint} actual={actual}")]
    return []


def run_state_invariants(conn: sqlite3.Connection, now: str) -> list[Diagnostic]:
    try:
        rows = conn.execute(_STATE_QUERY, {"now": now}).fetchall()
        cycle_rows = conn.execute(_CYCLE_QUERY).fetchall()
    except sqlite3.Error as e:
        raise InvariantQueryFailed(str(e)) from e
    return [Diagnostic(r[0], r[1], r[2]) for r in list(rows) + list(cycle_rows)]


def run_integrity_checks(conn: sqlite3.Connection) -> list[Diagnostic]:
    diags = []
    try:
        res = conn.execute("PRAGMA integrity_check").fetchall()
        if not (len(res) == 1 and res[0][0] == "ok"):
            diags.append(Diagnostic("D000", "integrity_check", str(res)))
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            diags.append(Diagnostic("D000", "foreign_key_check", str(fk)))
    except sqlite3.Error as e:
        raise InvariantQueryFailed(str(e)) from e
    return diags


def invariant_violations(conn: sqlite3.Connection, now: str,
                          expected_fingerprint: str | None = None) -> list[Diagnostic]:
    """Run all mandatory diagnostics in one pass. Raises InvariantQueryFailed
    (D000) on any prepare/bind/step/decode failure rather than ever
    returning a clean-but-wrong empty list."""
    try:
        diags: list[Diagnostic] = []
        if expected_fingerprint is not None:
            diags += check_schema_artifact(conn, expected_fingerprint)
        diags += check_policy_tables(conn)
        diags += run_integrity_checks(conn)
        diags += run_state_invariants(conn, now)
        return diags
    except InvariantQueryFailed:
        raise
    except sqlite3.Error as e:
        raise InvariantQueryFailed(str(e)) from e
