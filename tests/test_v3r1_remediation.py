"""v3.1 REJECT follow-through: one negative test per reviewer probe for
findings E (false silence), F (import trigger loss, non-atomic roundtrip.db,
canonical JSON) and A (forged owner-decision import, I008 NULL semantics,
I007 scoping). Findings C and B are covered in test_dispatch_proof.py
(DefectOneProbeTests) and test_block_authority.py respectively."""
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from tests.helpers import StoreTestCase, corrupted_copy
from work_store import api, cli, db, fixtures, heartbeat, legacy, migrate
from work_store.db import OperationError
from work_store.heartbeat import EXIT_DIAGNOSTICS, EXIT_QUERY_FAILED, EXIT_QUIET, compute_quiet
from work_store.invariants import artifact_fingerprint, invariant_violations, reference_fingerprint
from work_store.util import canonical_json, canonical_timestamp, now_rfc3339

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_DIR = os.path.join(_REPO_ROOT, "tests", "_tmp")

_SUCCESSOR = {"successor_title": "child", "successor_lane": "claude-code", "successor_env": "local"}


def _ids(diags):
    return sorted({d.diag_id for d in diags})


def _triggers(conn):
    return dict(conn.execute("SELECT name, sql FROM sqlite_schema WHERE type='trigger'").fetchall())


def _raw(path):
    return sqlite3.connect(path)


# ----------------------------------------------------------------- E: silence

