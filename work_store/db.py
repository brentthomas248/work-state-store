"""Connection setup: authorizer policy, private operation context, SQL
functions, and the trust boundary described in design section 1.
"""
from __future__ import annotations

import dataclasses
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Optional

from .capability import OwnerCapabilityIssuer
from .util import now_rfc3339

CANONICAL_TABLES = {
    "schema_version",
    "schema_migrations",
    "status_transitions",
    "phase_gates",
    "work_items",
    "item_revisions",
    "dispatch_proofs",
    "work_item_dependencies",
    "item_events",
}

# Tables each operation kind may directly INSERT into. Triggers additionally
# fire AAFTER INSERT on item_revisions/dispatch_proofs/work_item_dependencies;
# those trigger-issued INSERTs land on item_events, which is included here.
ALLOWED_INSERTS: dict[str, frozenset[str]] = {
    "create_item": frozenset({"work_items", "item_revisions", "item_events"}),
    "add_dependency": frozenset({"work_item_dependencies", "item_events"}),
    "transition": frozenset({"item_revisions", "item_events"}),
    "claim": frozenset({"dispatch_proofs", "item_revisions", "item_events"}),
    "append_proof_observation": frozenset({"dispatch_proofs", "item_events"}),
    "request_block": frozenset({"item_revisions", "item_events"}),
    "resolve_block": frozenset({"item_revisions", "item_events"}),
    "complete": frozenset({"work_items", "item_revisions", "item_events"}),
    "add_note": frozenset({"item_events"}),
}


class OperationError(Exception):
    """Raised for any authorizer/trigger/capability rejection. Message is the
    stable reason string (e.g. 'successor-not-fresh-queued')."""


@dataclasses.dataclass
class OperationContext:
    kind: str
    item_id: str
    actor: str
    nonce: str
    prior_revision: Optional[int] = None
    decision_ref: Optional[str] = None
    successor_id: Optional[str] = None
    detail: Optional[str] = None
    # Server-minted transaction time, sampled AFTER BEGIN IMMEDIATE acquires
    # the write lock. Every timestamp the operation writes, and every
    # expiry comparison the triggers make (op_now()), uses this value; it
    # is never accepted from the caller.
    now: Optional[str] = None


class _ContextHolder:
    def __init__(self) -> None:
        self.current: Optional[OperationContext] = None
        # Service-owned owner-capability verifier. Constructed here, with a
        # secret generated here, when the service opens its connection; the
        # typed API reads it from this private holder and never accepts an
        # issuer object from its caller.
        self.owner_issuer = OwnerCapabilityIssuer()


class StoreConnection(sqlite3.Connection):
    """Plain sqlite3.Connection subclass so we have somewhere to hang the
    private per-connection operation-context holder (stock sqlite3.Connection
    instances have no __dict__)."""


def _authorizer_factory(holder: _ContextHolder):
    def authorizer(action, arg1, arg2, dbname, source):
        if action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if arg1 in CANONICAL_TABLES:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_INSERT:
            ctx = holder.current
            if ctx is None:
                return sqlite3.SQLITE_DENY
            allowed = ALLOWED_INSERTS.get(ctx.kind, frozenset())
            if arg1 in CANONICAL_TABLES and arg1 not in allowed:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action in (
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
        ):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            # Read-only introspection pragmas only; all writable pragmas
            # (journal_mode, synchronous, foreign_keys, wal_checkpoint, ...)
            # are configured once at connect() time before the authorizer
            # is installed, and are denied for any later request.
            name = (arg1 or "").lower()
            if name in ("table_info", "foreign_key_check", "foreign_key_list",
                        "integrity_check", "quick_check"):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


def connect(path: str) -> sqlite3.Connection:
    """Open a service connection. Assumes schema 3 already migrated."""
    conn = sqlite3.connect(path, isolation_level=None, factory=StoreConnection)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")

    holder = _ContextHolder()
    conn._op_holder = holder  # type: ignore[attr-defined]

    conn.create_function("op_kind", 0, lambda: holder.current and holder.current.kind, deterministic=False)
    conn.create_function("op_item", 0, lambda: holder.current and holder.current.item_id, deterministic=False)
    conn.create_function("op_prior", 0, lambda: holder.current and holder.current.prior_revision, deterministic=False)
    conn.create_function("op_decision_ref", 0, lambda: holder.current and holder.current.decision_ref, deterministic=False)
    conn.create_function("op_successor", 0, lambda: holder.current and holder.current.successor_id, deterministic=False)
    conn.create_function("op_nonce", 0, lambda: holder.current and holder.current.nonce, deterministic=False)
    conn.create_function("actor_context", 0, lambda: holder.current and holder.current.actor, deterministic=False)
    conn.create_function("event_detail_context", 0, lambda: holder.current and holder.current.detail, deterministic=False)
    conn.create_function("op_now", 0, lambda: holder.current and holder.current.now, deterministic=False)

    conn.set_authorizer(_authorizer_factory(holder))
    return conn


def mint_nonce() -> str:
    return secrets.token_hex(32)


def _server_now() -> str:
    """The service clock. Indirection point so tests can advance server
    time; callers of the typed API have no way to influence it."""
    return now_rfc3339()


def owner_capability_issuer(conn: sqlite3.Connection) -> OwnerCapabilityIssuer:
    """Owner-side issuance channel for this service connection's capability
    verifier. The typed API (api.py) verifies tokens ONLY against this
    service-owned issuer; tokens signed by any other issuer instance fail
    signature verification. In this single-process build the owner channel
    is an in-process function rather than a separate authenticated socket
    (see BUILD-REPORT.md, STAYS PROSE)."""
    return conn._op_holder.owner_issuer  # type: ignore[attr-defined]


def checkpoint(path: str) -> None:
    """WAL checkpoint via a dedicated maintenance connection (no authorizer,
    since writable pragmas are denied on request-serving connections)."""
    admin = sqlite3.connect(path, isolation_level=None)
    try:
        admin.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        admin.close()


@contextmanager
def operation(conn: sqlite3.Connection, kind: str, item_id: str, actor: str,
              prior_revision: Optional[int] = None,
              decision_ref: Optional[str] = None,
              successor_id: Optional[str] = None,
              detail: Optional[str] = None,
              nonce: Optional[str] = None):
    """Install a private OperationContext for exactly one transaction.

    Nested contexts are refused. The context (and its nonce) is cleared in
    a finally block after COMMIT or ROLLBACK, regardless of outcome.
    """
    holder: _ContextHolder = conn._op_holder  # type: ignore[attr-defined]
    if holder.current is not None:
        raise OperationError("nested-operation-context-forbidden")
    ctx = OperationContext(
        kind=kind, item_id=item_id, actor=actor, nonce=nonce or mint_nonce(),
        prior_revision=prior_revision, decision_ref=decision_ref,
        successor_id=successor_id, detail=detail,
    )
    holder.current = ctx
    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        holder.current = None
        raise
    try:
        ctx.now = _server_now()
        yield ctx
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        holder.current = None
