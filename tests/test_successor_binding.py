"""Acceptance criterion A: successor binding. Stable test IDs T-SUCC-01..08
plus both exception-arm bypass negatives. Every negative test attempts the
exact raw authorized-insert shape a buggy/malicious 'complete' caller could
try (the same path api.complete() uses), and must observe:
  (1) OperationError raised (not a silent success),
  (2) no predecessor 'done' row/event committed,
  (3) the ledger counts unchanged.
"""
import sqlite3
import unittest

from tests.helpers import StoreTestCase
from work_store import api, db
from work_store.db import OperationError
from work_store.util import now_rfc3339


def _counts(conn):
    cur = conn.cursor()
    return (
        cur.execute("SELECT count(*) FROM item_revisions").fetchone()[0],
        cur.execute("SELECT count(*) FROM item_events").fetchone()[0],
        cur.execute("SELECT count(*) FROM work_items").fetchone()[0],
    )


class SuccessorBindingTests(StoreTestCase):
    def _gated_predecessor(self):
        item = self.make_item(phase="design-accepted")
        self.claim_item(item, expected_revision=1)
        return item  # now at revision 2, status active

    def _attempt_bad_completion(self, predecessor, successor_id, successor_phase="implementation-build",
                                 successor_nonce_mismatch=False, owner_item_override=None,
                                 owner_revision_override=None):
        """Mirrors api.complete()'s successor arm exactly, but lets the test
        inject a malicious/stale successor reference instead of the fresh
        UUID api.complete() would generate itself."""
        now = now_rfc3339()
        with self.assertRaises(OperationError):
            try:
                with db.operation(self.conn, "complete", predecessor, "tester",
                                   prior_revision=2, successor_id=successor_id) as ctx:
                    self.conn.execute(
                        "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                        "occurred_at,actor,operation_nonce,completion_successor_id,"
                        "completion_successor_phase) VALUES (?,3,2,'done','{}',?,?,?,?,?)",
                        (predecessor, now, "tester", ctx.nonce, successor_id, successor_phase),
                    )
            except sqlite3.Error as e:
                raise OperationError(str(e)) from e

    def test_tsucc01_terminal_successor(self):
        predecessor = self._gated_predecessor()
        successor = self.make_item(phase="implementation-build")
        api.transition(self.conn, successor, 1, "superseded", None, "tester")
        before = _counts(self.conn)
        self._attempt_bad_completion(predecessor, successor)
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc02_cancelled_successor(self):
        """The original discovered defect: a design-accepted completion
        pointing to a previously cancelled build."""
        predecessor = self._gated_predecessor()
        successor = self.make_item(phase="implementation-build")
        api.transition(self.conn, successor, 1, "cancelled", None, "tester")
        before = _counts(self.conn)
        self._attempt_bad_completion(predecessor, successor)
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc03_active_successor(self):
        predecessor = self._gated_predecessor()
        successor = self.make_item(phase="implementation-build")
        self.claim_item(successor, expected_revision=1)
        before = _counts(self.conn)
        self._attempt_bad_completion(predecessor, successor)
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc04_done_successor(self):
        predecessor = self._gated_predecessor()
        successor = self.make_item(phase="merge-or-PR")
        self.claim_item(successor, expected_revision=1)
        api.complete(self.conn, successor, 2, {"exception_kind": "final-phase", "reason": "x"}, "tester")
        before = _counts(self.conn)
        self._attempt_bad_completion(predecessor, successor)
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc05_revisionless_successor(self):
        predecessor = self._gated_predecessor()
        successor = f"ghost-{predecessor}"
        before = _counts(self.conn)
        with self.assertRaises(OperationError):
            try:
                with db.operation(self.conn, "complete", predecessor, "tester",
                                   prior_revision=2, successor_id=successor) as ctx:
                    self.conn.execute(
                        "INSERT INTO work_items(id,title,phase,lane,env,brief_ref,created_at,created_actor) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (successor, "ghost", "implementation-build", "operator", "local", None,
                         now_rfc3339(), "tester"),
                    )
                    self.conn.execute(
                        "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                        "occurred_at,actor,operation_nonce,completion_successor_id,"
                        "completion_successor_phase) VALUES (?,3,2,'done','{}',?,?,?,?,?)",
                        (predecessor, now_rfc3339(), "tester", ctx.nonce, successor, "implementation-build"),
                    )
            except sqlite3.Error as e:
                raise OperationError(str(e)) from e
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc06_preexisting_queued_successor(self):
        """A successor that already existed (own prior transaction, own
        nonce) cannot be adopted later -- its successor_owner_nonce can
        never match the current operation's nonce."""
        predecessor = self._gated_predecessor()
        preexisting = self.make_item(phase="implementation-build")  # created & committed earlier
        before = _counts(self.conn)
        self._attempt_bad_completion(predecessor, preexisting)
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc06_api_cannot_even_express_preexisting_successor(self):
        """api.complete() has no parameter through which a caller can name
        an existing successor id at all -- the service always mints a
        fresh uuid itself."""
        import inspect
        sig = inspect.signature(api.complete)
        self.assertNotIn("successor_id", sig.parameters)
        self.assertNotIn("successor_item_id", sig.parameters)

    def test_tsucc07_shared_successor_identity(self):
        predecessor1 = self._gated_predecessor()
        predecessor2 = self._gated_predecessor()
        done1 = api.complete(
            self.conn, predecessor1, 2,
            {"successor_title": "s1", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        shared_successor = done1["successor_id"]
        before = _counts(self.conn)
        self._attempt_bad_completion(predecessor2, shared_successor)
        self.assertEqual(_counts(self.conn), before)

    def test_tsucc08_clean_atomic_child_commits(self):
        predecessor = self._gated_predecessor()
        done = api.complete(
            self.conn, predecessor, 2,
            {"successor_title": "clean child", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        self.assertIsNotNone(done["successor_id"])
        snap = api.snapshot(self.conn)
        succ_rows = [r for r in snap["revisions"] if r["item_id"] == done["successor_id"]]
        self.assertEqual(len(succ_rows), 1)
        self.assertEqual(succ_rows[0]["status"], "queued")

    # ---- exception-arm bypasses --------------------------------------------

    def test_final_phase_bypass_on_gated_phase_rejected(self):
        """final-phase must only be usable on actually-final phases; using
        it to skip a REQUIRED gate must be rejected mechanically."""
        predecessor = self._gated_predecessor()  # design-accepted, gated
        before = _counts(self.conn)
        with self.assertRaises(OperationError) as cm:
            api.complete(self.conn, predecessor, 2,
                          {"exception_kind": "final-phase", "reason": "skip the gate"}, "tester")
        self.assertIn("exception-not-actually-final-phase", str(cm.exception))
        self.assertEqual(_counts(self.conn), before)

    def test_owner_decision_bypass_without_capability_rejected(self):
        predecessor = self._gated_predecessor()
        before = _counts(self.conn)
        with self.assertRaises(OperationError) as cm:
            api.complete(self.conn, predecessor, 2,
                          {"exception_kind": "owner-decision", "reason": "waived",
                           "decision_date": now_rfc3339(), "decision_ref": "FORGED-REF"},
                          "tester")  # no owner_capability/issuer supplied
        self.assertIn("owner-decision-capability-required", str(cm.exception))
        self.assertEqual(_counts(self.conn), before)

    def test_owner_decision_bypass_with_wrong_capability_scope_rejected(self):
        predecessor = self._gated_predecessor()
        # Capability issued for a DIFFERENT decision_ref than what's used.
        token = self.issuer.issue(predecessor, 2, "REAL-REF", operation="complete")
        before = _counts(self.conn)
        with self.assertRaises(OperationError):
            api.complete(self.conn, predecessor, 2,
                         {"exception_kind": "owner-decision", "reason": "waived",
                          "decision_date": now_rfc3339(), "decision_ref": "DIFFERENT-REF"},
                         "tester", owner_capability=token, issuer=self.issuer)
        self.assertEqual(_counts(self.conn), before)


if __name__ == "__main__":
    unittest.main()
