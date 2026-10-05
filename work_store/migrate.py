"""Schema-3 migration runner.

Computes the migration checksum over the exact schema template text (LF
newlines, bind tokens unchanged), then applies the DDL and binds the final
three values (checksum, applied_at, actor) into the two seed INSERT
statements using real parameter binding (executescript cannot bind
parameters, so the template is split at the first seed-row boundary).
"""
from __future__ import annotations

import hashlib
import sqlite3

from .schema import SCHEMA_SQL_TEMPLATE

_SPLIT_MARKER = "\nINSERT INTO schema_migrations VALUES"

SCHEMA_VERSION = 3
MIGRATION_NAME = "immutable-ledger-v3.1"


def migration_checksum() -> str:
    """SHA-256 of the UTF-8 template text, bind tokens unchanged."""
    text = SCHEMA_SQL_TEMPLATE
    if "\r" in text:
        raise AssertionError("schema template must use LF newlines only")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ddl_and_seed_sql() -> tuple[str, str]:
    idx = SCHEMA_SQL_TEMPLATE.index(_SPLIT_MARKER)
    ddl = SCHEMA_SQL_TEMPLATE[:idx]
    seed = SCHEMA_SQL_TEMPLATE[idx + 1:]  # drop leading newline
    return ddl, seed


def apply_schema(conn: sqlite3.Connection, applied_at: str, migration_actor: str) -> str:
    """Apply the schema-3 DDL and seed rows to a fresh connection.

    Returns the migration checksum that was recorded.
    """
    checksum = migration_checksum()
    ddl_sql, seed_sql = _ddl_and_seed_sql()
    conn.executescript(ddl_sql)
    # seed_sql is exactly the two INSERT statements with named bind tokens.
    statements = [s.strip() for s in seed_sql.split(";") if s.strip()]
    if len(statements) != 2:
        raise AssertionError("expected exactly two seed INSERT statements")
    params = {
        "migration_sha256": checksum,
        "applied_at": applied_at,
        "migration_actor": migration_actor,
    }
    for stmt in statements:
        conn.execute(stmt, params)
    conn.commit()
    return checksum
