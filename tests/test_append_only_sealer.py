"""Acceptance criterion D: append-only everywhere. Every canonical table
must deny UPDATE and DELETE unconditionally (authorizer-level, independent
of any operation context), and INSERT must be denied both with no
operation context at all and with a context whose kind is not on that
table's allowlist. This is the sealer matrix: one test per canonical
table x {UPDATE, DELETE}, plus the INSERT-policy denials.
"""
import sqlite3
import unittest

from tests.helpers import StoreTestCase
from work_store import api, db
from work_store.db import CANONICAL_TABLES, OperationError
from work_store.util import now_rfc3339


# BEFORE UPDATE/DELETE triggers in SQLite fire only for rows that actually
# match the WHERE clause, so a probe with no matching row proves nothing
# (the statement is simply a no-op, not denied). The row-scoped tables
# below are seeded with a real row by _build_table_probes() before the
# WHERE-qualified statements target it; the four policy tables already
# carry seed rows from migration, so an unqualified statement suffices.
_UNQUALIFIED_PROBES = {
    "schema_version": (
        "UPDATE schema_version SET version=version",
        "DELETE FROM schema_version",
    ),
    "schema_migrations": (
        "UPDATE schema_migrations SET migration_actor='x'",
        "DELETE FROM schema_migrations",
    ),
    "status_transitions": (
        "UPDATE status_transitions SET requires_block_resolution=requires_block_resolution",
        "DELETE FROM status_transitions",
    ),
    "phase_gates": (
        "UPDATE phase_gates SET required=required",
        "DELETE FROM phase_gates",
    ),
}


