"""Legacy (schema=2) interchange projection: export, import, watermark, and
canonical comparison, per brief.md section 6.1.

`export_legacy` and `compute_watermark` only issue SELECTs, so they accept
any sqlite3.Connection (authorized service connection or a bare offline
connection opened directly against a .db file). `import_legacy` rebuilds a
fresh schema-3 database directly from the `_canonical_v3` extension of a
projection, using raw INSERTs on a bare connection -- this is an offline
rebuild tool that runs before any service ever opens the file, exactly like
`migrate.apply_schema`, so it does not go through `db.connect()`'s
authorizer/operation-context machinery (there is no live operation to bind
to; the rows being replayed already carry their original audit trail).
The six insert-contract/event triggers it suspends for the literal replay
are recreated from their exact stored SQL before commit, and the rebuilt
database's full trigger set is verified against a fresh schema-3 artifact,
so an imported database enforces exactly what a migrated one does.
"""
from __future__ import annotations

import json
import os
import sqlite3

from . import migrate
from .util import canonical_hash, canonical_json, now_rfc3339

ENCODING = "work-state-v3-extension/1"

# Suspended only for the duration of the offline replay; always recreated.
_REPLAY_SUSPENDED_TRIGGERS = (
    "dependency_insert_contract", "dependency_insert_event",
    "revision_insert_contract", "revision_insert_event",
    "proof_insert_contract", "proof_insert_event",
)


class ImportIntegrityError(Exception):
    """The rebuilt database does not carry the full schema-3 enforcement
    artifact set."""


def _trigger_sql(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='trigger' ORDER BY name"
    ).fetchall())

_REVISION_COLUMNS = [
    "item_id", "revision", "prior_revision", "status", "payload",
    "occurred_at", "actor", "detail", "operation_nonce",
    "dispatch_root_id", "block_decision_ref", "block_decision_text",
    "block_requested_on", "resolved_decision_ref", "resolved_on",
    "resolution_text", "resolution_actor", "completion_successor_id",
    "completion_successor_phase", "exception_kind", "exception_reason",
    "exception_decision_date", "exception_decision_ref",
    "successor_owner_item_id", "successor_owner_revision",
    "successor_owner_nonce",
]
_PROOF_COLUMNS = [
    "proof_id", "item_id", "previous_proof_id", "root_proof_id",
    "proof_kind", "proof_value", "host_id", "issued_actor", "recorded_at",
    "observed_at", "valid_until", "observation", "observer_actor",
    "operation_nonce",
]
_EVENT_COLUMNS = [
    "event_id", "item_id", "revision", "occurred_at", "actor",
    "event_type", "detail", "metadata",
]
_DEPENDENCY_COLUMNS = ["item_id", "depends_on_id", "created_at", "created_actor", "operation_nonce"]
_IDENTITY_FIELDS = ["id", "title", "phase", "lane", "env", "brief_ref", "created_at", "created_actor"]


