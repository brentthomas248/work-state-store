"""Acceptance criterion C: dispatch proof. T-CLAIM-01 (revoked tip) and
T-CLAIM-02 (expired tip), attempted through every typed operation that
could conceivably create an 'active' revision, plus the raw authorized
insert path. Each raw attempt uses the REAL operation nonce (via the
callable form of raw_authorized_insert) so the failure asserted is
specifically 'claim-current-live-tip-required', not an incidental
nonce mismatch.

A proof's valid_until is checked (by trigger) against op_now(): the
server-minted transaction time sampled after BEGIN IMMEDIATE acquires the
write lock. Every inserted occurred_at/recorded_at must EQUAL op_now(), so
no caller -- typed API or raw service-authorized SQL -- can supply the
time an expiry is judged against. "Time has passed beyond valid_until"
is therefore simulated by advancing the service clock (db._server_now),
the only clock the store consults; DefectOneProbeTests mirror the
reviewer's backdated-claim probe.
"""
import datetime as _dt
import unittest
from unittest import mock

from tests.helpers import StoreTestCase
from work_store import api, db
from work_store.db import OperationError
from work_store.util import now_rfc3339, to_rfc3339


def _later(rfc3339_ts, seconds):
    dt = _dt.datetime.strptime(rfc3339_ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)
    return to_rfc3339(dt + _dt.timedelta(seconds=seconds))


def _active_insert_using_stale_root(item_id, new_revision, prior_revision, root_proof_id,
                                    occurred_at=None):
    """occurred_at defaults to the operation's server-minted ctx.now."""
    def build(ctx):
        return [(
            "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
            "occurred_at,actor,operation_nonce,dispatch_root_id) VALUES (?,?,?,'active','{}',?,?,?,?)",
            (item_id, new_revision, prior_revision, occurred_at or ctx.now, "tester", ctx.nonce,
             root_proof_id),
        )]
    return build


def _clock(ts):
    return mock.patch.object(db, "_server_now", lambda: ts)


class DispatchProofTests(StoreTestCase):
    def _claimed_with_tip_state(self, tip_state):
        item = self.make_item()
        claim_time = now_rfc3339()
        if tip_state == "revoked":
            claimed = self.claim_item(item, expected_revision=1, valid_until=_later(claim_time, 3600))
            api.append_proof_observation(
                self.conn, item, claimed["proof_id"], "revoked", "tester",
                valid_until=_later(claim_time, 3600),
            )
            far_future_valid_until = None
        elif tip_state == "expired":
            valid_until = _later(claim_time, 2)
            claimed = self.claim_item(item, expected_revision=1, valid_until=valid_until)
            far_future_valid_until = valid_until
        else:
            raise AssertionError(tip_state)
        return item, claimed, far_future_valid_until

    def _assert_cannot_create_active(self, tip_state):
        item, claimed, far_future_valid_until = self._claimed_with_tip_state(tip_state)
        root_proof_id = claimed["proof_id"]

        # transition() can never mint status='active' regardless of tip.
        fresh = self.make_item()
        with self.assertRaises(OperationError) as cm:
            api.transition(self.conn, fresh, 1, "active", None, "tester")
        self.assertIn("claim-current-live-tip-required", str(cm.exception))

        for kind, ctx_kwargs in [
            ("claim", {"prior_revision": 1}),
            ("request_block", {"prior_revision": 1}),
            ("resolve_block", {"prior_revision": 1, "decision_ref": "X"}),
            ("complete", {"prior_revision": 1, "successor_id": "irrelevant"}),
        ]:
            other = self.make_item()
            # For the expired case the service clock is advanced past the
            # proof's valid_until; the exploit insert's occurred_at is the
            # server-minted ctx.now (later than `other`'s revision-1), so
            # the failure is the tip check, not an ordering artifact.
            server_time = (_later(far_future_valid_until, 3600) if far_future_valid_until
                           else now_rfc3339())
            with _clock(server_time), self.assertRaises(OperationError) as cm:
                self.raw_authorized_insert(
                    kind, other,
                    _active_insert_using_stale_root(other, 2, 1, root_proof_id),
                    **ctx_kwargs,
                )
            self.assertIn("claim-current-live-tip-required", str(cm.exception))

    def test_tclaim01_revoked_tip_rejected_everywhere(self):
        self._assert_cannot_create_active("revoked")

    def test_tclaim02_expired_tip_rejected_everywhere(self):
        self._assert_cannot_create_active("expired")

    def test_legitimate_claim_mints_fresh_root_and_succeeds(self):
        item = self.make_item()
        result = self.claim_item(item, expected_revision=1)
        self.assertEqual(result["revision"], 2)

    def test_claim_cannot_reuse_a_committed_root_from_an_earlier_transaction(self):
        """The nonce check ensures a root minted in an EARLIER committed
        transaction can never be claimed as if minted 'now', even while
        still live and unexpired, because its operation_nonce no longer
        matches any current operation context."""
        item = self.make_item()
        first = self.claim_item(item, expected_revision=1)
        other = self.make_item()
        with self.assertRaises(OperationError) as cm:
            self.raw_authorized_insert(
                "claim", other,
                _active_insert_using_stale_root(other, 2, 1, first["proof_id"]),
                prior_revision=1,
            )
        self.assertIn("claim-current-live-tip-required", str(cm.exception))