class HeartbeatSilenceTests(StoreTestCase):
    def _heartbeat(self):
        conn = _raw(self.db_path)
        try:
            return compute_quiet(conn, now_rfc3339())
        finally:
            conn.close()

    def test_empty_store_is_quiet(self):
        result = self._heartbeat()
        self.assertEqual((result.quiet, result.exit_code, result.diagnostics, result.four_field),
                         (True, EXIT_QUIET, [], (0, 0, 0, 0)))

    def test_probe_queued_work_is_not_quiet(self):
        """Reviewer probe QUEUED_WORK (0,1,0,0): no diagnostics, but one
        eligible queued item -- must NOT report silence."""
        self.make_item()
        result = self._heartbeat()
        self.assertEqual(result.four_field, (0, 1, 0, 0))
        self.assertEqual(result.diagnostics, [])
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)

    def test_probe_active_work_is_not_quiet(self):
        """Reviewer probe ACTIVE_WORK (1,0,0,0)."""
        item = self.make_item()
        self.claim_item(item)
        result = self._heartbeat()
        self.assertEqual(result.four_field, (1, 0, 0, 0))
        self.assertEqual(result.diagnostics, [])
        self.assertFalse(result.quiet)
        self.assertEqual(result.exit_code, EXIT_DIAGNOSTICS)

    def test_blocked_and_pollable_external_are_not_quiet(self):
        item = self.make_item()
        self.claim_item(item)
        api.request_block(self.conn, item, 2, "DEC-B", "need a call", "tester")
        ext = self.make_item()
        api.claim(self.conn, ext, 1, {"proof_kind": "codex-task-url", "proof_value": "https://x/2",
                                      "valid_until": "2999-01-01T00:00:00.000000Z"}, "tester")
        result = self._heartbeat()
        self.assertEqual(result.four_field, (1, 0, 1, 1))
        self.assertFalse(result.quiet)

    def test_p001_runs_without_a_pinned_fingerprint(self):
        """P001 is mandatory: a schema-artifact drift is diagnosed even when
        the caller supplies no expected_fingerprint."""
        db.checkpoint(self.db_path)
        dst = self.db_path + ".p001.db"
        self.addCleanup(lambda: os.path.exists(dst) and os.remove(dst))
        conn = corrupted_copy(self.db_path, dst, ["revisions_no_update"])
        self.addCleanup(conn.close)
        self.assertIn("P001", _ids(invariant_violations(conn, now_rfc3339())))
        result = compute_quiet(conn, now_rfc3339())
        self.assertIn("P001", _ids(result.diagnostics))
        self.assertFalse(result.quiet)

    def test_untouched_store_matches_reference_fingerprint(self):
        conn = _raw(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(artifact_fingerprint(conn), reference_fingerprint())

    def test_both_passes_read_one_snapshot(self):
        """A write committed by another connection between the two passes
        must not be observed: both passes run in one read transaction."""
        reader = _raw(self.db_path)
        self.addCleanup(reader.close)
        real = heartbeat.four_field_predicate
        calls = []

        def predicate_with_concurrent_write(conn):
            out = real(conn)
            if not calls:
                api.create_item(self.conn, "concurrent", "t", "design-accepted", "operator",
                                "local", "tester")
            calls.append(out)
            return out

        with mock.patch.object(heartbeat, "four_field_predicate", predicate_with_concurrent_write):
            result = compute_quiet(reader, now_rfc3339())
        self.assertEqual(calls, [(0, 0, 0, 0), (0, 0, 0, 0)])
        self.assertEqual(result.diagnostics, [])
        self.assertTrue(result.quiet)
        self.assertFalse(reader.in_transaction)
        # The write did commit; the next heartbeat sees it.
        self.assertEqual(compute_quiet(reader, now_rfc3339()).four_field, (0, 1, 0, 0))


class HeartbeatCliTests(StoreTestCase):
    def _cli(self, path):
        return subprocess.run(
            [sys.executable, "-m", "work_store.cli", "heartbeat", "--db", path,
             "--once", "--no-external"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60)

    def test_cli_quiet(self):
        proc = self._cli(self.db_path)
        self.assertEqual(proc.returncode, EXIT_QUIET, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "all quiet")

    def test_cli_not_quiet_prints_counts(self):
        self.make_item()
        proc = self._cli(self.db_path)
        self.assertEqual(proc.returncode, EXIT_DIAGNOSTICS, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         "not quiet: active=0 eligible_queued=1 blocked=0 pollable_external=0")
        self.assertEqual(proc.stderr, "")

    def test_cli_prints_diagnostics_without_attribute_error(self):
        """The old CLI printed d.id/d.reason (AttributeError on any
        diagnostic). Diagnostic fields are diag_id/subject/detail."""
        db.checkpoint(self.db_path)
        dst = self.db_path + ".cli.db"
        self.addCleanup(lambda: os.path.exists(dst) and os.remove(dst))
        corrupted_copy(self.db_path, dst, ["revisions_no_update"]).close()
        proc = self._cli(dst)
        self.assertEqual(proc.returncode, EXIT_DIAGNOSTICS, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^P001: schema -- expected=[0-9a-f]{64} actual=[0-9a-f]{64}$")
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("AttributeError", proc.stderr)

    def test_cli_query_failure_is_d000_exit_30(self):
        missing = os.path.join(_TMP_DIR, f"no-such-dir-{uuid.uuid4().hex}", "x.db")
        proc = self._cli(missing)
        self.assertEqual(proc.returncode, EXIT_QUERY_FAILED, proc.stdout + proc.stderr)
        self.assertTrue(proc.stdout.startswith("D000: invariant-query-failed -- "), proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)


# ------------------------------------------------------------- F: import/util

class ImportTriggerPreservationTests(unittest.TestCase):
    def setUp(self):
        self.src, self.src_path = fixtures.build_baseline_conn()
        self.addCleanup(fixtures.cleanup, self.src_path)
        self.addCleanup(self.src.close)
        self.projection = legacy.export_legacy(self.src)
        fresh = sqlite3.connect(":memory:")
        migrate.apply_schema(fresh, now_rfc3339(), "reference")
        self.reference_triggers = _triggers(fresh)
        fresh.close()

    def test_import_preserves_full_trigger_set(self):
        imported = legacy.import_legacy(self.projection)
        self.addCleanup(imported.close)
        for name in ("revision_insert_contract", "revision_insert_event",
                     "proof_insert_contract", "proof_insert_event",
                     "dependency_insert_contract", "dependency_insert_event"):
            self.assertIn(name, _triggers(imported))
        self.assertEqual(_triggers(imported), self.reference_triggers)
        self.assertEqual(artifact_fingerprint(imported), reference_fingerprint())
        self.assertEqual(invariant_violations(imported, now_rfc3339()), [])

    def test_queued_to_active_without_proof_rejected_after_import(self):
        path = os.path.join(_TMP_DIR, f"imported-{uuid.uuid4().hex}.db")
        self.addCleanup(fixtures.cleanup, path)
        legacy.import_legacy_to_file(self.projection, path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        # Typed path. The event triggers are back too: creating an item on
        # the imported database records its 'created' event (probe: events lost).
        api.create_item(conn, "Q", "queued after import", "design-accepted", "operator",
                        "local", "tester")
        self.assertEqual(conn.execute("SELECT event_type FROM item_events WHERE item_id='Q'")
                         .fetchall(), [("created",)])
        with self.assertRaises(OperationError) as cm:
            api.transition(conn, "Q", 1, "active", None, "tester")
        self.assertIn("claim-current-live-tip-required", str(cm.exception))
        # Raw service-authorized path, server-minted time, no proof.
        with self.assertRaises(sqlite3.Error) as cm:
            with db.operation(conn, "claim", "Q", "tester", prior_revision=1) as ctx:
                conn.execute(
                    "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                    "occurred_at,actor,operation_nonce) VALUES ('Q',2,1,'active','{}',?,'tester',?)",
                    (ctx.now, ctx.nonce))
        self.assertIn("claim-current-live-tip-required", str(cm.exception))
        self.assertEqual(conn.execute("SELECT max(revision) FROM item_revisions WHERE item_id='Q'")
                         .fetchone()[0], 1)
        # The legitimate claim path still works on the imported database.
        claimed = api.claim(conn, "Q", 1, {"proof_kind": "worker-pid", "proof_value": "pid-q", "host_id": "h",
                                           "valid_until": "2999-01-01T00:00:00.000000Z"}, "tester")
        self.assertEqual(claimed["revision"], 2)


class AtomicRoundtripWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(_TMP_DIR, f"atomic-{uuid.uuid4().hex}")
        os.makedirs(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_atomic_backup_renames_into_place(self):
        target = os.path.join(self.dir, "roundtrip.db")
        with open(target, "wb") as f:
            f.write(b"OLD")
        src = sqlite3.connect(":memory:")
        self.addCleanup(src.close)
        src.execute("CREATE TABLE t(x)")
        src.execute("INSERT INTO t VALUES (42)")
        src.commit()
        real_replace = os.replace
        with mock.patch.object(legacy.os, "replace", side_effect=real_replace) as spy:
            legacy.atomic_backup(src, target)
        spy.assert_called_once_with(target + ".tmp", target)
        self.assertFalse(os.path.exists(target + ".tmp"))
        check = sqlite3.connect(target)
        self.assertEqual(check.execute("SELECT x FROM t").fetchone(), (42,))
        check.close()

    def test_failed_backup_leaves_existing_target_untouched(self):
        target = os.path.join(self.dir, "roundtrip.db")
        with open(target, "wb") as f:
            f.write(b"OLD")
        src = sqlite3.connect(":memory:")
        src.close()  # backup from a closed connection fails mid-operation
        with self.assertRaises(sqlite3.ProgrammingError):
            legacy.atomic_backup(src, target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"OLD")

    def test_prepare_legacy_downgrade_writes_roundtrip_db_via_rename(self):
        from tests.test_downgrade_fixtures import DowngradeEndToEndCommandChainTests, _READERS_DIR
        base = DowngradeEndToEndCommandChainTests("test_full_chain_exits_zero_and_proves_parity")
        base.setUp()
        self.addCleanup(base.doCleanups)
        out_dir = os.path.join(base.stage_root, "stage")
        real_replace = os.replace
        with mock.patch.object(legacy.os, "replace", side_effect=real_replace) as spy, \
                mock.patch("sys.stdout"):
            code = cli.main(["prepare-legacy-downgrade", "--db", base.live_db, "--out", out_dir,
                             "--legacy-reader", os.path.join(_READERS_DIR, "passthrough_reader.py"),
                             "--release-manifest", base.manifest_path])
        self.assertEqual(code, cli.EXIT_OK)
        roundtrip = os.path.join(out_dir, "roundtrip.db")
        self.assertIn(mock.call(roundtrip + ".tmp", roundtrip), spy.call_args_list)
        self.assertFalse(os.path.exists(roundtrip + ".tmp"))


class CanonicalJsonTests(unittest.TestCase):
    def test_non_finite_numbers_rejected(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                canonical_json({"x": bad})

    def test_keys_nfc_normalized(self):
        self.assertEqual(canonical_json({"é": "é"}), '{"é":"é"}')

    def test_keys_colliding_after_normalization_rejected(self):
        with self.assertRaises(ValueError):
            canonical_json({"é": 1, "é": 2})

    def test_non_string_keys_rejected(self):
        with self.assertRaises(ValueError):
            canonical_json({1: "a"})

    def test_canonical_timestamp(self):
        self.assertEqual(canonical_timestamp("2026-10-05T02:00:00+02:00"),
                         "2026-10-05T00:00:00.000000Z")
        self.assertEqual(canonical_timestamp("2026-10-05T00:00:00.5Z"),
                         "2026-10-05T00:00:00.500000Z")
        for bad in ("2026-10-05T00:00:00", "yesterday", 12345):
            with self.assertRaises(ValueError):
                canonical_timestamp(bad)


# -------------------------------------------------------- A: invariant set

class ForgedOwnerDecisionImportTests(unittest.TestCase):
    """Reviewer probe (A1): an owner-decision completion that arrives via
    legacy.import_legacy with no capability ever consumed must be
    diagnosed (I004), while the genuine one round-trips clean."""

    def setUp(self):
        self.src, self.src_path = fixtures.build_baseline_conn()
        self.addCleanup(fixtures.cleanup, self.src_path)
        self.addCleanup(self.src.close)
        self.projection = legacy.export_legacy(self.src)

    def _item(self, projection, item_id):
        return next(i for i in projection["items"] if i["id"] == item_id)

    def _import_ids(self, projection):
        conn = legacy.import_legacy(projection)
        self.addCleanup(conn.close)
        return _ids(invariant_violations(conn, now_rfc3339()))

    def test_genuine_owner_decision_roundtrips_clean(self):
        d = self._item(self.projection, "D")["_canonical_v3"]
        self.assertTrue(any(e["event_type"] == "verdict"
                            and (e["metadata"] or {}).get("capability") == "owner-decision"
                            for e in d["events"]))
        self.assertEqual(self._import_ids(self.projection), [])

    def test_probe_forged_owner_decision_without_capability_record(self):
        """Rewrite C's final-phase completion as an owner-decision exception
        in the projection: no capability was ever issued or consumed."""
        c = self._item(self.projection, "C")["_canonical_v3"]
        tip = c["revisions"][-1]
        self.assertEqual((tip["status"], tip["exception_kind"]), ("done", "final-phase"))
        tip["exception_kind"] = "owner-decision"
        tip["exception_decision_date"] = "2026-10-05T00:00:00.000000Z"
        tip["exception_decision_ref"] = "FORGED-REF"
        self.assertIn("I004", self._import_ids(self.projection))

    def test_probe_capability_record_stripped(self):
        d = self._item(self.projection, "D")["_canonical_v3"]
        d["events"] = [e for e in d["events"] if e["event_type"] != "verdict"]
        self.assertIn("I004", self._import_ids(self.projection))

    def test_probe_capability_record_for_other_decision(self):
        d = self._item(self.projection, "D")["_canonical_v3"]
        d["revisions"][-1]["exception_decision_ref"] = "SOME-OTHER-REF"
        self.assertIn("I004", self._import_ids(self.projection))


class ForgedOwnerDecisionLedgerExtensionTests(StoreTestCase):
    """Reviewer probe (A1), verbatim shape: a design-accepted item created
    and claimed normally; its exported ledger extended with a syntactically
    valid revision 3 (status='done', prior_revision=2, dispatch_root_id
    NULL, successor fields NULL, exception_kind='owner-decision', forged
    reason/ref), replayed through legacy.import_legacy with no capability
    issued or consumed. Formerly an empty diagnostic list; now I004."""

    def test_probe_forged_exception_intact_schema(self):
        item = self.make_item(phase="design-accepted")
        self.claim_item(item, expected_revision=1)
        projection = legacy.export_legacy(self.conn)
        ledger = next(i for i in projection["items"] if i["id"] == item)["_canonical_v3"]
        rev2 = ledger["revisions"][-1]
        self.assertEqual((rev2["revision"], rev2["status"]), (2, "active"))
        forged = {c: None for c in rev2}
        forged.update(item_id=item, revision=3, prior_revision=2, status="done",
                      payload=rev2["payload"], occurred_at="2999-01-01T00:00:00.000000Z",
                      actor="forger", operation_nonce="f" * 64,
                      exception_kind="owner-decision", exception_reason="forged reason",
                      exception_decision_date="2999-01-01T00:00:00.000000Z",
                      exception_decision_ref="FORGED-REF")
        ledger["revisions"].append(forged)
        imported = legacy.import_legacy(projection)
        self.addCleanup(imported.close)
        # Schema intact (the forgery is not detected via P001) ...
        self.assertEqual(artifact_fingerprint(imported), reference_fingerprint())
        # ... yet the forged completion is diagnosed.
        diags = invariant_violations(imported, now_rfc3339())
        self.assertIn(("I004", item), {(d.diag_id, d.subject) for d in diags})
        result = compute_quiet(imported, now_rfc3339())
        self.assertFalse(result.quiet)
        self.assertIn("I004", _ids(result.diagnostics))


class SuccessorInvariantScopeTests(StoreTestCase):
    def _complete_with_successor(self):
        predecessor = self.make_item(phase="design-accepted")
        self.claim_item(predecessor, expected_revision=1)
        done = api.complete(self.conn, predecessor, 2, dict(_SUCCESSOR), "tester")
        return predecessor, done["successor_id"]

    def _corrupt(self, triggers, tag=""):
        db.checkpoint(self.db_path)
        dst = self.db_path + f".corrupt{tag}.db"
        self.addCleanup(lambda: os.path.exists(dst) and os.remove(dst))
        conn = corrupted_copy(self.db_path, dst, triggers)
        self.addCleanup(conn.close)
        return conn

    def test_probe_null_successor_ownership_is_i008(self):
        """Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied."""
        _, child = self._complete_with_successor()
        for column in ("successor_owner_item_id", "successor_owner_revision",
                       "successor_owner_nonce", None):
            with self.subTest(column=column):
                conn = self._corrupt(["revisions_no_update"], tag=str(column))
                cols = ([column] if column else
                        ["successor_owner_item_id", "successor_owner_revision",
                         "successor_owner_nonce"])
                conn.execute("UPDATE item_revisions SET " + ",".join(f"{c}=NULL" for c in cols) +
                             " WHERE item_id=? AND revision=1", (child,))
                conn.commit()
                self.assertIn("I008", _ids(invariant_violations(conn, now_rfc3339())))

    def test_legitimate_successor_advancement_is_not_i007(self):
        """Completion, then api.claim of the child: the successor was fresh
        queued when bound, and later advanced -- no I007 (or any) finding."""
        predecessor, child = self._complete_with_successor()
        self.claim_item(child, expected_revision=1)
        conn = _raw(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(invariant_violations(conn, now_rfc3339()), [])

    def test_successor_advanced_in_completing_transaction_is_still_i007(self):
        """Scope check: a child revision>1 carrying the predecessor's own
        completion nonce (advanced inside the binding transaction) stays I007."""
        predecessor, child = self._complete_with_successor()
        conn = self._corrupt(["revision_insert_contract", "revision_insert_event"])
        nonce, occurred = conn.execute(
            "SELECT operation_nonce, occurred_at FROM item_revisions WHERE item_id=? AND "
            "status='done'", (predecessor,)).fetchone()
        conn.execute("INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,"
                     "occurred_at,actor,operation_nonce) VALUES (?,2,1,'cancelled','{}',?,'x',?)",
                     (child, "2999-01-01T00:00:00.000000Z", nonce))
        conn.commit()
        self.assertIn("I007", _ids(invariant_violations(conn, now_rfc3339())))


if __name__ == "__main__":
    unittest.main()
