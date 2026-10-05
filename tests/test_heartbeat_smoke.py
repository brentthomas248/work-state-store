import unittest

from tests.helpers import StoreTestCase
from work_store import api, invariants
from work_store.heartbeat import compute_quiet
from work_store.util import now_rfc3339


class HeartbeatSmokeTests(StoreTestCase):
    def test_quiet_on_empty_store(self):
        result = compute_quiet(self.conn, now_rfc3339())
        self.assertTrue(result.quiet, result.diagnostics)
        self.assertEqual(result.exit_code, 0)

    def test_quiet_after_full_lifecycle(self):
        item = self.make_item(phase="merge-or-PR")
        self.claim_item(item, expected_revision=1)
        api.complete(self.conn, item, 2, {"exception_kind": "final-phase", "reason": "terminal"}, "tester")
        result = compute_quiet(self.conn, now_rfc3339())
        self.assertTrue(result.quiet, result.diagnostics)

    def test_p001_fingerprint_matches_fresh_schema(self):
        fp = invariants.artifact_fingerprint(self.conn)
        result = compute_quiet(self.conn, now_rfc3339(), expected_fingerprint=fp)
        self.assertTrue(result.quiet, result.diagnostics)


if __name__ == "__main__":
    unittest.main()