def _row_dict(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def _revision_dict(row):
    d = dict(zip(_REVISION_COLUMNS, row))
    d["payload"] = json.loads(d["payload"]) if d["payload"] is not None else None
    return d


def _completion_arm(rev: dict):
    if rev["status"] != "done":
        return None
    if rev["completion_successor_id"] is not None:
        return {
            "arm": "successor",
            "id": rev["completion_successor_id"],
            "phase": rev["completion_successor_phase"],
        }
    return {
        "arm": "exception",
        "kind": rev["exception_kind"],
        "reason": rev["exception_reason"],
        "date": rev["exception_decision_date"],
        "ref": rev["exception_decision_ref"],
    }


def _resolution(revisions: list[dict]):
    for rev in reversed(revisions):
        if rev["resolved_decision_ref"] is not None:
            return {
                "resolved_decision_ref": rev["resolved_decision_ref"],
                "resolved_on": rev["resolved_on"],
                "resolution_text": rev["resolution_text"],
                "resolution_actor": rev["resolution_actor"],
            }
    return None


def identity_sha256(item_row: dict) -> str:
    identity = {k: item_row[k] for k in _IDENTITY_FIELDS}
    return canonical_hash(identity)


def compute_watermark(conn: sqlite3.Connection) -> dict:
    (schema_version,) = conn.execute("SELECT version FROM schema_version").fetchone()
    max_event_id = conn.execute("SELECT coalesce(max(event_id),0) FROM item_events").fetchone()[0]
    max_proof_id = conn.execute("SELECT coalesce(max(proof_id),0) FROM dispatch_proofs").fetchone()[0]
    items = {
        item_id: max_rev
        for item_id, max_rev in conn.execute(
            "SELECT item_id, max(revision) FROM item_revisions GROUP BY item_id ORDER BY item_id"
        )
    }
    return {
        "schema_version": schema_version,
        "max_event_id": max_event_id,
        "max_proof_id": max_proof_id,
        "items": items,
    }


def export_legacy(conn: sqlite3.Connection, generation: int = 1) -> dict:
    items_out = []
    item_cur = conn.execute(
        "SELECT id,title,phase,lane,env,brief_ref,created_at,created_actor "
        "FROM work_items ORDER BY id"
    )
    for item_row in item_cur.fetchall():
        item = _row_dict(item_cur, item_row)
        item_id = item["id"]

        revisions = [
            _revision_dict(r)
            for r in conn.execute(
                "SELECT " + ",".join(_REVISION_COLUMNS) +
                " FROM item_revisions WHERE item_id=? ORDER BY revision", (item_id,)
            ).fetchall()
        ]
        tip = revisions[-1] if revisions else None

        proofs = [
            dict(zip(_PROOF_COLUMNS, r))
            for r in conn.execute(
                "SELECT " + ",".join(_PROOF_COLUMNS) +
                " FROM dispatch_proofs WHERE item_id=? ORDER BY proof_id", (item_id,)
            ).fetchall()
        ]

        events = []
        for r in conn.execute(
            "SELECT " + ",".join(_EVENT_COLUMNS) +
            " FROM item_events WHERE item_id=? ORDER BY event_id", (item_id,)
        ).fetchall():
            e = dict(zip(_EVENT_COLUMNS, r))
            e["metadata"] = json.loads(e["metadata"]) if e["metadata"] is not None else None
            events.append(e)

        dependencies = [
            dict(zip(_DEPENDENCY_COLUMNS, r))
            for r in conn.execute(
                "SELECT " + ",".join(_DEPENDENCY_COLUMNS) +
                " FROM work_item_dependencies WHERE item_id=? ORDER BY depends_on_id", (item_id,)
            ).fetchall()
        ]
        notes = [
            {"occurred_at": e["occurred_at"], "actor": e["actor"], "text": e["detail"]}
            for e in events if e["event_type"] == "operator-note"
        ]
        history = [
            {"revision": r["revision"], "status": r["status"], "occurred_at": r["occurred_at"]}
            for r in revisions
        ]

        items_out.append({
            "id": item_id,
            "title": item["title"],
            "phase": item["phase"],
            "lane": item["lane"],
            "env": item["env"],
            "brief_ref": item["brief_ref"],
            "status": tip["status"] if tip else None,
            "payload": tip["payload"] if tip else {},
            "created_at": item["created_at"],
            "created_actor": item["created_actor"],
            "dependencies": dependencies,
            "notes": notes,
            "history": history,
            "_canonical_v3": {
                "encoding": ENCODING,
                "identity_sha256": identity_sha256(item),
                "revisions": revisions,
                "completion": _completion_arm(tip) if tip else None,
                "proofs": proofs,
                "resolution": _resolution(revisions),
                "events": events,
                "legacy_unmapped": {},
            },
        })

    return {
        "schema": 2,
        "generation": generation,
        "source": "sqlite-v3",
        "source_watermark": compute_watermark(conn),
        "items": items_out,
    }


def import_legacy(projection: dict, applied_at: str | None = None, actor: str = "import-legacy") -> sqlite3.Connection:
    """Rebuild a fresh in-memory schema-3 database from a projection's
    `_canonical_v3` extension. Raw inserts on a bare connection, in FK-safe
    order, with foreign_keys enforcement deferred until commit (the schema
    already declares the successor back-reference DEFERRABLE)."""
    applied_at = applied_at or now_rfc3339()
    conn = sqlite3.connect(":memory:")
    migrate.apply_schema(conn, applied_at, actor)
    conn.execute("PRAGMA defer_foreign_keys=ON")
    # This bare connection has no op_*()/actor_context() functions (those are
    # registered only by db.connect() for a live service), and the rows being
    # replayed already carry their own historical item_events rows in the
    # projection -- so the *_contract (BEFORE) triggers, which gate on a live
    # operation context, and the *_event (AFTER) triggers, which would
    # otherwise synthesize a SECOND, duplicate event row, are suspended for
    # this offline, literal replay and recreated verbatim afterwards.
    expected_triggers = _trigger_sql(conn)
    for trigger in _REPLAY_SUSPENDED_TRIGGERS:
        conn.execute(f"DROP TRIGGER {trigger}")

    for item in projection["items"]:
        conn.execute(
            "INSERT INTO work_items(id,title,phase,lane,env,brief_ref,created_at,created_actor) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (item["id"], item["title"], item["phase"], item["lane"], item["env"],
             item["brief_ref"], item["created_at"], item["created_actor"]),
        )
    for item in projection["items"]:
        for dep in item["_canonical_v3"].get("dependencies", item.get("dependencies", [])):
            conn.execute(
                "INSERT INTO work_item_dependencies(item_id,depends_on_id,created_at,created_actor,"
                "operation_nonce) VALUES (?,?,?,?,?)",
                (dep["item_id"], dep["depends_on_id"], dep["created_at"],
                 dep["created_actor"], dep["operation_nonce"]),
            )
    for item in projection["items"]:
        for p in item["_canonical_v3"]["proofs"]:
            conn.execute(
                "INSERT INTO dispatch_proofs(" + ",".join(_PROOF_COLUMNS) + ") "
                "VALUES (" + ",".join("?" for _ in _PROOF_COLUMNS) + ")",
                tuple(p[c] for c in _PROOF_COLUMNS),
            )
    for item in projection["items"]:
        for r in item["_canonical_v3"]["revisions"]:
            payload_text = canonical_json(r["payload"]) if r["payload"] is not None else "{}"
            values = dict(r)
            values["payload"] = payload_text
            conn.execute(
                "INSERT INTO item_revisions(" + ",".join(_REVISION_COLUMNS) + ") "
                "VALUES (" + ",".join("?" for _ in _REVISION_COLUMNS) + ")",
                tuple(values[c] for c in _REVISION_COLUMNS),
            )
    for item in projection["items"]:
        for e in item["_canonical_v3"]["events"]:
            metadata_text = canonical_json(e["metadata"]) if e["metadata"] is not None else "{}"
            conn.execute(
                "INSERT INTO item_events(event_id,item_id,revision,occurred_at,actor,event_type,"
                "detail,metadata) VALUES (?,?,?,?,?,?,?,?)",
                (e["event_id"], e["item_id"], e["revision"], e["occurred_at"], e["actor"],
                 e["event_type"], e["detail"], metadata_text),
            )
    for trigger in _REPLAY_SUSPENDED_TRIGGERS:
        conn.execute(expected_triggers[trigger])
    actual_triggers = _trigger_sql(conn)
    if actual_triggers != expected_triggers:
        missing = sorted(set(expected_triggers) - set(actual_triggers))
        changed = sorted(n for n in expected_triggers
                         if n in actual_triggers and actual_triggers[n] != expected_triggers[n])
        conn.rollback()
        conn.close()
        raise ImportIntegrityError(f"trigger set mismatch: missing={missing} changed={changed}")
    conn.commit()
    return conn