class AppendOnlySealerTests(StoreTestCase):
    def _build_table_probes(self):
        """Seed one real, matchable row per row-scoped canonical table (via
        the normal authorized API) and return the full table -> (update_sql,
        delete_sql) probe matrix, covering all 9 canonical tables."""
        a = self.make_item()
        b = self.make_item()
        api.add_dependency(self.conn, b, a, "tester")  # both still rev-1/queued
        claimed = self.claim_item(a, expected_revision=1)  # seeds dispatch_proofs, item_revisions rev 2
        probes = dict(_UNQUALIFIED_PROBES)
        probes["work_items"] = (
            f"UPDATE work_items SET title='x' WHERE id='{a}'",
            f"DELETE FROM work_items WHERE id='{a}'",
        )
        probes["item_revisions"] = (
            f"UPDATE item_revisions SET status='x' WHERE item_id='{a}' AND revision=1",
            f"DELETE FROM item_revisions WHERE item_id='{a}' AND revision=1",
        )
        probes["dispatch_proofs"] = (
            f"UPDATE dispatch_proofs SET observation='x' WHERE proof_id='{claimed['proof_id']}'",
            f"DELETE FROM dispatch_proofs WHERE proof_id='{claimed['proof_id']}'",
        )
        probes["work_item_dependencies"] = (
            f"UPDATE work_item_dependencies SET depends_on_id='{a}' WHERE item_id='{b}' AND depends_on_id='{a}'",
            f"DELETE FROM work_item_dependencies WHERE item_id='{b}' AND depends_on_id='{a}'",
        )
        probes["item_events"] = (
            f"UPDATE item_events SET kind='x' WHERE item_id='{a}'",
            f"DELETE FROM item_events WHERE item_id='{a}'",
        )
        return probes

    def test_all_canonical_tables_covered_by_probe_matrix(self):
        self.assertEqual(set(self._build_table_probes().keys()), CANONICAL_TABLES)

    def test_update_denied_on_every_canonical_table(self):
        for table, (update_sql, _delete_sql) in self._build_table_probes().items():
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.Error):
                    self.conn.execute(update_sql)

    def test_delete_denied_on_every_canonical_table(self):
        for table, (_update_sql, delete_sql) in self._build_table_probes().items():
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.Error):
                    self.conn.execute(delete_sql)

    def test_update_denied_even_inside_a_live_operation_context(self):
        """A live, otherwise-valid operation context must not relax the
        UPDATE/DELETE ban -- the seal is unconditional, not merely the
        absence of an allowlist entry."""
        item = self.make_item()
        with self.assertRaises(OperationError):
            try:
                with db.operation(self.conn, "transition", item, "tester", prior_revision=1):
                    self.conn.execute("UPDATE work_items SET title='hijacked' WHERE id=?", (item,))
            except sqlite3.Error as e:
                raise OperationError(str(e)) from e

    def test_insert_denied_with_no_operation_context(self):
        """The authorizer's INSERT branch denies unconditionally when
        holder.current is None, before even consulting ALLOWED_INSERTS --
        so any canonical table is representative; these two are the
        cheapest to construct without foreign-key prerequisites."""
        probes = [
            (
                "work_items",
                "INSERT INTO work_items(id,title,phase,lane,env,created_at,created_actor) "
                "VALUES ('x','x','design-accepted','operator','local',?,?)",
                (now_rfc3339(), "tester"),
            ),
            (
                "item_events",
                "INSERT INTO item_events(item_id,occurred_at,actor,kind,detail) "
                "VALUES ('x',?,?,'y','{}')",
                (now_rfc3339(), "tester"),
            ),
        ]
        for table, sql, params in probes:
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.Error):
                    self.conn.execute(sql, params)

    def test_insert_denied_for_operation_kind_not_on_tables_allowlist(self):
        """add_note's context may only insert into item_events -- using that
        same live context to smuggle an item_revisions row must fail."""
        item = self.make_item()
        with self.assertRaises(OperationError):
            try:
                with db.operation(self.conn, "add_note", item, "tester") as ctx:
                    self.conn.execute(
                        "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                        "occurred_at,actor,operation_nonce) VALUES (?,2,1,'active','{}',?,?,?)",
                        (item, now_rfc3339(), "tester", ctx.nonce),
                    )
            except sqlite3.Error as e:
                raise OperationError(str(e)) from e

    def test_dependency_insertion_outside_revision_one_window_denied(self):
        """add_dependency is only admitted while BOTH items are still at
        their pristine revision 1 / status queued; once either side has
        moved on (claimed, blocked, etc.) the window is permanently
        closed -- this is enforced mechanically (dependency-window-closed),
        not just by apparent apparent unavailability of the API elsewhere."""
        a = self.make_item()
        b = self.make_item()
        self.claim_item(a, expected_revision=1)  # a now at revision 2
        with self.assertRaises(OperationError) as cm:
            api.add_dependency(self.conn, b, a, "tester")
        self.assertIn("dependency-window-closed", str(cm.exception))

    def test_triggers_deny_append_only_violations_even_without_python_authorizer(self):
        """The immutability boundary is not solely a Python-level authorizer
        convenience -- the SQL triggers themselves (installed by the schema,
        independent of db.connect()) refuse UPDATE/DELETE on every canonical
        table even on a bare sqlite3.connect() with no authorizer at all, as
        would be the case for an offline tool or a future migration step."""
        probes = self._build_table_probes()
        bare = sqlite3.connect(self.db_path)
        try:
            for table, (update_sql, delete_sql) in probes.items():
                with self.subTest(table=table):
                    with self.assertRaises(sqlite3.Error):
                        bare.execute(update_sql)
                    with self.assertRaises(sqlite3.Error):
                        bare.execute(delete_sql)
        finally:
            bare.close()

    def test_startup_authorizer_self_test(self):
        """A fresh connect() must itself refuse a trivial boundary probe --
        this is the smoke check an operator would run to confirm the
        authorizer actually installed, not merely that the Python object
        exists."""
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("DROP TABLE work_items")
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("ATTACH DATABASE ':memory:' AS evil")
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("PRAGMA journal_mode=DELETE")


if __name__ == "__main__":
    unittest.main()
