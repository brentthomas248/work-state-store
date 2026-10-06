import unittest

from tests.helpers import StoreTestCase
from work_store import api
from work_store.db import OperationError


class HappyPathTests(StoreTestCase):
    def test_create_claim_block_resolve_complete_with_successor(self):
        item = self.make_item(phase="design-accepted")
        snap = api.snapshot(self.conn)
        self.assertEqual(len(snap["items"]), 1)
        self.assertEqual(len(snap["revisions"]), 1)

        claimed = self.claim_item(item, expected_revision=1)
        self.assertEqual(claimed["revision"], 2)

        blocked = api.request_block(self.conn, item, 2, "DEC-1", "need owner call", "tester")
        self.assertEqual(blocked["revision"], 3)

        token = self.issuer.issue(item, 3, "DEC-1")
        resolved = api.resolve_block(
            self.conn, item, 3, "DEC-1",
            {"new_status": "queued", "text": "approved", "actor": "owner"},
            token,
        )
        self.assertEqual(resolved["revision"], 4)

        claimed2 = self.claim_item(item, expected_revision=4)
        self.assertEqual(claimed2["revision"], 5)

        done = api.complete(
            self.conn, item, 5,
            {"successor_title": "build it", "successor_lane": "claude-code",
             "successor_env": "local"},
            "tester",
        )
        self.assertEqual(done["revision"], 6)
        self.assertIsNotNone(done["successor_id"])

        snap = api.snapshot(self.conn)
        successor_rows = [r for r in snap["revisions"] if r["item_id"] == done["successor_id"]]
        self.assertEqual(len(successor_rows), 1)
        self.assertEqual(successor_rows[0]["status"], "queued")

    def test_complete_final_phase_exception(self):
        item = self.make_item(phase="merge-or-PR")
        self.claim_item(item, expected_revision=1)
        done = api.complete(self.conn, item, 2, {"exception_kind": "final-phase", "reason": "nothing follows merge"}, "tester")
        self.assertEqual(done["successor_id"], None)

    def test_complete_owner_decision_exception(self):
        item = self.make_item(phase="design-accepted")
        self.claim_item(item, expected_revision=1)
        token = self.issuer.issue(item, 2, "OWNER-DEC-1", operation="complete")
        done = api.complete(
            self.conn, item, 2,
            {"exception_kind": "owner-decision", "reason": "owner waived the build gate",
             "decision_date": "2026-10-05T00:00:00.000000Z", "decision_ref": "OWNER-DEC-1"},
            "tester", owner_capability=token,
        )
        self.assertEqual(done["successor_id"], None)

    def test_add_dependency_and_eligibility(self):
        a = self.make_item()
        b = self.make_item()
        api.add_dependency(self.conn, a, b, "tester")
        from work_store.heartbeat import eligible_queued
        eligible_ids = {r["id"] for r in eligible_queued(self.conn)}
        self.assertIn(b, eligible_ids)
        self.assertNotIn(a, eligible_ids)

    def test_add_note(self):
        item = self.make_item()
        api.add_note(self.conn, item, 1, "just a note", "tester")
        snap = api.snapshot(self.conn)
        notes = [e for e in snap["events"] if e["event_type"] == "operator-note"]
        self.assertEqual(len(notes), 1)

    def test_append_proof_observation_chain(self):
        item = self.make_item()
        claimed = self.claim_item(item, expected_revision=1)
        appended = api.append_proof_observation(
            self.conn, item, claimed["proof_id"], "dead", "tester",
        )
        self.assertIn("proof_id", appended)


if __name__ == "__main__":
    unittest.main()
