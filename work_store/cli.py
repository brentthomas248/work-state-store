"""Legacy downgrade CLI (brief.md 6.2). Exact exit codes:

0  staged, parity-proven, and switch complete
40 REFUSED_UNREPRESENTABLE -- reader does not demonstrate byte-exact
   `_canonical_v3` preservation
41 REFUSED_LOSSY -- round-trip omits a row/field/count
42 REFUSED_PARITY -- present but unequal normalized value/hash
43 REFUSED_MOVED_WATERMARK -- watermark moved between initial read and the
   immediately-pre-switch reread
44 tool/integrity failure

On every nonzero result config remains sqlite-v3 and no legacy writer
starts: nothing in this module ever renames a consumer config file except
on the exit-0 path, and even then only after both the transaction's final
watermark recheck succeeds and the durable renames are fsynced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

from . import legacy
from .heartbeat import EXIT_DIAGNOSTICS, EXIT_QUERY_FAILED, EXIT_QUIET, compute_quiet
from .util import canonical_hash, canonical_json, canonical_json_bytes, now_rfc3339

EXIT_OK = 0
EXIT_REFUSED_UNREPRESENTABLE = 40
EXIT_REFUSED_LOSSY = 41
EXIT_REFUSED_PARITY = 42
EXIT_REFUSED_MOVED_WATERMARK = 43
EXIT_TOOL_FAILURE = 44

_FIXTURE_NAMES = [
    "roundtrip-all", "reader-drops-extension", "lossy-history",
    "parity-value", "moved-watermark",
]
_FIXTURE_EXPECTED_EXIT = {
    "roundtrip-all": EXIT_OK,
    "reader-drops-extension": EXIT_REFUSED_UNREPRESENTABLE,
    "lossy-history": EXIT_REFUSED_LOSSY,
    "parity-value": EXIT_REFUSED_PARITY,
    "moved-watermark": EXIT_REFUSED_MOVED_WATERMARK,
}


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _run_reader(reader_path: str, projection: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, reader_path], input=canonical_json_bytes(projection),
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"legacy-reader failed: {proc.stderr.decode('utf-8', 'replace')}")
    return json.loads(proc.stdout.decode("utf-8"))


def _reader_fidelity_diffs(projection: dict, reader_output: dict) -> list[str]:
    left_by_id = {i["id"]: i for i in projection["items"]}
    right_by_id = {i["id"]: i for i in reader_output.get("items", [])}
    problems = []
    for item_id, left in left_by_id.items():
        right = right_by_id.get(item_id)
        if right is None or "_canonical_v3" not in right:
            problems.append(f"items[{item_id}]._canonical_v3 missing from reader output")
        elif canonical_json(right["_canonical_v3"]) != canonical_json(left["_canonical_v3"]):
            problems.append(f"items[{item_id}]._canonical_v3 not byte-exact in reader output")
    return problems


def _check_release_manifest(manifest_path: str) -> str | None:
    """Returns an error string, or None if the manifest and its pinned
    artifact check out (brief.md 6.2: protocol/schema range/measured
    digest)."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return f"release manifest unreadable: {e}"
    required = ["protocol", "schema_min", "schema_max", "artifact", "sha256", "self_test", "python_abi"]
    missing = [k for k in required if k not in manifest]
    if missing:
        return f"release manifest missing fields: {missing}"
    if not (manifest["schema_min"] <= 3 <= manifest["schema_max"]):
        return "release manifest schema range does not contain 3"
    artifact_path = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), manifest["artifact"])
    if not os.path.exists(artifact_path):
        return f"pinned artifact not found: {artifact_path}"
    measured = _sha256_file(artifact_path)
    if measured != manifest["sha256"]:
        return f"pinned artifact digest mismatch: measured {measured} != manifest {manifest['sha256']}"
    return None


