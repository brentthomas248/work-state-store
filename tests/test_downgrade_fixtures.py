"""Acceptance criterion F: executable forward-only downgrade. Runs the five
checked-in fixtures (brief.md 6.4) through the exact replay logic
`work_store.cli fixture-run` uses, asserting each produces its specified
exit code, and separately drives the full command chain end to end
(`prepare-legacy-downgrade`, `legacy-validate`, `import-legacy`,
`compare-canonical`, `heartbeat`) against a freshly built live database,
asserting exit 0 and zero parity diffs on the happy path, and exit 40 with
no staged artifacts when the reader cannot demonstrate byte-exact
unknown-object preservation.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from work_store import cli, fixtures, legacy

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIXTURES_DIR = os.path.join(_REPO_ROOT, "tests", "fixtures", "downgrade")
_READERS_DIR = os.path.join(_FIXTURES_DIR, "readers")
_TMP_DIR = os.path.join(_REPO_ROOT, "tests", "_tmp")
os.makedirs(_TMP_DIR, exist_ok=True)


def _load_fixture(name):
    with open(os.path.join(_FIXTURES_DIR, f"{name}.json"), "r", encoding="utf-8") as f:
        return json.load(f)


class DowngradeFixtureReplayTests(unittest.TestCase):
    """One test per fixture, matching the one-to-one falsification matrix
    style used by the rest of the suite. Each asserts the exact exit code
    `_replay_fixture` (the same function `fixture-run` calls) produces."""

    def _assert_fixture_exit(self, name, expected_exit):
        fixture = _load_fixture(name)
        self.assertEqual(fixture["expected_exit"], expected_exit)
        actual_exit, log = cli._replay_fixture(fixture, _READERS_DIR)
        self.assertEqual(actual_exit, expected_exit,
                          f"{name}: expected {expected_exit}, got {actual_exit}; log={log}")

    def test_roundtrip_all_is_clean(self):
        self._assert_fixture_exit("roundtrip-all", cli.EXIT_OK)

    def test_reader_drops_extension_is_unrepresentable(self):
        self._assert_fixture_exit("reader-drops-extension", cli.EXIT_REFUSED_UNREPRESENTABLE)

    def test_lossy_history_is_lossy(self):
        self._assert_fixture_exit("lossy-history", cli.EXIT_REFUSED_LOSSY)

    def test_parity_value_is_parity_mismatch(self):
        self._assert_fixture_exit("parity-value", cli.EXIT_REFUSED_PARITY)

    def test_moved_watermark_is_detected(self):
        self._assert_fixture_exit("moved-watermark", cli.EXIT_REFUSED_MOVED_WATERMARK)

    def test_fixture_run_subcommand_passes_all_five(self):
        """The exact required command: python3 -m work_store.cli fixture-run
        --fixtures tests/fixtures/downgrade, executed as a real subprocess,
        with its exit code and full stdout captured."""
        proc = subprocess.run(
            [sys.executable, "-m", "work_store.cli", "fixture-run", "--fixtures", _FIXTURES_DIR],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in cli._FIXTURE_NAMES:
            self.assertIn(f"[PASS] {name}", proc.stdout)


class DowngradeEndToEndCommandChainTests(unittest.TestCase):
    """Drives the exact CLI surface of brief.md 6.2 end to end as real
    subprocesses against a freshly built live database, per the acceptance
    requirement that criterion F be EXECUTED, not merely described."""

    def setUp(self):
        import uuid
        self.stage_root = os.path.join(_TMP_DIR, f"downgrade-e2e-{uuid.uuid4().hex}")
        os.makedirs(self.stage_root, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.stage_root, ignore_errors=True)

        conn, db_path = fixtures.build_baseline_conn()
        conn.close()
        self.live_db = os.path.join(self.stage_root, "live.db")
        shutil.copyfile(db_path, self.live_db)
        fixtures.cleanup(db_path)

        artifact_path = os.path.join(self.stage_root, "work-store.pyz")
        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("fake-content-addressed-artifact")
        with open(artifact_path, "rb") as f:
            artifact_sha256 = hashlib.sha256(f.read()).hexdigest()
        manifest_path = os.path.join(self.stage_root, "release-manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({
                "protocol": 3, "schema_min": 3, "schema_max": 3,
                "artifact": "work-store.pyz",
                "sha256": artifact_sha256,
                "self_test": "schema3-read-write", "python_abi": "cp312",
            }, f)
        self.manifest_path = manifest_path

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "work_store.cli", *args],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
        )

    def test_full_chain_exits_zero_and_proves_parity(self):
        out_dir = os.path.join(self.stage_root, "stage")
        proc = self._run(
            "prepare-legacy-downgrade", "--db", self.live_db, "--out", out_dir,
            "--legacy-reader", os.path.join(_READERS_DIR, "passthrough_reader.py"),
            "--release-manifest", self.manifest_path,
        )
        self.assertEqual(proc.returncode, cli.EXIT_OK, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(out_dir, "projection.json")))
        self.assertTrue(os.path.exists(os.path.join(out_dir, "watermark.json")))
        self.assertTrue(os.path.exists(os.path.join(out_dir, "roundtrip.db")))

        proc = self._run("legacy-validate", "--input", os.path.join(out_dir, "projection.json"),
                          "--preserve-unknown", "byte-exact")
        self.assertEqual(proc.returncode, cli.EXIT_OK, proc.stdout + proc.stderr)

        reimport_db = os.path.join(out_dir, "reimport.db")
        proc = self._run("import-legacy", "--input", os.path.join(out_dir, "projection.json"),
                          "--db", reimport_db)
        self.assertEqual(proc.returncode, cli.EXIT_OK, proc.stdout + proc.stderr)

        proc = self._run("compare-canonical", "--left-db", self.live_db,
                          "--right-db", os.path.join(out_dir, "roundtrip.db"),
                          "--watermark", os.path.join(out_dir, "watermark.json"))
        self.assertEqual(proc.returncode, cli.EXIT_OK, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout.splitlines()[0]), [])

        proc = self._run("heartbeat", "--db", os.path.join(out_dir, "roundtrip.db"),
                          "--once", "--no-external")
        # The baseline legitimately carries eligible queued work (and an
        # external-lane item), so the honest heartbeat answer is "not quiet"
        # (exit 20) with the counts printed -- and zero diagnostics: no
        # invariant, P001 fingerprint, or D000 failure on the roundtrip db.
        self.assertEqual(proc.returncode, 20, proc.stdout + proc.stderr)
        self.assertIn("not quiet: active=0 eligible_queued=", proc.stdout)
        self.assertNotRegex(proc.stdout, r"\b[IPD]\d{3}:")
        self.assertNotIn("all quiet", proc.stdout)

    def test_unrepresentable_reader_refuses_with_no_staged_output_and_source_untouched(self):
        out_dir = os.path.join(self.stage_root, "stage_refused")
        proc = self._run(
            "prepare-legacy-downgrade", "--db", self.live_db, "--out", out_dir,
            "--legacy-reader", os.path.join(_READERS_DIR, "drop_extension_reader.py"),
            "--release-manifest", self.manifest_path,
        )
        self.assertEqual(proc.returncode, cli.EXIT_REFUSED_UNREPRESENTABLE, proc.stdout + proc.stderr)
        self.assertEqual(os.listdir(out_dir), [])

        # Config remains sqlite-v3: the live db is untouched and still
        # exports cleanly with the real (passthrough) reader afterward.
        proc = self._run(
            "prepare-legacy-downgrade", "--db", self.live_db, "--out", out_dir,
            "--legacy-reader", os.path.join(_READERS_DIR, "passthrough_reader.py"),
            "--release-manifest", self.manifest_path,
        )
        self.assertEqual(proc.returncode, cli.EXIT_OK, proc.stdout + proc.stderr)

    def test_tampered_release_manifest_is_a_tool_integrity_failure(self):
        bad_manifest = os.path.join(self.stage_root, "bad-manifest.json")
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["sha256"] = "0" * 64
        with open(bad_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        out_dir = os.path.join(self.stage_root, "stage_tampered")
        proc = self._run(
            "prepare-legacy-downgrade", "--db", self.live_db, "--out", out_dir,
            "--legacy-reader", os.path.join(_READERS_DIR, "passthrough_reader.py"),
            "--release-manifest", bad_manifest,
        )
        self.assertEqual(proc.returncode, cli.EXIT_TOOL_FAILURE, proc.stdout + proc.stderr)
        self.assertEqual(os.listdir(out_dir), [])


if __name__ == "__main__":
    unittest.main()
