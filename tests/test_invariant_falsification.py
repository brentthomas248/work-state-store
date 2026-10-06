"""Acceptance criterion E: computed silence / falsification matrix.
One test per stable diagnostic ID (I001-I017, P001-P003, D000), each
building a valid baseline through the normal authorized API, then
corrupting an offline COPY (never the live db) by dropping the specific
trigger(s) that would have refused the bad write, to prove the
invariant QUERY -- independent of the trigger that normally prevents it
-- detects the same defect. This is deliberate defense in depth: the
trigger stops it online, the invariant query catches it if it ever
got in anyway.

A few invariants (I004's "exactly one of successor/exception" clause,
I007's successor-shape clause, I009) are additionally, redundantly
enforced by a table CHECK constraint -- corrupted_copy() disables CHECK
enforcement too (PRAGMA ignore_check_constraints) so the corruption can
land at all; the CHECK is documented in BUILD-REPORT.md as a second,
independent layer for those specific cases.
"""
import unittest

from tests.helpers import StoreTestCase, corrupted_copy
from work_store import api, db
from work_store.invariants import (
    Diagnostic, InvariantQueryFailed, artifact_fingerprint, check_policy_tables,
    check_schema_artifact, invariant_violations,
)
from work_store.heartbeat import EXIT_DIAGNOSTICS, EXIT_QUERY_FAILED, compute_quiet
from work_store.util import now_rfc3339


def _ids(diags):
    return {d.diag_id for d in diags}