def cmd_prepare_legacy_downgrade(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("BEGIN IMMEDIATE")

        manifest_error = _check_release_manifest(args.release_manifest)
        if manifest_error is not None:
            conn.execute("ROLLBACK")
            print(f"REFUSED (tool/integrity failure): {manifest_error}")
            return EXIT_TOOL_FAILURE

        watermark_before = legacy.compute_watermark(conn)
        projection = legacy.export_legacy(conn)

        try:
            reader_output = _run_reader(args.legacy_reader, projection)
        except Exception as e:
            conn.execute("ROLLBACK")
            print(f"REFUSED (tool/integrity failure): legacy-reader invocation failed: {e}")
            return EXIT_TOOL_FAILURE
        fidelity_problems = _reader_fidelity_diffs(projection, reader_output)
        if fidelity_problems:
            conn.execute("ROLLBACK")
            print("REFUSED_UNREPRESENTABLE:")
            for p in fidelity_problems:
                print(f"  {p}")
            return EXIT_REFUSED_UNREPRESENTABLE

        roundtrip_conn = legacy.import_legacy(projection)
        diffs = legacy.compare_projection_to_conn(projection, roundtrip_conn)
        missing = [d for d in diffs if d["kind"] == "missing"]
        mismatch = [d for d in diffs if d["kind"] == "mismatch"]
        if missing:
            conn.execute("ROLLBACK")
            print("REFUSED_LOSSY:")
            for d in missing:
                print(f"  {d['path']}")
            return EXIT_REFUSED_LOSSY
        if mismatch:
            conn.execute("ROLLBACK")
            print("REFUSED_PARITY:")
            for d in mismatch:
                print(f"  {d['path']}")
            return EXIT_REFUSED_PARITY

        # Immediately before switch, on the SAME connection/transaction:
        # reread and compare against the initial read.
        watermark_after = legacy.compute_watermark(conn)
        if canonical_hash(watermark_before) != canonical_hash(watermark_after):
            conn.execute("ROLLBACK")
            print("REFUSED_MOVED_WATERMARK: watermark changed since initial read")
            print(f"  before: {canonical_json(watermark_before)}")
            print(f"  after:  {canonical_json(watermark_after)}")
            return EXIT_REFUSED_MOVED_WATERMARK

        _atomic_write(os.path.join(args.out, "projection.json"), canonical_json_bytes(projection))
        _atomic_write(os.path.join(args.out, "watermark.json"), canonical_json_bytes(watermark_after))
        legacy.atomic_backup(roundtrip_conn, os.path.join(args.out, "roundtrip.db"))

        conn.execute("COMMIT")
        print(f"OK: staged to {args.out} (schema_version={watermark_after['schema_version']})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_legacy_validate(args) -> int:
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            text = f.read()
        projection = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"REFUSED (tool/integrity failure): {e}")
        return EXIT_TOOL_FAILURE

    for item in projection.get("items", []):
        try:
            encoded = canonical_json_bytes(item["_canonical_v3"])
            decoded = json.loads(encoded.decode("utf-8"))
            if canonical_json(decoded) != canonical_json(item["_canonical_v3"]):
                print(f"REFUSED (tool/integrity failure): items[{item['id']}]._canonical_v3 "
                      "not byte-exact across an encode/decode cycle")
                return EXIT_TOOL_FAILURE
        except (ValueError, KeyError) as e:
            print(f"REFUSED (tool/integrity failure): items[{item.get('id')}] {e}")
            return EXIT_TOOL_FAILURE

    sys.stdout.buffer.write(canonical_json_bytes(projection))
    print(f"\n# executable identity: {os.path.abspath(__file__)} sha256={_sha256_file(__file__)} "
          f"--version=work-store-cli/1 preserve-unknown={args.preserve_unknown}", file=sys.stderr)
    return EXIT_OK


def _reject_duplicate_keys(pairs):
    seen = set()
    out = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key: {k}")
        seen.add(k)
        out[k] = v
    return out


def cmd_import_legacy(args) -> int:
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            projection = json.load(f)
        legacy.import_legacy_to_file(projection, args.db)
    except Exception as e:
        print(f"REFUSED (tool/integrity failure): {e}")
        return EXIT_TOOL_FAILURE
    print(f"OK: imported {len(projection.get('items', []))} items to {args.db}")
    return EXIT_OK


def cmd_compare_canonical(args) -> int:
    try:
        left = sqlite3.connect(args.left_db)
        right = sqlite3.connect(args.right_db)
        diffs = legacy._diffs(legacy.export_legacy(left), legacy.export_legacy(right))
        with open(args.watermark, "r", encoding="utf-8") as f:
            stored_watermark = json.load(f)
        current_watermark = legacy.compute_watermark(left)
    except Exception as e:
        print(f"REFUSED (tool/integrity failure): {e}")
        return EXIT_TOOL_FAILURE

    print(json.dumps(diffs, indent=2))
    if canonical_hash(stored_watermark) != canonical_hash(current_watermark):
        print("REFUSED_MOVED_WATERMARK")
        return EXIT_REFUSED_MOVED_WATERMARK
    if any(d["kind"] == "missing" for d in diffs):
        return EXIT_REFUSED_LOSSY
    if any(d["kind"] == "mismatch" for d in diffs):
        return EXIT_REFUSED_PARITY
    print("OK: parity")
    return EXIT_OK


def cmd_heartbeat(args) -> int:
    try:
        conn = sqlite3.connect(args.db)
    except sqlite3.Error as e:
        print(f"D000: invariant-query-failed -- {e}")
        return EXIT_QUERY_FAILED
    try:
        # P001 always runs: compute_quiet checks the compiled reference
        # fingerprint when no explicit one is pinned.
        result = compute_quiet(conn, now_rfc3339())
    finally:
        conn.close()
    if result.quiet:
        print("all quiet")
    else:
        for d in result.diagnostics:
            print(f"{d.diag_id}: {d.subject} -- {d.detail}")
        if result.four_field:
            active, eligible, blocked, pollable = result.four_field
            print(f"not quiet: active={active} eligible_queued={eligible} "
                  f"blocked={blocked} pollable_external={pollable}")
    return result.exit_code


def _build_fixture_source(tmp_dir: str):
    from . import fixtures as fixture_module
    conn, path = fixture_module.build_baseline_conn()
    return conn, path, fixture_module


def generate_fixtures(out_dir: str) -> None:
    """Builds the five checked-in fixtures from a single rich baseline
    database (brief.md 6.4). Each fixture file is this tool's own scenario
    descriptor: the (possibly deliberately mutated) legacy projection, which
    stub reader to replay it through, and the watermark captured at the
    point the real pipeline would have taken its initial read."""
    from . import fixtures as fixture_module

    os.makedirs(out_dir, exist_ok=True)
    readers_dir = os.path.join(out_dir, "readers")
    os.makedirs(readers_dir, exist_ok=True)
    passthrough_reader = os.path.join(readers_dir, "passthrough_reader.py")
    drop_extension_reader = os.path.join(readers_dir, "drop_extension_reader.py")
    with open(passthrough_reader, "w", encoding="utf-8") as f:
        f.write(
            "import sys, json\n"
            "obj = json.load(sys.stdin)\n"
            "json.dump(obj, sys.stdout)\n"
        )
    with open(drop_extension_reader, "w", encoding="utf-8") as f:
        f.write(
            "import sys, json\n"
            "obj = json.load(sys.stdin)\n"
            "for item in obj.get('items', []):\n"
            "    item.pop('_canonical_v3', None)\n"
            "json.dump(obj, sys.stdout)\n"
        )

    conn, db_path = fixture_module.build_baseline_conn()
    try:
        watermark_before = legacy.compute_watermark(conn)
        projection = legacy.export_legacy(conn)

        _write_fixture(out_dir, "roundtrip-all", projection, projection, "passthrough",
                        watermark_before, EXIT_OK,
                        "Full baseline: every status, transition, completion arm, "
                        "proof observation, resolution, dependency, note/history "
                        "event, null optional, Unicode string, and an opaque "
                        "nested payload object a legacy reader would not "
                        "understand. Round-trips byte-exact.")

        _write_fixture(out_dir, "reader-drops-extension", projection, projection, "drop-extension",
                        watermark_before, EXIT_REFUSED_UNREPRESENTABLE,
                        "Same data as roundtrip-all, replayed through a stub reader "
                        "that deletes _canonical_v3 -- the reader does not "
                        "demonstrate byte-exact unknown-object preservation.")

        import copy
        lossy = copy.deepcopy(projection)
        lossy["items"][0]["_canonical_v3"]["events"].pop()
        _write_fixture(out_dir, "lossy-history", projection, lossy, "passthrough",
                        watermark_before, EXIT_REFUSED_LOSSY,
                        "One item_events row dropped from the staged projection "
                        "relative to the true live source -- round-trip omits a "
                        "row.")

        parity = copy.deepcopy(projection)
        parity["items"][0]["lane"] = (
            "claude-code" if parity["items"][0]["lane"] != "claude-code" else "operator"
        )
        _write_fixture(out_dir, "parity-value", projection, parity, "passthrough",
                        watermark_before, EXIT_REFUSED_PARITY,
                        "One item's lane field changed in the staged projection "
                        "relative to the true live source -- present on both "
                        "sides but unequal.")

        from . import api
        api.add_note(conn, "A", 3, "a note that lands after the initial watermark read", "tester")
        projection_after_write = legacy.export_legacy(conn)
        _write_fixture(out_dir, "moved-watermark", projection_after_write, projection_after_write,
                        "passthrough", watermark_before, EXIT_REFUSED_MOVED_WATERMARK,
                        "A note event is appended to the live source after the "
                        "initial watermark read (watermark_before) but before the "
                        "immediately-pre-switch reread (source_watermark in the "
                        "projection) -- the two must not match.")
    finally:
        conn.close()
        fixture_module.cleanup(db_path)


def _write_fixture(out_dir, name, source_projection, projection, reader, watermark_before,
                    expected_exit, description):
    """`source_projection` is the true, ground-truth export of the live
    baseline db; `projection` is what actually gets staged/replayed (for
    lossy-history and parity-value these differ -- that's the deliberate
    fault)."""
    fixture = {
        "name": name,
        "description": description,
        "reader": reader,
        "watermark_before": watermark_before,
        "source_projection": source_projection,
        "projection": projection,
        "expected_exit": expected_exit,
    }
    path = os.path.join(out_dir, f"{name}.json")
    with open(path, "wb") as f:
        f.write(canonical_json_bytes(fixture))


def _replay_fixture(fixture: dict, readers_dir: str) -> tuple[int, list[str]]:
    """Replays one fixture's scenario through the same checks
    prepare-legacy-downgrade performs, in the same order, and returns the
    resulting exit code plus a short log of what was checked."""
    log = []
    projection = fixture["projection"]
    source_projection = fixture["source_projection"]
    reader_name = fixture["reader"]
    reader_path = os.path.join(readers_dir, f"{reader_name.replace('-', '_')}_reader.py")

    reader_output = _run_reader(reader_path, projection)
    fidelity_problems = _reader_fidelity_diffs(projection, reader_output)
    log.append(f"reader={reader_name}: {len(fidelity_problems)} fidelity problem(s)")
    if fidelity_problems:
        return EXIT_REFUSED_UNREPRESENTABLE, log

    roundtrip_conn = legacy.import_legacy(projection)
    diffs = legacy.compare_projection_to_conn(source_projection, roundtrip_conn)
    missing = [d for d in diffs if d["kind"] == "missing"]
    mismatch = [d for d in diffs if d["kind"] == "mismatch"]
    log.append(f"roundtrip diffs: {len(missing)} missing, {len(mismatch)} mismatch")
    if missing:
        return EXIT_REFUSED_LOSSY, log
    if mismatch:
        return EXIT_REFUSED_PARITY, log

    before_hash = canonical_hash(fixture["watermark_before"])
    after_hash = canonical_hash(projection["source_watermark"])
    log.append(f"watermark before={before_hash[:12]} after={after_hash[:12]}")
    if before_hash != after_hash:
        return EXIT_REFUSED_MOVED_WATERMARK, log

    return EXIT_OK, log


def cmd_fixture_run(args) -> int:
    readers_dir = os.path.join(args.fixtures, "readers")
    all_ok = True
    start = time.time()
    for name in _FIXTURE_NAMES:
        path = os.path.join(args.fixtures, f"{name}.json")
        t0 = time.time()
        try:
            with open(path, "r", encoding="utf-8") as f:
                fixture = json.load(f)
            actual_exit, log = _replay_fixture(fixture, readers_dir)
        except Exception as e:
            actual_exit, log = EXIT_TOOL_FAILURE, [f"error: {e}"]
        elapsed = time.time() - t0
        expected_exit = _FIXTURE_EXPECTED_EXIT[name]
        ok = actual_exit == expected_exit
        all_ok = all_ok and ok
        digest = _sha256_file(path) if os.path.exists(path) else "MISSING"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: expected={expected_exit} actual={actual_exit} "
              f"digest={digest} elapsed={elapsed:.4f}s")
        for line in log:
            print(f"    {line}")
    print(f"total elapsed={time.time() - start:.4f}s")
    return EXIT_OK if all_ok else EXIT_TOOL_FAILURE


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="work_store.cli")
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("prepare-legacy-downgrade")
    pl.add_argument("--db", required=True)
    pl.add_argument("--out", required=True)
    pl.add_argument("--legacy-reader", required=True)
    pl.add_argument("--release-manifest", required=True)
    pl.set_defaults(func=cmd_prepare_legacy_downgrade)

    lv = sub.add_parser("legacy-validate")
    lv.add_argument("--input", required=True)
    lv.add_argument("--preserve-unknown", default="byte-exact")
    lv.set_defaults(func=cmd_legacy_validate)

    il = sub.add_parser("import-legacy")
    il.add_argument("--input", required=True)
    il.add_argument("--db", required=True)
    il.set_defaults(func=cmd_import_legacy)

    cc = sub.add_parser("compare-canonical")
    cc.add_argument("--left-db", required=True)
    cc.add_argument("--right-db", required=True)
    cc.add_argument("--watermark", required=True)
    cc.set_defaults(func=cmd_compare_canonical)

    hb = sub.add_parser("heartbeat")
    hb.add_argument("--db", required=True)
    hb.add_argument("--once", action="store_true")
    hb.add_argument("--no-external", action="store_true")
    hb.set_defaults(func=cmd_heartbeat)

    fr = sub.add_parser("fixture-run")
    fr.add_argument("--fixtures", required=True)
    fr.set_defaults(func=cmd_fixture_run)

    gf = sub.add_parser("generate-fixtures", help="internal: (re)generate the checked-in fixtures")
    gf.add_argument("--out", required=True)
    gf.set_defaults(func=lambda args: (generate_fixtures(args.out), EXIT_OK)[1])

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
