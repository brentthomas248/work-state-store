"""Builds the single rich baseline database that the five checked-in
downgrade fixtures (brief.md 6.4) are generated from: every status,
transition, completion arm, proof observation, resolution, dependency,
note/history event, null optional, Unicode string, and (via payload) an
opaque nested object a legacy reader would not understand.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from . import api, db, migrate
from .capability import OwnerCapabilityIssuer
from .util import now_rfc3339


def build_baseline_conn() -> sqlite3.Connection:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    raw = sqlite3.connect(path)
    migrate.apply_schema(raw, now_rfc3339(), "fixture-builder")
    raw.close()
    conn = db.connect(path)
    issuer = OwnerCapabilityIssuer()

    # A: queued -> active -> done (gated successor arm), Unicode title/payload.
    api.create_item(conn, "A", "Tëst item → 日本語 💥", "design-accepted", "operator", "local",
                     "tester", payload={"unmapped_legacy_field": {"nested": [1, 2, "x"]}})
    api.claim(conn, "A", 1, {"proof_kind": "worker-pid", "proof_value": "pid-1",
                             "host_id": "host-1", "valid_until": "2999-01-01T00:00:00.000000Z"},
              "tester")
    api.add_note(conn, "A", 2, "note on A", "tester")
    api.complete(conn, "A", 2, {"successor_title": "A successor", "successor_lane": "operator",
                                "successor_env": "local"}, "tester")

    # C: queued -> active -> done (final-phase exception; no gate for this phase).
    api.create_item(conn, "C", "item C", "misc-phase", "operator", "local", "tester")
    api.claim(conn, "C", 1, {"proof_kind": "codex-task-url", "proof_value": "https://x/1",
                             "valid_until": "2999-01-01T00:00:00.000000Z"}, "tester")
    api.complete(conn, "C", 2, {"exception_kind": "final-phase", "reason": "nothing further required"},
                 "tester")

    # D: queued -> active -> done (owner-decision exception, via capability).
    api.create_item(conn, "D", "item D", "misc-phase-2", "operator", "local", "tester")
    api.claim(conn, "D", 1, {"proof_kind": "worker-pid", "proof_value": "pid-2", "host_id": "host-2",
                             "valid_until": "2999-01-01T00:00:00.000000Z"}, "tester")
    token = issuer.issue("D", 2, "DEC-OWNER-1", operation="complete")
    api.complete(conn, "D", 2, {"exception_kind": "owner-decision", "reason": "owner override",
                                "decision_date": now_rfc3339(), "decision_ref": "DEC-OWNER-1"},
                 "tester", owner_capability=token, issuer=issuer)

    # E/F: dependency tuple (added while both revision-1/queued), then E is
    # claimed, revoked-then-reclaimed proof history, blocked, and resolved
    # back to queued (covers resolution fields + a multi-hop proof chain).
    api.create_item(conn, "E", "item E", "design-accepted", "operator", "local", "tester")
    api.create_item(conn, "F", "item F", "design-accepted", "operator", "local", "tester")
    api.add_dependency(conn, "F", "E", "tester")
    claimed = api.claim(conn, "E", 1, {"proof_kind": "worker-pid", "proof_value": "pid-3",
                                       "host_id": "host-3",
                                       "valid_until": "2999-01-01T00:00:00.000000Z"}, "tester")
    api.append_proof_observation(conn, "E", claimed["proof_id"], "revoked", "tester",
                                  observed_at=now_rfc3339(), valid_until="2999-01-01T00:00:00.000000Z")
    api.request_block(conn, "E", 2, "DEC-BLOCK-1", "need a call", "tester")
    block_token = issuer.issue("E", 3, "DEC-BLOCK-1")
    api.resolve_block(conn, "E", 3, "DEC-BLOCK-1",
                       {"new_status": "queued", "text": "resolved, proceed", "actor": "owner"},
                       block_token, issuer)

    # G: queued -> cancelled (terminal sink, no gate/claim involved).
    api.create_item(conn, "G", "item G", "design-accepted", "operator", "local", "tester")
    api.transition(conn, "G", 1, "cancelled", None, "tester")

    # H: queued -> superseded directly (the other terminal sink).
    api.create_item(conn, "H", "item H", "design-accepted", "operator", "local", "tester")
    api.transition(conn, "H", 1, "superseded", None, "tester")

    return conn, path


def cleanup(path: str) -> None:
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass
