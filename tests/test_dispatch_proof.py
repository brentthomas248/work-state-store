"""Acceptance criterion C: dispatch proof. T-CLAIM-01 (revoked tip) and
T-CLAIM-02 (expired tip), attempted through every typed operation that
could conceivably create an 'active' revision, plus the raw authorized
insert path. Each raw attempt uses the REAL operation nonce (via the
callable form of raw_authorized_insert) so the failure asserted is
specifically 'claim-current-live-tip-required', not an incidental
nonce mismatch.

A proof's valid_until is checked against NEW.occurred_at, not wall-clock
time at test-execution time -- in the real API, occurred_at is always
server-minted at call time, so "time has passed beyond valid_until" is
simulated here by giving the exploit attempt's occurred_at a timestamp
later than the proof's valid_until, exactly as a later real call would
have a later server-minted occurred_at. This is what acceptance
criterion C means by "validated against server-minted now, never
caller timestamp": the trigger never trusts a caller-supplied notion of
"is this still valid", it recomputes it from valid_until vs occurred_at
on every single insert attempt.
"""
import datetime as _dt
import unittest

from tests.helpers import StoreTestCase
from work_store import api
from work_store.db import OperationError
from work_store.util import now_rfc3339, to_rfc3339


def _later(rfc3339_ts, seconds):
    dt = _dt.datetime.strptime(rfc3339_ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)
    return to_rfc3339(dt + _dt.timedelta(seconds=seconds))


def _active_insert_using_stale_root(item_id, new_revision, prior_revision, root_proof_id, occurred_at):
    def build(ctx):
        return [(
            "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
            "occurred_at,actor,operation_nonce,dispatch_root_id) VALUES (?,?,?,'active','{}',?,?,?,?)",
            (item_id, new_revision, prior_revision, occurred_at, "tester", ctx.nonce, root_proof_id),
        )]
    return build


class DispatchProofTests(StoreTestCase):
    def _claimed_with_tip_state(self, tip_state):
        item = self.make_item()
        claim_time = now_rfc3339()
        if tip_state == "revoked":
            claimed = self.claim_item(item, expected_revision=1, valid_until=_later(claim_time, 3600))
            api.append_proof_observation(
                self.conn, item, claimed["proof_id"], "revoked", "tester",
                observed_at=now_rfc3339(), valid_until=_later(claim_time, 3600),
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
            # occurred_at must be freshly minted AFTER `other` exists, so it
            # always satisfies "occurred-at-not-advanced" relative to
            # `other`'s own revision-1 -- the test must fail on the tip
            # check being exercised, not on an unrelated ordering artifact.
            exploit_occurred_at = (
                _later(far_future_valid_until, 3600) if far_future_valid_until else now_rfc3339()
            )
            with self.assertRaises(OperationError) as cm:
                self.raw_authorized_insert(
                    kind, other,
                    _active_insert_using_stale_root(other, 2, 1, root_proof_id, exploit_occurred_at),
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
                _active_insert_using_stale_root(other, 2, 1, first["proof_id"], now_rfc3339()),
                prior_revision=1,
            )
        self.assertIn("claim-current-live-tip-required", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