class DefectOneProbeTests(StoreTestCase):
    """Reviewer probe (C): item created at 2000-01-01, claimed with caller
    time 2000-01-02 and proof valid_until=2000-01-03. The proof is expired
    relative to the server's now, so the claim must be rejected no matter
    what time the caller asserts."""

    PROOF = {"proof_kind": "worker-pid", "proof_value": "pid-x", "host_id": "h",
             "valid_until": "2000-01-03T00:00:00.000000Z"}

    def _item_created_2000(self):
        with _clock("2000-01-01T00:00:00.000000Z"):
            return self.make_item()

    def _assert_unclaimed(self, item):
        rows = self.conn.execute("SELECT revision,status FROM item_revisions WHERE item_id=?",
                                 (item,)).fetchall()
        self.assertEqual(rows, [(1, "queued")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM dispatch_proofs WHERE item_id=?",
                                           (item,)).fetchone()[0], 0)

    def test_probe_backdated_claim_via_api_rejected(self):
        item = self._item_created_2000()
        # Caller time cannot be passed at all ...
        with self.assertRaises(TypeError):
            api.claim(self.conn, item, 1, dict(self.PROOF), "tester",
                      occurred_at="2000-01-02T00:00:00.000000Z")
        # ... nor smuggled through the proof object ...
        for key in ("occurred_at", "recorded_at", "observed_at"):
            with self.assertRaises(OperationError) as cm:
                api.claim(self.conn, item, 1,
                          dict(self.PROOF, **{key: "2000-01-02T00:00:00.000000Z"}), "tester")
            self.assertIn("caller-supplied-time-forbidden", str(cm.exception))
        # ... and the expired proof itself is refused against server now
        # (the proof row's observed_at IS server now, so the row CHECK
        # observed_at<=valid_until refuses it before the claim trigger).
        with self.assertRaises(OperationError) as cm:
            api.claim(self.conn, item, 1, dict(self.PROOF), "tester")
        self.assertIn("observed_at<=valid_until", str(cm.exception))
        self._assert_unclaimed(item)

    def test_probe_backdated_claim_via_raw_authorized_sql_rejected(self):
        """The same probe on the raw service-authorized insert path: the
        caller writes its own backdated recorded_at/occurred_at."""
        item = self._item_created_2000()
        backdated = "2000-01-02T00:00:00.000000Z"

        def build(ctx):
            return [
                ("INSERT INTO dispatch_proofs(proof_id,item_id,previous_proof_id,root_proof_id,"
                 "proof_kind,proof_value,host_id,issued_actor,recorded_at,observed_at,valid_until,"
                 "observation,observer_actor,operation_nonce) "
                 "VALUES (9001,?,NULL,9001,'worker-pid','pid-x','h','tester',?,?,?,'live','tester',?)",
                 (item, backdated, backdated, self.PROOF["valid_until"], ctx.nonce)),
                ("INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                 "occurred_at,actor,operation_nonce,dispatch_root_id) "
                 "VALUES (?,2,1,'active','{}',?,'tester',?,9001)",
                 (item, backdated, ctx.nonce)),
            ]
        with self.assertRaises(OperationError) as cm:
            self.raw_authorized_insert("claim", item, build, prior_revision=1)
        self.assertIn("recorded-at-not-server-minted", str(cm.exception))
        self._assert_unclaimed(item)

    def test_raw_revision_with_caller_occurred_at_rejected(self):
        """A revision whose occurred_at is anything but op_now() is refused,
        even on an otherwise-legal arc (queued -> cancelled)."""
        item = self.make_item()
        future = "2999-01-01T00:00:00.000000Z"
        with self.assertRaises(OperationError) as cm:
            self.raw_authorized_insert("transition", item, lambda ctx: [(
                "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                "occurred_at,actor,operation_nonce) VALUES (?,2,1,'cancelled','{}',?,'tester',?)",
                (item, future, ctx.nonce))], prior_revision=1)
        self.assertIn("occurred-at-not-server-minted", str(cm.exception))

    def test_claim_rejected_once_server_clock_passes_valid_until(self):
        """Same proof, judged at two server times: admitted before expiry,
        refused after -- the expiry comparison uses only the server clock."""
        a = self._item_created_2000()
        b = self._item_created_2000()
        with _clock("2000-01-02T00:00:00.000000Z"):
            self.assertEqual(api.claim(self.conn, a, 1, dict(self.PROOF), "tester")["revision"], 2)
        with _clock("2000-01-03T00:00:00.000001Z"), self.assertRaises(OperationError) as cm:
            api.claim(self.conn, b, 1, dict(self.PROOF, proof_value="pid-y"), "tester")
        self.assertIn("observed_at<=valid_until", str(cm.exception))
        self._assert_unclaimed(b)

    def test_naive_valid_until_rejected(self):
        item = self.make_item()
        with self.assertRaises(OperationError) as cm:
            api.claim(self.conn, item, 1, dict(self.PROOF, valid_until="2999-01-01T00:00:00"),
                      "tester")
        self.assertIn("invalid-timestamp", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
