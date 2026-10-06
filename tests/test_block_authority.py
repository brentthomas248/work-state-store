"""Acceptance criterion B: block authority. T-BLOCK-01 plus the capability
unforgeability/rollback/replay matrix."""
import inspect
import unittest

from tests.helpers import StoreTestCase
from work_store import api, db
from work_store.capability import CapabilityError, OwnerCapabilityIssuer
from work_store.db import OperationError
from work_store.util import now_rfc3339


class BlockAuthorityTests(StoreTestCase):
    def _blocked_item(self, decision_ref="DEC-7"):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        api.request_block(self.conn, item, 2, decision_ref, "need a call", "tester")
        return item  # now revision 3, status blocked

    def test_tblock01_generic_transition_cannot_resolve_block(self):
        """Caller reads the exact decision reference (it's visible via
        snapshot) and supplies it through the generic transition path --
        must be rejected since transition() never grants resolve_block
        capability context."""
        item = self._blocked_item("DEC-7")
        snap = api.snapshot(self.conn)
        tip = [r for r in snap["revisions"] if r["item_id"] == item][-1]
        self.assertEqual(tip["block_decision_ref"], "DEC-7")

        with self.assertRaises(OperationError) as cm:
            api.transition(self.conn, item, 3, "queued", None, "tester")
        self.assertIn("block-resolution-capability-required", str(cm.exception))

    def test_generic_transition_cannot_forge_resolution_fields(self):
        """Even a transition that lands on a non-resolution-requiring arc
        must be rejected if it tries to smuggle resolution fields in (the
        trigger forbids resolution fields outside op_kind='resolve_block')."""
        item = self.make_item()
        with self.assertRaises(OperationError) as cm:
            try:
                with db.operation(self.conn, "transition", item, "tester", prior_revision=1) as ctx:
                    self.conn.execute(
                        "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                        "occurred_at,actor,operation_nonce,resolved_decision_ref,resolved_on,"
                        "resolution_text,resolution_actor) VALUES (?,2,1,'cancelled','{}',?,?,?,?,?,?,?)",
                        (item, now_rfc3339(), "tester", ctx.nonce, "FORGED", now_rfc3339(), "x", "attacker"),
                    )
            except Exception as e:
                raise OperationError(str(e)) from e
        self.assertIn("resolution-fields-forbidden", str(cm.exception))

    def test_capability_forged_signature_rejected(self):
        item = self._blocked_item("DEC-8")
        forged = self.issuer.issue(item, 3, "DEC-8")[:-4] + "0000"  # tamper with signature
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(forged, item, 3, "DEC-8")

    def test_capability_wrong_scope_rejected(self):
        item = self._blocked_item("DEC-9")
        token = self.issuer.issue(item, 3, "DEC-9")
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(token, item, 3, "WRONG-REF")
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(token, item, 99, "DEC-9")
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(token, "other-item", 3, "DEC-9")

    def test_capability_single_use_after_commit(self):
        item = self._blocked_item("DEC-10")
        token = self.issuer.issue(item, 3, "DEC-10")
        api.resolve_block(self.conn, item, 3, "DEC-10",
                           {"new_status": "queued", "text": "ok", "actor": "owner"},
                           token)
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(token, item, 3, "DEC-10")

    def test_capability_released_on_rollback_is_usable_again(self):
        """Rollback/replay semantics: a reservation that never commits must
        not permanently burn the capability. The token is valid for (item,
        revision 3, DEC-11), so reserve() succeeds; the transaction then
        fails INSIDE the database (resolution to 'done' is not a legal
        block-resolution arc) and rolls back, which must release it."""
        item = self._blocked_item("DEC-11")
        token = self.issuer.issue(item, 3, "DEC-11")
        calls = {"reserve": 0, "release": 0, "finalize": 0}
        real = {name: getattr(self.issuer, name) for name in calls}

        def spy(name):
            def wrapper(*a, **kw):
                calls[name] += 1
                return real[name](*a, **kw)
            return wrapper

        for name in calls:
            setattr(self.issuer, name, spy(name))
        try:
            with self.assertRaises(OperationError):
                api.resolve_block(self.conn, item, 3, "DEC-11",
                                   {"new_status": "done", "text": "ok", "actor": "owner"},
                                   token)
        finally:
            for name in calls:
                delattr(self.issuer, name)
        self.assertEqual(calls["reserve"], 1)       # a real reservation happened
        self.assertGreater(calls["release"], 0)     # and a real rollback released it
        self.assertEqual(calls["finalize"], 0)
        tip = self.conn.execute("SELECT MAX(revision) FROM item_revisions WHERE item_id=?",
                                (item,)).fetchone()[0]
        self.assertEqual(tip, 3)                     # nothing committed
        # Capability must still be usable since nothing committed.
        result = api.resolve_block(self.conn, item, 3, "DEC-11",
                                    {"new_status": "queued", "text": "ok", "actor": "owner"},
                                    token)
        self.assertEqual(result["revision"], 4)

    def test_caller_constructed_issuer_token_rejected_by_resolve_block(self):
        """Reviewer probe (B): a freshly constructed OwnerCapabilityIssuer()
        with a self-issued token must NOT authorize resolve_block -- the API
        verifies only against the service-owned issuer."""
        item = self._blocked_item("DEC-20")
        rogue = OwnerCapabilityIssuer()
        token = rogue.issue(item, 3, "DEC-20")
        with self.assertRaises(OperationError) as cm:
            api.resolve_block(self.conn, item, 3, "DEC-20",
                               {"new_status": "queued", "text": "ok", "actor": "owner"},
                               token)
        self.assertIn("capability", str(cm.exception))
        tip = self.conn.execute("SELECT status FROM item_revisions WHERE item_id=? "
                                "ORDER BY revision DESC LIMIT 1", (item,)).fetchone()[0]
        self.assertEqual(tip, "blocked")

    def test_caller_constructed_issuer_token_rejected_by_complete(self):
        """Reviewer probe (B) against complete()'s owner-decision arm."""
        item = self.make_item(phase="misc-phase")
        self.claim_item(item, expected_revision=1)
        rogue = OwnerCapabilityIssuer()
        token = rogue.issue(item, 2, "DEC-21", operation="complete")
        with self.assertRaises(OperationError) as cm:
            api.complete(self.conn, item, 2,
                         {"exception_kind": "owner-decision", "reason": "r",
                          "decision_date": now_rfc3339(), "decision_ref": "DEC-21"},
                         "tester", owner_capability=token)
        self.assertIn("capability", str(cm.exception))
        tip = self.conn.execute("SELECT status FROM item_revisions WHERE item_id=? "
                                "ORDER BY revision DESC LIMIT 1", (item,)).fetchone()[0]
        self.assertEqual(tip, "active")

    def test_api_takes_no_issuer_parameter(self):
        """The issuer is service-owned: no typed-API entry point accepts an
        issuer object from its caller."""
        for fn in (api.resolve_block, api.complete):
            self.assertNotIn("issuer", inspect.signature(fn).parameters, fn.__name__)
        with self.assertRaises(TypeError):
            api.resolve_block(self.conn, "x", 3, "D", {"new_status": "queued", "text": "t",
                              "actor": "o"}, "tok", OwnerCapabilityIssuer())

    def test_capability_cannot_be_reserved_twice_concurrently(self):
        item = self._blocked_item("DEC-12")
        token = self.issuer.issue(item, 3, "DEC-12")
        self.issuer.reserve(token, item, 3, "DEC-12")
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(token, item, 3, "DEC-12")

    def test_knowing_stored_decision_ref_text_cannot_forge_capability(self):
        """A caller who only knows the plaintext decision_ref (readable via
        snapshot) cannot construct a valid token without the issuer's
        secret -- confirms unforgeability, not just the API's refusal to
        accept a bare string."""
        item = self._blocked_item("DEC-13")
        attacker_guess = "resolve_block|%s|3|DEC-13|deadbeefdeadbeefdeadbeefdeadbeef|%s" % (
            item, "0" * 64)
        with self.assertRaises(CapabilityError):
            self.issuer.reserve(attacker_guess, item, 3, "DEC-13")


if __name__ == "__main__":
    unittest.main()
