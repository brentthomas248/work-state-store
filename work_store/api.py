"""Typed RPC surface (design section 1). Every mutating call opens exactly
one `db.operation()` context (BEGIN IMMEDIATE .. COMMIT/ROLLBACK) and issues
only parameterized SQL; it never accepts or runs caller SQL. Every row it
inserts carries the private connection's operation_nonce so the triggers in
schema.py can prove the insert happened inside a verified capability.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from . import db
from .db import OperationError
from .capability import OwnerCapabilityIssuer, CapabilityError
from .util import now_rfc3339, canonical_json


def _raise_op(exc: sqlite3.Error) -> None:
    raise OperationError(str(exc)) from exc


def _current_tip(conn: sqlite3.Connection, item_id: str) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM item_revisions WHERE item_id=? "
        "AND revision=(SELECT max(x.revision) FROM item_revisions x WHERE x.item_id=?)",
        (item_id, item_id),
    )
    row = cur.fetchone()
    conn.row_factory = None
    return row


def _next_revision(conn: sqlite3.Connection, item_id: str) -> int:
    cur = conn.execute(
        "SELECT coalesce(max(revision),0)+1 FROM item_revisions WHERE item_id=?",
        (item_id,),
    )
    return cur.fetchone()[0]


def _next_proof_id(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT coalesce(max(proof_id),0)+1 FROM dispatch_proofs")
    return cur.fetchone()[0]


# ---------------------------------------------------------------- create_item

def create_item(conn: sqlite3.Connection, item_id: str, title: str, phase: str,
                 lane: str, env: str, actor: str, brief_ref: Optional[str] = None,
                 payload: Optional[dict] = None, occurred_at: Optional[str] = None) -> dict:
    occurred_at = occurred_at or now_rfc3339()
    payload_json = canonical_json(payload or {})
    try:
        with db.operation(conn, "create_item", item_id, actor) as ctx:
            conn.execute(
                "INSERT INTO work_items(id,title,phase,lane,env,brief_ref,created_at,created_actor) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (item_id, title, phase, lane, env, brief_ref, occurred_at, actor),
            )
            conn.execute(
                "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                "occurred_at,actor,operation_nonce) VALUES (?,1,NULL,'queued',?,?,?,?)",
                (item_id, payload_json, occurred_at, actor, ctx.nonce),
            )
    except sqlite3.Error as e:
        _raise_op(e)
    return {"item_id": item_id, "revision": 1}


# ------------------------------------------------------------- add_dependency

def add_dependency(conn: sqlite3.Connection, item_id: str, depends_on_id: str,
                    actor: str, occurred_at: Optional[str] = None) -> None:
    occurred_at = occurred_at or now_rfc3339()
    try:
        with db.operation(conn, "add_dependency", item_id, actor) as ctx:
            conn.execute(
                "INSERT INTO work_item_dependencies(item_id,depends_on_id,created_at,"
                "created_actor,operation_nonce) VALUES (?,?,?,?,?)",
                (item_id, depends_on_id, occurred_at, actor, ctx.nonce),
            )
    except sqlite3.Error as e:
        _raise_op(e)


# ----------------------------------------------------------------- transition

def transition(conn: sqlite3.Connection, item_id: str, expected_revision: int,
                new_status: str, evidence: Optional[dict], actor: str,
                occurred_at: Optional[str] = None) -> dict:
    """Generic CAS transition. Never touches block-resolution or completion
    fields; those require resolve_block/complete respectively. Attempting a
    queued -> active transition here is always rejected (claim-only)."""
    occurred_at = occurred_at or now_rfc3339()
    prior = _current_tip(conn, item_id)
    payload_json = canonical_json(evidence) if evidence is not None else (
        prior["payload"] if prior is not None else "{}"
    )
    new_revision = expected_revision + 1
    try:
        with db.operation(conn, "transition", item_id, actor, prior_revision=expected_revision) as ctx:
            conn.execute(
                "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                "occurred_at,actor,operation_nonce) VALUES (?,?,?,?,?,?,?,?)",
                (item_id, new_revision, expected_revision, new_status, payload_json,
                 occurred_at, actor, ctx.nonce),
            )
    except sqlite3.Error as e:
        _raise_op(e)
    return {"item_id": item_id, "revision": new_revision}


# ----------------------------------------------------------------------- claim

def claim(conn: sqlite3.Connection, item_id: str, expected_revision: int,
          proof: dict, actor: str, occurred_at: Optional[str] = None) -> dict:
    """queued -> active. Mints one fresh root dispatch proof, then the active
    revision referencing it, atomically, in the same operation_nonce."""
    occurred_at = occurred_at or now_rfc3339()
    new_revision = expected_revision + 1
    proof_kind = proof["proof_kind"]
    proof_value = proof["proof_value"]
    host_id = proof.get("host_id")
    issued_actor = proof.get("issued_actor", actor)
    recorded_at = proof.get("recorded_at", occurred_at)
    observed_at = proof.get("observed_at", occurred_at)
    valid_until = proof["valid_until"]
    observation = proof.get("observation", "live")
    observer_actor = proof.get("observer_actor", actor)
    prior = _current_tip(conn, item_id)
    payload_json = prior["payload"] if prior is not None else "{}"
    try:
        with db.operation(conn, "claim", item_id, actor, prior_revision=expected_revision) as ctx:
            proof_id = _next_proof_id(conn)
            conn.execute(
                "INSERT INTO dispatch_proofs(proof_id,item_id,previous_proof_id,root_proof_id,"
                "proof_kind,proof_value,host_id,issued_actor,recorded_at,observed_at,valid_until,"
                "observation,observer_actor,operation_nonce) VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?)",
                (proof_id, item_id, proof_id, proof_kind, proof_value, host_id, issued_actor,
                 recorded_at, observed_at, valid_until, observation, observer_actor, ctx.nonce),
            )
            conn.execute(
                "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                "occurred_at,actor,operation_nonce,dispatch_root_id) VALUES (?,?,?,'active',?,?,?,?,?)",
                (item_id, new_revision, expected_revision, payload_json, occurred_at, actor,
                 ctx.nonce, proof_id),
            )
    except sqlite3.Error as e:
        _raise_op(e)
    return {"item_id": item_id, "revision": new_revision, "proof_id": proof_id}


# ------------------------------------------------------- append_proof_observation

def append_proof_observation(conn: sqlite3.Connection, item_id: str, previous_proof_id: int,
                              observation: str, actor: str, observed_at: Optional[str] = None,
                              valid_until: Optional[str] = None,
                              recorded_at: Optional[str] = None) -> dict:
    observed_at = observed_at or now_rfc3339()
    valid_until = valid_until or observed_at
    recorded_at = recorded_at or observed_at
    cur = conn.execute("SELECT * FROM dispatch_proofs WHERE proof_id=?", (previous_proof_id,))
    prev = cur.fetchone()
    if prev is None:
        raise OperationError("no-such-previous-proof")
    conn.row_factory = None
    cols = [d[0] for d in cur.description]
    prev_map = dict(zip(cols, prev))
    try:
        with db.operation(conn, "append_proof_observation", item_id, actor) as ctx:
            proof_id = _next_proof_id(conn)
            conn.execute(
                "INSERT INTO dispatch_proofs(proof_id,item_id,previous_proof_id,root_proof_id,"
                "proof_kind,proof_value,host_id,issued_actor,recorded_at,observed_at,valid_until,"
                "observation,observer_actor,operation_nonce) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proof_id, item_id, previous_proof_id, prev_map["root_proof_id"],
                 prev_map["proof_kind"], prev_map["proof_value"], prev_map["host_id"],
                 prev_map["issued_actor"], recorded_at, observed_at, valid_until,
                 observation, actor, ctx.nonce),
            )
    except sqlite3.Error as e:
        _raise_op(e)
    return {"proof_id": proof_id}


# -------------------------------------------------------------- request_block

def request_block(conn: sqlite3.Connection, item_id: str, expected_revision: int,
                   decision_ref: str, text: str, actor: str,
                   occurred_at: Optional[str] = None) -> dict:
    occurred_at = occurred_at or now_rfc3339()
    new_revision = expected_revision + 1
    prior = _current_tip(conn, item_id)
    payload_json = prior["payload"] if prior is not None else "{}"
    try:
        with db.operation(conn, "request_block", item_id, actor, prior_revision=expected_revision) as ctx:
            conn.execute(
                "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                "occurred_at,actor,operation_nonce,block_decision_ref,block_decision_text,"
                "block_requested_on) VALUES (?,?,?,'blocked',?,?,?,?,?,?,?)",
                (item_id, new_revision, expected_revision, payload_json, occurred_at, actor,
                 ctx.nonce, decision_ref, text, occurred_at),
            )
    except sqlite3.Error as e:
        _raise_op(e)
    return {"item_id": item_id, "revision": new_revision}


# -------------------------------------------------------------- resolve_block

def resolve_block(conn: sqlite3.Connection, item_id: str, expected_revision: int,
                   exact_ref: str, resolution: dict, owner_capability: str,
                   issuer: OwnerCapabilityIssuer, occurred_at: Optional[str] = None) -> dict:
    """resolution = {"new_status": "queued"|"superseded"|"cancelled",
    "text": str, "actor": str}. The owner_capability token is verified and
    reserved BEFORE the transaction opens; finalized on commit, released on
    rollback (design section 1: transaction-scoped, unforgeable, with
    rollback/replay semantics)."""
    occurred_at = occurred_at or now_rfc3339()
    new_status = resolution["new_status"]
    resolution_text = resolution["text"]
    resolution_actor = resolution["actor"]
    new_revision = expected_revision + 1

    try:
        issuer.reserve(owner_capability, item_id, expected_revision, exact_ref, "resolve_block")
    except CapabilityError as e:
        raise OperationError(str(e)) from e

    prior = _current_tip(conn, item_id)
    payload_json = prior["payload"] if prior is not None else "{}"
    try:
        with db.operation(conn, "resolve_block", item_id, resolution_actor,
                           prior_revision=expected_revision, decision_ref=exact_ref) as ctx:
            conn.execute(
                "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                "occurred_at,actor,operation_nonce,resolved_decision_ref,resolved_on,"
                "resolution_text,resolution_actor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, new_revision, expected_revision, new_status, payload_json,
                 occurred_at, resolution_actor, ctx.nonce, exact_ref, occurred_at,
                 resolution_text, resolution_actor),
            )
    except sqlite3.Error as e:
        issuer.release(owner_capability)
        _raise_op(e)
    except Exception:
        issuer.release(owner_capability)
        raise
    else:
        issuer.finalize(owner_capability)
    return {"item_id": item_id, "revision": new_revision}


# -------------------------------------------------------------------- add_note

def add_note(conn: sqlite3.Connection, item_id: str, expected_revision: int,
             note: str, actor: str, occurred_at: Optional[str] = None) -> None:
    occurred_at = occurred_at or now_rfc3339()
    tip = _current_tip(conn, item_id)
    if tip is None or tip["revision"] != expected_revision:
        raise OperationError("stale-version")
    try:
        with db.operation(conn, "add_note", item_id, actor, prior_revision=expected_revision) as ctx:
            conn.execute(
                "INSERT INTO item_events(item_id,revision,occurred_at,actor,event_type,detail) "
                "VALUES (?,?,?,?,'operator-note',?)",
                (item_id, expected_revision, occurred_at, actor, note),
            )
    except sqlite3.Error as e:
        _raise_op(e)


# ---------------------------------------------------------------------- complete

def complete(conn: sqlite3.Connection, item_id: str, expected_revision: int,
             completion: dict, actor: str, occurred_at: Optional[str] = None,
             owner_capability: Optional[str] = None,
             issuer: Optional[OwnerCapabilityIssuer] = None) -> dict:
    """completion is one of:
      {"successor_title": str, "successor_lane": str, "successor_env": str,
       "successor_brief_ref": str|None}          -> gated/plain successor arm
      {"exception_kind": "final-phase", "reason": str}
      {"exception_kind": "owner-decision", "reason": str,
       "decision_date": str, "decision_ref": str}
    The service -- never the caller -- selects the fresh successor UUID.
    The owner-decision arm requires a capability token from the same
    `issuer` used by resolve_block, scoped to operation="complete" and the
    exact decision_ref; it is reserved before the transaction opens and
    finalized/released exactly like resolve_block's capability.
    """
    occurred_at = occurred_at or now_rfc3339()
    new_revision = expected_revision + 1
    prior = _current_tip(conn, item_id)
    payload_json = prior["payload"] if prior is not None else "{}"

    predecessor_phase = conn.execute(
        "SELECT phase FROM work_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    gate = conn.execute(
        "SELECT successor_phase FROM phase_gates WHERE predecessor_phase=? AND required=1",
        (predecessor_phase,),
    ).fetchone()

    exception_kind = completion.get("exception_kind")
    successor_id = None

    try:
        if exception_kind == "owner-decision":
            reason = completion["reason"]
            decision_date = completion["decision_date"]
            decision_ref = completion["decision_ref"]
            if issuer is None or owner_capability is None:
                raise OperationError("owner-decision-capability-required")
            try:
                issuer.reserve(owner_capability, item_id, expected_revision, decision_ref, "complete")
            except CapabilityError as e:
                raise OperationError(str(e)) from e
            try:
                with db.operation(conn, "complete", item_id, actor, prior_revision=expected_revision,
                                   decision_ref=decision_ref) as ctx:
                    conn.execute(
                        "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                        "occurred_at,actor,operation_nonce,exception_kind,exception_reason,"
                        "exception_decision_date,exception_decision_ref) "
                        "VALUES (?,?,?,'done',?,?,?,?,?,?,?,?)",
                        (item_id, new_revision, expected_revision, payload_json, occurred_at, actor,
                         ctx.nonce, exception_kind, reason, decision_date, decision_ref),
                    )
            except sqlite3.Error:
                issuer.release(owner_capability)
                raise
            except Exception:
                issuer.release(owner_capability)
                raise
            else:
                issuer.finalize(owner_capability)
        elif exception_kind == "final-phase":
            reason = completion["reason"]
            with db.operation(conn, "complete", item_id, actor, prior_revision=expected_revision) as ctx:
                conn.execute(
                    "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                    "occurred_at,actor,operation_nonce,exception_kind,exception_reason) "
                    "VALUES (?,?,?,'done',?,?,?,?,?,?)",
                    (item_id, new_revision, expected_revision, payload_json, occurred_at, actor,
                     ctx.nonce, exception_kind, reason),
                )
        else:
            successor_id = str(uuid.uuid4())
            successor_phase = gate[0] if gate is not None else completion["successor_phase"]
            with db.operation(conn, "complete", item_id, actor, prior_revision=expected_revision,
                               successor_id=successor_id) as ctx:
                conn.execute(
                    "INSERT INTO work_items(id,title,phase,lane,env,brief_ref,created_at,created_actor) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (successor_id, completion["successor_title"], successor_phase,
                     completion["successor_lane"], completion["successor_env"],
                     completion.get("successor_brief_ref"), occurred_at, actor),
                )
                conn.execute(
                    "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                    "occurred_at,actor,operation_nonce,successor_owner_item_id,"
                    "successor_owner_revision,successor_owner_nonce) "
                    "VALUES (?,1,NULL,'queued','{}',?,?,?,?,?,?)",
                    (successor_id, occurred_at, actor, ctx.nonce, item_id, new_revision, ctx.nonce),
                )
                conn.execute(
                    "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                    "occurred_at,actor,operation_nonce,completion_successor_id,"
                    "completion_successor_phase) VALUES (?,?,?,'done',?,?,?,?,?,?)",
                    (item_id, new_revision, expected_revision, payload_json, occurred_at, actor,
                     ctx.nonce, successor_id, successor_phase),
                )
    except sqlite3.Error as e:
        _raise_op(e)
    return {"item_id": item_id, "revision": new_revision, "successor_id": successor_id}


# -------------------------------------------------------------------- snapshot

def snapshot(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    try:
        items = [dict(r) for r in conn.execute("SELECT * FROM work_items ORDER BY id")]
        revisions = [dict(r) for r in conn.execute(
            "SELECT * FROM item_revisions ORDER BY item_id,revision")]
        proofs = [dict(r) for r in conn.execute(
            "SELECT * FROM dispatch_proofs ORDER BY proof_id")]
        deps = [dict(r) for r in conn.execute(
            "SELECT * FROM work_item_dependencies ORDER BY item_id,depends_on_id")]
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM item_events ORDER BY event_id")]
    finally:
        conn.row_factory = None
    return {
        "items": items, "revisions": revisions, "proofs": proofs,
        "dependencies": deps, "events": events,
    }