def atomic_backup(conn: sqlite3.Connection, db_path: str) -> None:
    """Write a SQLite backup of `conn` to `db_path` durably and atomically:
    back up to a sibling temp file, fsync it, os.replace() it into place,
    then fsync the directory. A reader never observes a partial file."""
    tmp = db_path + ".tmp"
    for ext in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(tmp + ext):
            os.remove(tmp + ext)
    disk = sqlite3.connect(tmp)
    try:
        conn.backup(disk)
    finally:
        disk.close()
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, db_path)
    dir_fd = os.open(os.path.dirname(os.path.abspath(db_path)) or ".", os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def import_legacy_to_file(projection: dict, db_path: str, **kwargs) -> None:
    mem = import_legacy(projection, **kwargs)
    try:
        atomic_backup(mem, db_path)
    finally:
        mem.close()


def _diffs(left: dict, right: dict) -> list[dict]:
    """Field-by-field comparison per brief.md 6.1: identity; tip
    status/payload; every revision column; completion arm; every
    dependency tuple and nonce; every proof column; every event column;
    resolution fields; counts by table/event/status; and watermark.
    Returns a list of {"kind": "missing"|"mismatch", "path": str} diffs --
    "missing" means a row/field/count present on the left is absent on the
    right (REFUSED_LOSSY); "mismatch" means both sides have it but the
    normalized value/hash differs (REFUSED_PARITY)."""
    diffs = []
    left_items = {i["id"]: i for i in left["items"]}
    right_items = {i["id"]: i for i in right["items"]}

    for item_id in left_items:
        if item_id not in right_items:
            diffs.append({"kind": "missing", "path": f"items[{item_id}]"})
            continue
        li, ri = left_items[item_id], right_items[item_id]

        for field in ["id", "title", "phase", "lane", "env", "brief_ref",
                      "created_at", "created_actor", "status", "payload"]:
            if field not in ri:
                diffs.append({"kind": "missing", "path": f"items[{item_id}].{field}"})
            elif canonical_json(li[field]) != canonical_json(ri[field]):
                diffs.append({"kind": "mismatch", "path": f"items[{item_id}].{field}"})

        lv, rv = li["_canonical_v3"], ri["_canonical_v3"]
        if lv["identity_sha256"] != rv["identity_sha256"]:
            diffs.append({"kind": "mismatch", "path": f"items[{item_id}].identity_sha256"})
        if canonical_json(lv["completion"]) != canonical_json(rv["completion"]):
            diffs.append({"kind": "mismatch", "path": f"items[{item_id}].completion"})
        if canonical_json(lv["resolution"]) != canonical_json(rv["resolution"]):
            diffs.append({"kind": "mismatch", "path": f"items[{item_id}].resolution"})

        for group, key in [("revisions", "revision"), ("proofs", "proof_id"),
                            ("events", "event_id"), ("dependencies", "depends_on_id")]:
            lseq = lv.get(group, li.get(group)) if group == "dependencies" else lv[group]
            rseq = rv.get(group, ri.get(group)) if group == "dependencies" else rv[group]
            if len(lseq) != len(rseq):
                diffs.append({"kind": "missing", "path": f"items[{item_id}].{group} (count {len(lseq)} vs {len(rseq)})"})
                continue
            rbykey = {str(r[key]): r for r in rseq}
            for lrow in lseq:
                rrow = rbykey.get(str(lrow[key]))
                if rrow is None:
                    diffs.append({"kind": "missing", "path": f"items[{item_id}].{group}[{lrow[key]}]"})
                elif canonical_json(lrow) != canonical_json(rrow):
                    diffs.append({"kind": "mismatch", "path": f"items[{item_id}].{group}[{lrow[key]}]"})

    for item_id in right_items:
        if item_id not in left_items:
            diffs.append({"kind": "missing", "path": f"items[{item_id}] (unexpected on right)"})

    if canonical_json(left["source_watermark"]) != canonical_json(right["source_watermark"]):
        diffs.append({"kind": "mismatch", "path": "source_watermark"})

    return diffs


def compare_canonical(left_conn: sqlite3.Connection, right_conn: sqlite3.Connection) -> list[dict]:
    left = export_legacy(left_conn)
    right = export_legacy(right_conn)
    return _diffs(left, right)


def compare_projection_to_conn(left_projection: dict, right_conn: sqlite3.Connection) -> list[dict]:
    right = export_legacy(right_conn)
    return _diffs(left_projection, right)
