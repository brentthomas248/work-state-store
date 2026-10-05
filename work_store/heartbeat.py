"""Heartbeat query module (design section 4). Computed silence: `quiet` is
true only after every diagnostic is empty, integrity and foreign keys pass,
and the eligibility/active/blocked/pollable predicate is evaluated twice
(same or later snapshot) with no change. Any diagnostic or query failure
forces quiet=false; D000 is never silent.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .invariants import Diagnostic, InvariantQueryFailed, invariant_violations

EXIT_QUIET = 0
EXIT_DIAGNOSTICS = 20
EXIT_QUERY_FAILED = 30

_ELIGIBLE_QUEUED_SQL = """
SELECT c.id,c.revision,c.lane,c.env,c.brief_ref,c.payload FROM current_items c
WHERE c.status='queued' AND NOT EXISTS(
 SELECT 1 FROM work_item_dependencies d JOIN current_items p ON p.id=d.depends_on_id
 WHERE d.item_id=c.id AND p.status<>'done') ORDER BY c.created_at,c.id
"""

_FOUR_FIELD_SQL = """
SELECT
 (SELECT count(*) FROM current_items WHERE status='active') AS active_count,
 (SELECT count(*) FROM ({eligible})) AS eligible_queued_count,
 (SELECT count(*) FROM current_items WHERE status='blocked') AS blocked_count,
 (SELECT count(*) FROM dispatch_proofs p WHERE p.proof_kind='codex-task-url'
   AND NOT EXISTS(SELECT 1 FROM dispatch_proofs n WHERE n.previous_proof_id=p.proof_id)
 ) AS pollable_external_count
""".format(eligible=_ELIGIBLE_QUEUED_SQL)


@dataclass
class HeartbeatResult:
    quiet: bool
    diagnostics: list[Diagnostic]
    four_field: tuple
    exit_code: int


def eligible_queued(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(_ELIGIBLE_QUEUED_SQL).fetchall()
    finally:
        conn.row_factory = None


def four_field_predicate(conn: sqlite3.Connection) -> tuple:
    row = conn.execute(_FOUR_FIELD_SQL).fetchone()
    return tuple(row)


def compute_quiet(conn: sqlite3.Connection, now: str,
                   expected_fingerprint: str | None = None) -> HeartbeatResult:
    try:
        diags = invariant_violations(conn, now, expected_fingerprint)
        first_pass = four_field_predicate(conn)
        diags += invariant_violations(conn, now, expected_fingerprint)
        second_pass = four_field_predicate(conn)
    except InvariantQueryFailed as e:
        return HeartbeatResult(False, [Diagnostic("D000", "invariant-query-failed", str(e))],
                                (), EXIT_QUERY_FAILED)

    quiet = len(diags) == 0 and first_pass == second_pass
    if not quiet and len(diags) == 0:
        # Predicate changed between passes with no diagnostic cause: treat as
        # a query-failure-class condition rather than ever claiming silence.
        diags = [Diagnostic("D000", "predicate-unstable-across-passes",
                             f"{first_pass} != {second_pass}")]
    exit_code = EXIT_QUIET if quiet else EXIT_DIAGNOSTICS
    return HeartbeatResult(quiet, diags, second_pass, exit_code)