class InvariantFalsificationTests(StoreTestCase):
    def _corrupt(self, triggers):
        # WAL-mode writes may still be sitting in the -wal file; checkpoint
        # so the plain file copy corrupted_copy() takes actually contains
        # everything committed so far.
        db.checkpoint(self.db_path)
        dst = self.db_path + ".corrupt.db"
        conn = corrupted_copy(self.db_path, dst, triggers)
        self.addCleanup(conn.close)
        import os
        self.addCleanup(lambda: os.path.exists(dst) and os.remove(dst))
        return conn

    def _assert_detected(self, conn, expected_id, now=None):
        diags = invariant_violations(conn, now or now_rfc3339())
        ids = _ids(diags)
        self.assertIn(expected_id, ids, f"expected {expected_id} in {ids}")
        result = compute_quiet(conn, now or now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)
        self.assertIn(expected_id, _ids(result.diagnostics))
        return diags

    # ---- I001: revision chain broken (gap after deleting rev 1) -----------

    def test_i001_revision_chain_gap(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)  # revisions 1,2
        corrupt = self._corrupt(["revisions_no_delete"])
        corrupt.execute("DELETE FROM item_revisions WHERE item_id=? AND revision=1", (item,))
        corrupt.commit()
        self._assert_detected(corrupt, "I001")

    # ---- I002: work_item with no revision at all ---------------------------

    def test_i002_item_with_no_revision(self):
        item = self.make_item()
        corrupt = self._corrupt(["revisions_no_delete"])
        corrupt.execute("DELETE FROM item_revisions WHERE item_id=?", (item,))
        corrupt.commit()
        self._assert_detected(corrupt, "I002")

    # ---- I003: illegal status transition between consecutive revisions ----

    def test_i003_illegal_transition(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)  # revision 2 = active
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute("UPDATE item_revisions SET status='queued' WHERE item_id=? AND revision=2", (item,))
        corrupt.commit()
        self._assert_detected(corrupt, "I003")

    # ---- I004a: done row with BOTH successor and exception set ------------

    def test_i004a_done_both_successor_and_exception(self):
        item = self.make_item(phase="merge-or-PR")
        self.claim_item(item, expected_revision=1)
        api.complete(self.conn, item, 2, {"exception_kind": "final-phase", "reason": "terminal"}, "tester")
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute(
            "UPDATE item_revisions SET completion_successor_id='forged-successor',"
            "completion_successor_phase='implementation-build' WHERE item_id=? AND revision=3",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I004")

    # ---- I004b: final-phase exception used on a phase that IS gated -------

    def test_i004b_final_phase_exception_on_now_gated_phase(self):
        item = self.make_item(phase="merge-or-PR")  # not gated at completion time
        self.claim_item(item, expected_revision=1)
        api.complete(self.conn, item, 2, {"exception_kind": "final-phase", "reason": "terminal"}, "tester")
        corrupt = self._corrupt(["work_items_no_update"])
        corrupt.execute("UPDATE work_items SET phase='design-accepted' WHERE id=?", (item,))
        corrupt.commit()
        self._assert_detected(corrupt, "I004")

    # ---- I005: successor phase doesn't match the gate's successor_phase ---

    def test_i005_successor_phase_mismatch(self):
        predecessor = self.make_item(phase="design-accepted")
        self.claim_item(predecessor, expected_revision=1)
        done = api.complete(
            self.conn, predecessor, 2,
            {"successor_title": "child", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        corrupt = self._corrupt(["work_items_no_update"])
        corrupt.execute("UPDATE work_items SET phase='merge-or-PR' WHERE id=?", (done["successor_id"],))
        corrupt.commit()
        self._assert_detected(corrupt, "I005")

    # ---- I006: completed-with-successor but successor has no revision 1 --

    def test_i006_successor_missing_revision_one(self):
        predecessor = self.make_item(phase="design-accepted")
        self.claim_item(predecessor, expected_revision=1)
        done = api.complete(
            self.conn, predecessor, 2,
            {"successor_title": "child", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        corrupt = self._corrupt(["revisions_no_delete"])
        corrupt.execute(
            "DELETE FROM item_revisions WHERE item_id=? AND revision=1", (done["successor_id"],)
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I006")

    # ---- I007: successor's revision 1 isn't a pristine fresh queued row ---

    def test_i007_successor_not_fresh_queued(self):
        predecessor = self.make_item(phase="design-accepted")
        self.claim_item(predecessor, expected_revision=1)
        done = api.complete(
            self.conn, predecessor, 2,
            {"successor_title": "child", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute(
            "UPDATE item_revisions SET status='active' WHERE item_id=? AND revision=1",
            (done["successor_id"],),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I007")

    # ---- I008: successor ownership back-reference doesn't match -----------

    def test_i008_successor_ownership_mismatch(self):
        predecessor = self.make_item(phase="design-accepted")
        self.claim_item(predecessor, expected_revision=1)
        done = api.complete(
            self.conn, predecessor, 2,
            {"successor_title": "child", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute(
            "UPDATE item_revisions SET successor_owner_item_id='someone-else' "
            "WHERE item_id=? AND revision=1",
            (done["successor_id"],),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I008")

    # ---- I009: blocked tip missing decision ref/text (CHECK-redundant) -----

    def test_i009_blocked_missing_decision_fields(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        api.request_block(self.conn, item, 2, "DEC-1", "need a call", "tester")
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute(
            "UPDATE item_revisions SET block_decision_text=NULL WHERE item_id=? AND status='blocked'",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I009")

    # ---- I010: resolution doesn't echo the exact decision it resolved -----

    def test_i010_resolution_ref_mismatch(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        api.request_block(self.conn, item, 2, "DEC-2", "need a call", "tester")
        api.resolve_block(
            self.conn, item, 3, "DEC-2",
            {"new_status": "queued", "text": "resolved", "actor": "owner"},
            self.issuer.issue(item, 3, "DEC-2"),
        )
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute(
            "UPDATE item_revisions SET resolved_decision_ref='WRONG-REF' WHERE item_id=? AND revision=4",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I010")

    # ---- I011: proof root/chain pointer integrity --------------------------

    def test_i011_proof_root_self_reference_broken(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        corrupt = self._corrupt(["proofs_no_update"])
        corrupt.execute(
            "UPDATE dispatch_proofs SET root_proof_id=root_proof_id+999 "
            "WHERE item_id=? AND previous_proof_id IS NULL",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I011")

    # ---- I012: proof chain identity continuity -----------------------------

    def test_i012_proof_chain_identity_drift(self):
        item = self.make_item()
        claimed = self.claim_item(item, expected_revision=1)
        api.append_proof_observation(
            self.conn, item, claimed["proof_id"], "live", "tester",
            valid_until="2999-01-01T00:00:00.000000Z",
        )
        corrupt = self._corrupt(["proofs_no_update"])
        corrupt.execute(
            "UPDATE dispatch_proofs SET proof_value='drifted' "
            "WHERE item_id=? AND previous_proof_id IS NOT NULL",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I012")

    # ---- I013: proof row internal field validity ---------------------------

    def test_i013_proof_internal_field_invalid(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        corrupt = self._corrupt(["proofs_no_update"])
        corrupt.execute("UPDATE dispatch_proofs SET proof_value='   ' WHERE item_id=?", (item,))
        corrupt.commit()
        self._assert_detected(corrupt, "I013")

    # ---- I014: active tip's dispatch_root isn't actually live/current -----

    def test_i014_active_tip_root_not_live(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        corrupt = self._corrupt(["proofs_no_update"])
        corrupt.execute(
            "UPDATE dispatch_proofs SET observation='revoked' "
            "WHERE item_id=? AND previous_proof_id IS NULL",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I014")

    # ---- I015: revision missing its matching created/state-change event ---

    def test_i015_revision_missing_event(self):
        item = self.make_item()
        self.claim_item(item, expected_revision=1)
        corrupt = self._corrupt(["events_no_delete"])
        corrupt.execute(
            "DELETE FROM item_events WHERE item_id=? AND revision=2 AND event_type='state-change'",
            (item,),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I015")

    # ---- I016a: dependency row with no matching dependency-added event ----

    def test_i016a_dependency_without_event(self):
        a = self.make_item()
        b = self.make_item()
        corrupt = self._corrupt(["dependency_insert_contract", "dependency_insert_event"])
        corrupt.execute(
            "INSERT INTO work_item_dependencies(item_id,depends_on_id,created_at,created_actor,"
            "operation_nonce) VALUES (?,?,?,?,?)",
            (b, a, now_rfc3339(), "tester", "0" * 64),
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I016")

    # ---- I016b: dependency-added event with no matching dependency row ----

    def test_i016b_event_without_dependency(self):
        a = self.make_item()
        b = self.make_item()
        api.add_dependency(self.conn, b, a, "tester")
        corrupt = self._corrupt(["dependencies_no_delete"])
        corrupt.execute(
            "DELETE FROM work_item_dependencies WHERE item_id=? AND depends_on_id=?", (b, a)
        )
        corrupt.commit()
        self._assert_detected(corrupt, "I016")

    # ---- I017: dependency cycle, reachable through the normal API ---------

    def test_i017_dependency_cycle_via_normal_api(self):
        a = self.make_item()
        b = self.make_item()
        c = self.make_item()
        api.add_dependency(self.conn, a, b, "tester")
        api.add_dependency(self.conn, b, c, "tester")
        api.add_dependency(self.conn, c, a, "tester")
        diags = invariant_violations(self.conn, now_rfc3339())
        self.assertIn("I017", _ids(diags))
        result = compute_quiet(self.conn, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)

    # ---- P001: schema artifact drift ---------------------------------------

    def test_p001_schema_artifact_drift(self):
        fp = artifact_fingerprint(self.conn)
        corrupt = self._corrupt([])
        corrupt.execute("CREATE INDEX extra_unexpected_index ON work_items(lane)")
        corrupt.commit()
        diags = check_schema_artifact(corrupt, fp)
        self.assertEqual(_ids(diags), {"P001"})
        result = compute_quiet(corrupt, now_rfc3339(), expected_fingerprint=fp)
        self.assertFalse(result.quiet)
        self.assertIn("P001", _ids(result.diagnostics))

    # ---- P002: status_transitions policy table drift -----------------------

    def test_p002_status_transitions_drift(self):
        corrupt = self._corrupt([
            "status_transitions_no_insert", "status_transitions_no_update",
            "status_transitions_no_delete",
        ])
        corrupt.execute("DELETE FROM status_transitions WHERE from_status='queued' AND to_status='cancelled'")
        corrupt.commit()
        diags = check_policy_tables(corrupt)
        self.assertEqual(_ids(diags), {"P002"})
        result = compute_quiet(corrupt, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertIn("P002", _ids(result.diagnostics))

    # ---- P003: phase_gates policy table drift ------------------------------

    def test_p003_phase_gates_drift(self):
        corrupt = self._corrupt([
            "phase_gates_no_insert", "phase_gates_no_update", "phase_gates_no_delete",
        ])
        corrupt.execute("UPDATE phase_gates SET required=0 WHERE predecessor_phase='design-accepted'")
        corrupt.commit()
        diags = check_policy_tables(corrupt)
        self.assertEqual(_ids(diags), {"P003"})
        result = compute_quiet(corrupt, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertIn("P003", _ids(result.diagnostics))

    # ---- D000: invariant query failure is NEVER silent --------------------

    def test_d000_query_failure_never_silent(self):
        corrupt = self._corrupt([
            "phase_gates_no_insert", "phase_gates_no_update", "phase_gates_no_delete",
        ])
        corrupt.execute("DROP TABLE phase_gates")
        corrupt.commit()
        with self.assertRaises(InvariantQueryFailed):
            invariant_violations(corrupt, now_rfc3339())
        result = compute_quiet(corrupt, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_QUERY_FAILED)
        self.assertEqual(_ids(result.diagnostics), {"D000"})

    def test_d000_predicate_instability_without_diagnostics_also_not_silent(self):
        """If the two-pass four-field predicate changes between passes with
        no diagnostic explaining why, compute_quiet must still refuse to
        claim quiet -- it manufactures a D000 rather than ever reporting
        quiet=True on an unexplained state change."""
        from unittest import mock
        from work_store import heartbeat as hb

        item = self.make_item()
        calls = {"n": 0}
        real_four_field = hb.four_field_predicate

        def flaky(conn):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, 1, 0, 0)
            return (0, 0, 0, 0)

        with mock.patch.object(hb, "four_field_predicate", side_effect=flaky):
            result = hb.compute_quiet(self.conn, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertEqual(_ids(result.diagnostics), {"D000"})
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)

    # ---- end-to-end silence: the ORIGINAL discovered defect shape ---------

    def test_end_to_end_silence_tsucc02_shaped_corruption(self):
        """Mirrors the historical bug this whole design exists to prevent:
        a design-accepted completion whose successor got cancelled out from
        under it after the fact. Demonstrates quiet goes false end-to-end
        through the heartbeat entry point, not just the raw invariant list."""
        predecessor = self.make_item(phase="design-accepted")
        self.claim_item(predecessor, expected_revision=1)
        done = api.complete(
            self.conn, predecessor, 2,
            {"successor_title": "child", "successor_lane": "claude-code", "successor_env": "local"},
            "tester",
        )
        corrupt = self._corrupt(["revisions_no_update"])
        corrupt.execute(
            "UPDATE item_revisions SET status='cancelled' WHERE item_id=? AND revision=1",
            (done["successor_id"],),
        )
        corrupt.commit()
        result = compute_quiet(corrupt, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)
        self.assertIn("I007", _ids(result.diagnostics))

    def test_end_to_end_silence_dependency_deletion(self):
        a = self.make_item()
        b = self.make_item()
        api.add_dependency(self.conn, b, a, "tester")
        corrupt = self._corrupt(["dependencies_no_delete"])
        corrupt.execute(
            "DELETE FROM work_item_dependencies WHERE item_id=? AND depends_on_id=?", (b, a)
        )
        corrupt.commit()
        result = compute_quiet(corrupt, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)
        self.assertIn("I016", _ids(result.diagnostics))


if __name__ == "__main__":
    unittest.main()
