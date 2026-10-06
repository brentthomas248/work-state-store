# Remediation receipt -- work-state-store v3.1 (REJECT #1 follow-through)

- Reviewed (rejected) SHA: `3c104dc62fe7c735e85442363d2a094f489c9c53`
  (`feature/work-state-store/v3`)
- Remediation branch: `fix/work-state-store/v3r1`. The new SHA for
  re-review is the commit that adds this file.
- Interpreter: `/Applications/Xcode.app/Contents/Developer/usr/bin/python3`, Python 3.9.6
- Scope: the verdict's quoted findings only. No document or tenant scoping
  exists in this codebase, and none was added or altered.

## Finding -> fix -> negative test (mirrors the reviewer's probe)

| Verdict finding | Fix | Probe-mirroring test(s) |
|---|---|---|
| C: `claim(..., occurred_at=None)` admits an expired proof on caller time (BACKDATED_EXPIRED_CLAIM) | `db.operation` samples `ctx.now` AFTER `BEGIN IMMEDIATE`; `op_now()` SQL function; all `occurred_at`/`observed_at` parameters removed from the API; proof dicts with time keys refused; triggers require `occurred_at`/`recorded_at` = `op_now()` and `valid_until >= op_now()` | `test_dispatch_proof.DefectOneProbeTests`: `test_probe_backdated_claim_via_api_rejected` (created 2000-01-01, caller time 2000-01-02, `valid_until` 2000-01-03), `test_probe_backdated_claim_via_raw_authorized_sql_rejected`, `test_raw_revision_with_caller_occurred_at_rejected`, `test_claim_rejected_once_server_clock_passes_valid_until`, `test_naive_valid_until_rejected` |
| E: `quiet` ignores counts (QUEUED_WORK (0,1,0,0), ACTIVE_WORK (1,0,0,0)) | `quiet = no diagnostics AND passes equal AND every count zero`; otherwise exit 20 | `test_probe_queued_work_is_not_quiet`, `test_probe_active_work_is_not_quiet`, `test_blocked_and_pollable_external_are_not_quiet` |
| E: CLI `d.id`/`d.reason` AttributeError | prints `diag_id: subject -- detail`; connect failure → D000 exit 30 | `HeartbeatCliTests` (exit 0 / 20 counts / 20 with P001 line, no traceback / 30 D000) |
| E: P001 optional | `invariant_violations` always runs P001 against `reference_fingerprint()` when none is pinned | `test_p001_runs_without_a_pinned_fingerprint`, `test_cli_prints_diagnostics_without_attribute_error` |
| E: repeated reads not in one snapshot | both passes run inside one deferred read transaction | `test_both_passes_read_one_snapshot` (concurrent committed write between passes is not observed) |
| B: caller-supplied issuer object accepted (CALLER_ISSUER_BLOCK_RESOLUTION, CALLER_SELECTED_ISSUER) | issuer constructed and owned by `db.connect`; `resolve_block`/`complete` take tokens only | `test_caller_constructed_issuer_token_rejected_by_resolve_block`, `..._by_complete`, `test_api_takes_no_issuer_parameter` |
| B: vacuous rollback test (release_calls=0) | rewritten: valid token, real reservation, in-DB failure (`new_status='done'`), real rollback | `test_capability_released_on_rollback_is_usable_again` asserts reserve=1, `release > 0`, finalize=0, no row committed, token reusable |
| F: import drops 6 triggers (IMPORTED_MISSING_TRIGGERS, IMPORTED_GENERIC_ACTIVE, events lost) | triggers recreated from their exact stored SQL; full set verified against a fresh schema (`ImportIntegrityError` otherwise) | `test_import_preserves_full_trigger_set`, `test_queued_to_active_without_proof_rejected_after_import` (typed + raw path, `created` event recorded, legitimate claim still works) |
| F: `roundtrip.db` not atomically renamed | `legacy.atomic_backup` (tmp, fsync, `os.replace`, dir fsync), used by `prepare-legacy-downgrade` and `import_legacy_to_file` | `AtomicRoundtripWriteTests` (3, including the CLI command in-process with an `os.replace` spy) |
| F: CANONICAL_NONFINITE; keys not normalized; timestamps not canonicalized | `allow_nan=False`; keys NFC-normalized, collisions and non-string keys rejected; `canonical_timestamp` at API boundary | `CanonicalJsonTests` (5) |
| A1: forged owner-decision completion via `legacy.import_legacy` (FORGED_EXCEPTION_INTACT_SCHEMA, empty diagnostics) | `complete` records a capability-consumption `verdict` event in the same transaction; I004(c) requires it | `ForgedOwnerDecisionLedgerExtensionTests.test_probe_forged_exception_intact_schema` (verbatim probe shape), `ForgedOwnerDecisionImportTests` (4) |
| A2: NULL_SUCCESSOR_OWNERSHIP | I008 treats NULL ownership/nonce as violation | `test_probe_null_successor_ownership_is_i008` (each column, and all three) |
| A3: I007 false positive after completion + `api.claim` of child | I007 limited to child revisions that predate, or share the nonce of, the binding completion | `test_legitimate_successor_advancement_is_not_i007`; detection retained by `test_successor_advanced_in_completing_transaction_is_still_i007` and the existing `test_i007_successor_not_fresh_queued` |
| G: report over-claims | BUILD-REPORT.md rewritten: property→evidence table; remaining gaps labeled STAYS PROSE | -- |

Changed existing tests (all within the findings):
- `test_downgrade_fixtures.test_full_chain_exits_zero_and_proves_parity`:
  the heartbeat on `roundtrip.db` now expects exit 20 with no diagnostics.
  The baseline has queued work, so the old exit-0 "all quiet" assertion
  encoded the false silence.
- Callers updated for the removed `issuer=` / `occurred_at=` /
  `observed_at=` parameters.
- `test_happy_path.test_append_proof_observation_chain` no longer supplies
  a past `observed_at`.
- `test_dispatch_proof` simulates expiry by advancing the server clock
  instead of supplying a caller timestamp.
- The checked-in downgrade fixtures were regenerated with
  `python3 -m work_store.cli generate-fixtures --out tests/fixtures/downgrade`,
  because schema text and fixture content changed.

Noted only (out of scope per brief):
- The D weak-update probes (nonexistent columns).
- I005 and I010 share the `<>`-with-NULL shape that was fixed in I008.

## 1. Full suite (fix branch)

Each command was run with stdout and stderr captured; the trailing `exit=N`
line is `echo "exit=$?"` appended immediately after the command.

```
$ python3 -m unittest discover -s tests -p "test_*.py"
..................................................................................................................
----------------------------------------------------------------------
Ran 114 tests in 3.229s

OK
exit=0
```

## 2. Downgrade fixtures (fix branch)

```
$ python3 -m work_store.cli fixture-run --fixtures tests/fixtures/downgrade
[PASS] roundtrip-all: expected=0 actual=0 digest=342378ee6cb73009eed1ecd547f7be950632a6d3446c5977fbfd45d30c708ce3 elapsed=0.0232s
    reader=passthrough: 0 fidelity problem(s)
    roundtrip diffs: 0 missing, 0 mismatch
    watermark before=c4b87612cd4a after=c4b87612cd4a
[PASS] reader-drops-extension: expected=40 actual=40 digest=7c85a88a379d003c20d238dfcbcbd10c5ba546e331ee57f6d615c66c4474a821 elapsed=0.0173s
    reader=drop-extension: 8 fidelity problem(s)
[PASS] lossy-history: expected=41 actual=41 digest=cd164b5206436306e08cd86418f23c3aef0d3f353910a4671bcb638c3067dc18 elapsed=0.0217s
    reader=passthrough: 0 fidelity problem(s)
    roundtrip diffs: 1 missing, 0 mismatch
[PASS] parity-value: expected=42 actual=42 digest=39a8451d58ae27a44808682d1ffecaf2ce976d53e992a8a0dcb26540f3788c12 elapsed=0.0217s
    reader=passthrough: 0 fidelity problem(s)
    roundtrip diffs: 0 missing, 2 mismatch
[PASS] moved-watermark: expected=43 actual=43 digest=3a8311f150d9677c4e19f93c2efed3338b299acc8388e7ef3126c43a5608e761 elapsed=0.0216s
    reader=passthrough: 0 fidelity problem(s)
    roundtrip diffs: 0 missing, 0 mismatch
    watermark before=c4b87612cd4a after=87c5123c39a5
total elapsed=0.1059s
exit=0
```

## 3. Probe-mirroring tests, verbose (fix branch)

```
$ python3 -m unittest -v tests.test_v3r1_remediation tests.test_dispatch_proof.DefectOneProbeTests tests.test_block_authority
test_atomic_backup_renames_into_place (tests.test_v3r1_remediation.AtomicRoundtripWriteTests) ... ok
test_failed_backup_leaves_existing_target_untouched (tests.test_v3r1_remediation.AtomicRoundtripWriteTests) ... ok
test_prepare_legacy_downgrade_writes_roundtrip_db_via_rename (tests.test_v3r1_remediation.AtomicRoundtripWriteTests) ... ok
test_canonical_timestamp (tests.test_v3r1_remediation.CanonicalJsonTests) ... ok
test_keys_colliding_after_normalization_rejected (tests.test_v3r1_remediation.CanonicalJsonTests) ... ok
test_keys_nfc_normalized (tests.test_v3r1_remediation.CanonicalJsonTests) ... ok
test_non_finite_numbers_rejected (tests.test_v3r1_remediation.CanonicalJsonTests) ... ok
test_non_string_keys_rejected (tests.test_v3r1_remediation.CanonicalJsonTests) ... ok
test_genuine_owner_decision_roundtrips_clean (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests) ... ok
test_probe_capability_record_for_other_decision (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests) ... ok
test_probe_capability_record_stripped (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests) ... ok
test_probe_forged_owner_decision_without_capability_record (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests)
Rewrite C's final-phase completion as an owner-decision exception ... ok
test_probe_forged_exception_intact_schema (tests.test_v3r1_remediation.ForgedOwnerDecisionLedgerExtensionTests) ... ok
test_cli_not_quiet_prints_counts (tests.test_v3r1_remediation.HeartbeatCliTests) ... ok
test_cli_prints_diagnostics_without_attribute_error (tests.test_v3r1_remediation.HeartbeatCliTests)
The old CLI printed d.id/d.reason (AttributeError on any ... ok
test_cli_query_failure_is_d000_exit_30 (tests.test_v3r1_remediation.HeartbeatCliTests) ... ok
test_cli_quiet (tests.test_v3r1_remediation.HeartbeatCliTests) ... ok
test_blocked_and_pollable_external_are_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests) ... ok
test_both_passes_read_one_snapshot (tests.test_v3r1_remediation.HeartbeatSilenceTests)
A write committed by another connection between the two passes ... ok
test_empty_store_is_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests) ... ok
test_p001_runs_without_a_pinned_fingerprint (tests.test_v3r1_remediation.HeartbeatSilenceTests)
P001 is mandatory: a schema-artifact drift is diagnosed even when ... ok
test_probe_active_work_is_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
Reviewer probe ACTIVE_WORK (1,0,0,0). ... ok
test_probe_queued_work_is_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
Reviewer probe QUEUED_WORK (0,1,0,0): no diagnostics, but one ... ok
test_untouched_store_matches_reference_fingerprint (tests.test_v3r1_remediation.HeartbeatSilenceTests) ... ok
test_import_preserves_full_trigger_set (tests.test_v3r1_remediation.ImportTriggerPreservationTests) ... ok
test_queued_to_active_without_proof_rejected_after_import (tests.test_v3r1_remediation.ImportTriggerPreservationTests) ... ok
test_legitimate_successor_advancement_is_not_i007 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Completion, then api.claim of the child: the successor was fresh ... ok
test_probe_null_successor_ownership_is_i008 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied. ... ok
test_successor_advanced_in_completing_transaction_is_still_i007 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Scope check: a child revision>1 carrying the predecessor's own ... ok
test_claim_rejected_once_server_clock_passes_valid_until (tests.test_dispatch_proof.DefectOneProbeTests)
Same proof, judged at two server times: admitted before expiry, ... ok
test_naive_valid_until_rejected (tests.test_dispatch_proof.DefectOneProbeTests) ... ok
test_probe_backdated_claim_via_api_rejected (tests.test_dispatch_proof.DefectOneProbeTests) ... ok
test_probe_backdated_claim_via_raw_authorized_sql_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
The same probe on the raw service-authorized insert path: the ... ok
test_raw_revision_with_caller_occurred_at_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
A revision whose occurred_at is anything but op_now() is refused, ... ok
test_api_takes_no_issuer_parameter (tests.test_block_authority.BlockAuthorityTests)
The issuer is service-owned: no typed-API entry point accepts an ... ok
test_caller_constructed_issuer_token_rejected_by_complete (tests.test_block_authority.BlockAuthorityTests)
Reviewer probe (B) against complete()'s owner-decision arm. ... ok
test_caller_constructed_issuer_token_rejected_by_resolve_block (tests.test_block_authority.BlockAuthorityTests)
Reviewer probe (B): a freshly constructed OwnerCapabilityIssuer() ... ok
test_capability_cannot_be_reserved_twice_concurrently (tests.test_block_authority.BlockAuthorityTests) ... ok
test_capability_forged_signature_rejected (tests.test_block_authority.BlockAuthorityTests) ... ok
test_capability_released_on_rollback_is_usable_again (tests.test_block_authority.BlockAuthorityTests)
Rollback/replay semantics: a reservation that never commits must ... ok
test_capability_single_use_after_commit (tests.test_block_authority.BlockAuthorityTests) ... ok
test_capability_wrong_scope_rejected (tests.test_block_authority.BlockAuthorityTests) ... ok
test_generic_transition_cannot_forge_resolution_fields (tests.test_block_authority.BlockAuthorityTests)
Even a transition that lands on a non-resolution-requiring arc ... ok
test_knowing_stored_decision_ref_text_cannot_forge_capability (tests.test_block_authority.BlockAuthorityTests)
A caller who only knows the plaintext decision_ref (readable via ... ok
test_tblock01_generic_transition_cannot_resolve_block (tests.test_block_authority.BlockAuthorityTests)
Caller reads the exact decision reference (it's visible via ... ok

----------------------------------------------------------------------
Ran 45 tests in 2.102s

OK
exit=0
```

## 4. Negative control: the same tests against the rejected SHA 3c104dc

Method:
1. `git worktree add ../baseline-3c104dc 3c104dc62fe7c735e85442363d2a094f489c9c53`
2. Copied the three test files above into it, unmodified except for two
   import shims in `test_v3r1_remediation.py` for names that do not exist
   at 3c104dc:
   - `canonical_timestamp` raises AssertionError.
   - `reference_fingerprint` is computed from 3c104dc's own migrated schema.
3. Ran the same command.

Result: of 45 test methods, 34 fail or error at 3c104dc and pass on the
fix. unittest reports `failures=28, errors=9` because it counts each
failing I008 subTest separately. The 11 methods that pass at baseline are:
- Pre-existing block-authority tests.
- Deliberate non-regression guards: empty-store quiet, CLI quiet, the
  untouched-store fingerprint, and I007 still detected in-transaction.
- `test_caller_constructed_issuer_token_rejected_by_complete`. At 3c104dc
  this test cannot hand `complete` the rogue issuer object, because the
  fixed signature has no such parameter, so it is refused for lack of an
  issuer. The baseline failure for that probe is shown instead by
  `test_api_takes_no_issuer_parameter`.

```
$ git rev-parse HEAD; git diff --stat; python3 -m unittest -v tests.test_v3r1_remediation tests.test_dispatch_proof.DefectOneProbeTests tests.test_block_authority
3c104dc62fe7c735e85442363d2a094f489c9c53
 tests/test_block_authority.py |  84 +++++++++++++++++++++---
 tests/test_dispatch_proof.py  | 148 +++++++++++++++++++++++++++++++++++-------
 2 files changed, 198 insertions(+), 34 deletions(-)
test_atomic_backup_renames_into_place (tests.test_v3r1_remediation.AtomicRoundtripWriteTests) ... ERROR
test_failed_backup_leaves_existing_target_untouched (tests.test_v3r1_remediation.AtomicRoundtripWriteTests) ... ERROR
test_prepare_legacy_downgrade_writes_roundtrip_db_via_rename (tests.test_v3r1_remediation.AtomicRoundtripWriteTests) ... ERROR
test_canonical_timestamp (tests.test_v3r1_remediation.CanonicalJsonTests) ... FAIL
test_keys_colliding_after_normalization_rejected (tests.test_v3r1_remediation.CanonicalJsonTests) ... FAIL
test_keys_nfc_normalized (tests.test_v3r1_remediation.CanonicalJsonTests) ... FAIL
test_non_finite_numbers_rejected (tests.test_v3r1_remediation.CanonicalJsonTests) ... FAIL
test_non_string_keys_rejected (tests.test_v3r1_remediation.CanonicalJsonTests) ... FAIL
test_genuine_owner_decision_roundtrips_clean (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests) ... FAIL
test_probe_capability_record_for_other_decision (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests) ... FAIL
test_probe_capability_record_stripped (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests) ... FAIL
test_probe_forged_owner_decision_without_capability_record (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests)
Rewrite C's final-phase completion as an owner-decision exception ... FAIL
test_probe_forged_exception_intact_schema (tests.test_v3r1_remediation.ForgedOwnerDecisionLedgerExtensionTests) ... FAIL
test_cli_not_quiet_prints_counts (tests.test_v3r1_remediation.HeartbeatCliTests) ... FAIL
test_cli_prints_diagnostics_without_attribute_error (tests.test_v3r1_remediation.HeartbeatCliTests)
The old CLI printed d.id/d.reason (AttributeError on any ... FAIL
test_cli_query_failure_is_d000_exit_30 (tests.test_v3r1_remediation.HeartbeatCliTests) ... FAIL
test_cli_quiet (tests.test_v3r1_remediation.HeartbeatCliTests) ... ok
test_blocked_and_pollable_external_are_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests) ... FAIL
test_both_passes_read_one_snapshot (tests.test_v3r1_remediation.HeartbeatSilenceTests)
A write committed by another connection between the two passes ... FAIL
test_empty_store_is_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests) ... ok
test_p001_runs_without_a_pinned_fingerprint (tests.test_v3r1_remediation.HeartbeatSilenceTests)
P001 is mandatory: a schema-artifact drift is diagnosed even when ... FAIL
test_probe_active_work_is_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
Reviewer probe ACTIVE_WORK (1,0,0,0). ... FAIL
test_probe_queued_work_is_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
Reviewer probe QUEUED_WORK (0,1,0,0): no diagnostics, but one ... FAIL
test_untouched_store_matches_reference_fingerprint (tests.test_v3r1_remediation.HeartbeatSilenceTests) ... ok
test_import_preserves_full_trigger_set (tests.test_v3r1_remediation.ImportTriggerPreservationTests) ... FAIL
test_queued_to_active_without_proof_rejected_after_import (tests.test_v3r1_remediation.ImportTriggerPreservationTests) ... FAIL
test_legitimate_successor_advancement_is_not_i007 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Completion, then api.claim of the child: the successor was fresh ... FAIL
test_probe_null_successor_ownership_is_i008 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied. ... test_successor_advanced_in_completing_transaction_is_still_i007 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Scope check: a child revision>1 carrying the predecessor's own ... ok
test_claim_rejected_once_server_clock_passes_valid_until (tests.test_dispatch_proof.DefectOneProbeTests)
Same proof, judged at two server times: admitted before expiry, ... ERROR
test_naive_valid_until_rejected (tests.test_dispatch_proof.DefectOneProbeTests) ... FAIL
test_probe_backdated_claim_via_api_rejected (tests.test_dispatch_proof.DefectOneProbeTests) ... ERROR
test_probe_backdated_claim_via_raw_authorized_sql_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
The same probe on the raw service-authorized insert path: the ... ERROR
test_raw_revision_with_caller_occurred_at_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
A revision whose occurred_at is anything but op_now() is refused, ... FAIL
test_api_takes_no_issuer_parameter (tests.test_block_authority.BlockAuthorityTests)
The issuer is service-owned: no typed-API entry point accepts an ... FAIL
test_caller_constructed_issuer_token_rejected_by_complete (tests.test_block_authority.BlockAuthorityTests)
Reviewer probe (B) against complete()'s owner-decision arm. ... ok
test_caller_constructed_issuer_token_rejected_by_resolve_block (tests.test_block_authority.BlockAuthorityTests)
Reviewer probe (B): a freshly constructed OwnerCapabilityIssuer() ... ERROR
test_capability_cannot_be_reserved_twice_concurrently (tests.test_block_authority.BlockAuthorityTests) ... ok
test_capability_forged_signature_rejected (tests.test_block_authority.BlockAuthorityTests) ... ok
test_capability_released_on_rollback_is_usable_again (tests.test_block_authority.BlockAuthorityTests)
Rollback/replay semantics: a reservation that never commits must ... ERROR
test_capability_single_use_after_commit (tests.test_block_authority.BlockAuthorityTests) ... ERROR
test_capability_wrong_scope_rejected (tests.test_block_authority.BlockAuthorityTests) ... ok
test_generic_transition_cannot_forge_resolution_fields (tests.test_block_authority.BlockAuthorityTests)
Even a transition that lands on a non-resolution-requiring arc ... ok
test_knowing_stored_decision_ref_text_cannot_forge_capability (tests.test_block_authority.BlockAuthorityTests)
A caller who only knows the plaintext decision_ref (readable via ... ok
test_tblock01_generic_transition_cannot_resolve_block (tests.test_block_authority.BlockAuthorityTests)
Caller reads the exact decision reference (it's visible via ... ok

======================================================================
ERROR: test_atomic_backup_renames_into_place (tests.test_v3r1_remediation.AtomicRoundtripWriteTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 249, in test_atomic_backup_renames_into_place
    with mock.patch.object(legacy.os, "replace", side_effect=real_replace) as spy:
AttributeError: module 'work_store.legacy' has no attribute 'os'

======================================================================
ERROR: test_failed_backup_leaves_existing_target_untouched (tests.test_v3r1_remediation.AtomicRoundtripWriteTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 264, in test_failed_backup_leaves_existing_target_untouched
    legacy.atomic_backup(src, target)
AttributeError: module 'work_store.legacy' has no attribute 'atomic_backup'

======================================================================
ERROR: test_prepare_legacy_downgrade_writes_roundtrip_db_via_rename (tests.test_v3r1_remediation.AtomicRoundtripWriteTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 275, in test_prepare_legacy_downgrade_writes_roundtrip_db_via_rename
    with mock.patch.object(legacy.os, "replace", side_effect=real_replace) as spy, \
AttributeError: module 'work_store.legacy' has no attribute 'os'

======================================================================
ERROR: test_claim_rejected_once_server_clock_passes_valid_until (tests.test_dispatch_proof.DefectOneProbeTests)
Same proof, judged at two server times: admitted before expiry,
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 206, in test_claim_rejected_once_server_clock_passes_valid_until
    a = self._item_created_2000()
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 138, in _item_created_2000
    with _clock("2000-01-01T00:00:00.000000Z"):
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/unittest/mock.py", line 1405, in __enter__
    original, local = self.get_original()
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/unittest/mock.py", line 1378, in get_original
    raise AttributeError(
AttributeError: <module 'work_store.db' from '/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/work_store/db.py'> does not have the attribute '_server_now'

======================================================================
ERROR: test_probe_backdated_claim_via_api_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 149, in test_probe_backdated_claim_via_api_rejected
    item = self._item_created_2000()
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 138, in _item_created_2000
    with _clock("2000-01-01T00:00:00.000000Z"):
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/unittest/mock.py", line 1405, in __enter__
    original, local = self.get_original()
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/unittest/mock.py", line 1378, in get_original
    raise AttributeError(
AttributeError: <module 'work_store.db' from '/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/work_store/db.py'> does not have the attribute '_server_now'

======================================================================
ERROR: test_probe_backdated_claim_via_raw_authorized_sql_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
The same probe on the raw service-authorized insert path: the
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 171, in test_probe_backdated_claim_via_raw_authorized_sql_rejected
    item = self._item_created_2000()
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 138, in _item_created_2000
    with _clock("2000-01-01T00:00:00.000000Z"):
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/unittest/mock.py", line 1405, in __enter__
    original, local = self.get_original()
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/unittest/mock.py", line 1378, in get_original
    raise AttributeError(
AttributeError: <module 'work_store.db' from '/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/work_store/db.py'> does not have the attribute '_server_now'

======================================================================
ERROR: test_caller_constructed_issuer_token_rejected_by_resolve_block (tests.test_block_authority.BlockAuthorityTests)
Reviewer probe (B): a freshly constructed OwnerCapabilityIssuer()
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_block_authority.py", line 124, in test_caller_constructed_issuer_token_rejected_by_resolve_block
    api.resolve_block(self.conn, item, 3, "DEC-20",
TypeError: resolve_block() missing 1 required positional argument: 'issuer'

======================================================================
ERROR: test_capability_released_on_rollback_is_usable_again (tests.test_block_authority.BlockAuthorityTests)
Rollback/replay semantics: a reservation that never commits must
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_block_authority.py", line 98, in test_capability_released_on_rollback_is_usable_again
    api.resolve_block(self.conn, item, 3, "DEC-11",
TypeError: resolve_block() missing 1 required positional argument: 'issuer'

======================================================================
ERROR: test_capability_single_use_after_commit (tests.test_block_authority.BlockAuthorityTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_block_authority.py", line 71, in test_capability_single_use_after_commit
    api.resolve_block(self.conn, item, 3, "DEC-10",
TypeError: resolve_block() missing 1 required positional argument: 'issuer'

======================================================================
FAIL: test_canonical_timestamp (tests.test_v3r1_remediation.CanonicalJsonTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 304, in test_canonical_timestamp
    self.assertEqual(canonical_timestamp("2026-10-05T02:00:00+02:00"),
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 27, in canonical_timestamp
    def canonical_timestamp(v): raise AssertionError("canonical_timestamp absent at 3c104dc")
AssertionError: canonical_timestamp absent at 3c104dc

======================================================================
FAIL: test_keys_colliding_after_normalization_rejected (tests.test_v3r1_remediation.CanonicalJsonTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 297, in test_keys_colliding_after_normalization_rejected
    canonical_json({"é": 1, "é": 2})
AssertionError: ValueError not raised

======================================================================
FAIL: test_keys_nfc_normalized (tests.test_v3r1_remediation.CanonicalJsonTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 293, in test_keys_nfc_normalized
    self.assertEqual(canonical_json({"é": "é"}), '{"é":"é"}')
AssertionError: '{"é":"é"}' != '{"é":"é"}'
- {"é":"é"}
?   ^^
+ {"é":"é"}
?   ^


======================================================================
FAIL: test_non_finite_numbers_rejected (tests.test_v3r1_remediation.CanonicalJsonTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 290, in test_non_finite_numbers_rejected
    canonical_json({"x": bad})
AssertionError: ValueError not raised

======================================================================
FAIL: test_non_string_keys_rejected (tests.test_v3r1_remediation.CanonicalJsonTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 301, in test_non_string_keys_rejected
    canonical_json({1: "a"})
AssertionError: ValueError not raised

======================================================================
FAIL: test_genuine_owner_decision_roundtrips_clean (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 336, in test_genuine_owner_decision_roundtrips_clean
    self.assertTrue(any(e["event_type"] == "verdict"
AssertionError: False is not true

======================================================================
FAIL: test_probe_capability_record_for_other_decision (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 360, in test_probe_capability_record_for_other_decision
    self.assertIn("I004", self._import_ids(self.projection))
AssertionError: 'I004' not found in []

======================================================================
FAIL: test_probe_capability_record_stripped (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 355, in test_probe_capability_record_stripped
    self.assertIn("I004", self._import_ids(self.projection))
AssertionError: 'I004' not found in []

======================================================================
FAIL: test_probe_forged_owner_decision_without_capability_record (tests.test_v3r1_remediation.ForgedOwnerDecisionImportTests)
Rewrite C's final-phase completion as an owner-decision exception
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 350, in test_probe_forged_owner_decision_without_capability_record
    self.assertIn("I004", self._import_ids(self.projection))
AssertionError: 'I004' not found in []

======================================================================
FAIL: test_probe_forged_exception_intact_schema (tests.test_v3r1_remediation.ForgedOwnerDecisionLedgerExtensionTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 389, in test_probe_forged_exception_intact_schema
    self.assertEqual(artifact_fingerprint(imported), reference_fingerprint())
AssertionError: 'a1c064b606a0021488b9fd4b92b8e24f81cc3fa5382bef15378ac59a16a199a7' != '4eabe44827d10b3532aa4f23b14c89bc31e25dd806d02c600eda9430c425e00e'
- a1c064b606a0021488b9fd4b92b8e24f81cc3fa5382bef15378ac59a16a199a7
+ 4eabe44827d10b3532aa4f23b14c89bc31e25dd806d02c600eda9430c425e00e


======================================================================
FAIL: test_cli_not_quiet_prints_counts (tests.test_v3r1_remediation.HeartbeatCliTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 152, in test_cli_not_quiet_prints_counts
    self.assertEqual(proc.returncode, EXIT_DIAGNOSTICS, proc.stdout + proc.stderr)
AssertionError: 0 != 20 : all quiet


======================================================================
FAIL: test_cli_prints_diagnostics_without_attribute_error (tests.test_v3r1_remediation.HeartbeatCliTests)
The old CLI printed d.id/d.reason (AttributeError on any
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 165, in test_cli_prints_diagnostics_without_attribute_error
    self.assertEqual(proc.returncode, EXIT_DIAGNOSTICS, proc.stdout + proc.stderr)
AssertionError: 0 != 20 : all quiet


======================================================================
FAIL: test_cli_query_failure_is_d000_exit_30 (tests.test_v3r1_remediation.HeartbeatCliTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 173, in test_cli_query_failure_is_d000_exit_30
    self.assertEqual(proc.returncode, EXIT_QUERY_FAILED, proc.stdout + proc.stderr)
AssertionError: 1 != 30 : Traceback (most recent call last):
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/runpy.py", line 197, in _run_module_as_main
    return _run_code(code, main_globals, None,
  File "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/runpy.py", line 87, in _run_code
    exec(code, run_globals)
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/work_store/cli.py", line 493, in <module>
    sys.exit(main())
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/work_store/cli.py", line 489, in main
    return args.func(args)
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/work_store/cli.py", line 263, in cmd_heartbeat
    conn = sqlite3.connect(args.db)
sqlite3.OperationalError: unable to open database file


======================================================================
FAIL: test_blocked_and_pollable_external_are_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 91, in test_blocked_and_pollable_external_are_not_quiet
    self.assertFalse(result.quiet)
AssertionError: True is not false

======================================================================
FAIL: test_both_passes_read_one_snapshot (tests.test_v3r1_remediation.HeartbeatSilenceTests)
A write committed by another connection between the two passes
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 129, in test_both_passes_read_one_snapshot
    self.assertEqual(calls, [(0, 0, 0, 0), (0, 0, 0, 0)])
AssertionError: Lists differ: [(0, 0, 0, 0), (0, 1, 0, 0)] != [(0, 0, 0, 0), (0, 0, 0, 0)]

First differing element 1:
(0, 1, 0, 0)
(0, 0, 0, 0)

- [(0, 0, 0, 0), (0, 1, 0, 0)]
?                    ---

+ [(0, 0, 0, 0), (0, 0, 0, 0)]
?                      +++


======================================================================
FAIL: test_p001_runs_without_a_pinned_fingerprint (tests.test_v3r1_remediation.HeartbeatSilenceTests)
P001 is mandatory: a schema-artifact drift is diagnosed even when
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 101, in test_p001_runs_without_a_pinned_fingerprint
    self.assertIn("P001", _ids(invariant_violations(conn, now_rfc3339())))
AssertionError: 'P001' not found in []

======================================================================
FAIL: test_probe_active_work_is_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
Reviewer probe ACTIVE_WORK (1,0,0,0).
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 79, in test_probe_active_work_is_not_quiet
    self.assertFalse(result.quiet)
AssertionError: True is not false

======================================================================
FAIL: test_probe_queued_work_is_not_quiet (tests.test_v3r1_remediation.HeartbeatSilenceTests)
Reviewer probe QUEUED_WORK (0,1,0,0): no diagnostics, but one
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 69, in test_probe_queued_work_is_not_quiet
    self.assertFalse(result.quiet)
AssertionError: True is not false

======================================================================
FAIL: test_import_preserves_full_trigger_set (tests.test_v3r1_remediation.ImportTriggerPreservationTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 197, in test_import_preserves_full_trigger_set
    self.assertIn(name, _triggers(imported))
AssertionError: 'revision_insert_contract' not found in {'schema_version_no_insert': "CREATE TRIGGER schema_version_no_insert BEFORE INSERT ON schema_version\n WHEN EXISTS(SELECT 1 FROM schema_version)\n BEGIN SELECT RAISE(ABORT,'immutable-schema-version'); END", 'schema_version_no_update': "CREATE TRIGGER schema_version_no_update BEFORE UPDATE ON schema_version\n BEGIN SELECT RAISE(ABORT,'immutable-schema-version'); END", 'schema_version_no_delete': "CREATE TRIGGER schema_version_no_delete BEFORE DELETE ON schema_version\n BEGIN SELECT RAISE(ABORT,'immutable-schema-version'); END", 'schema_migrations_no_update': "CREATE TRIGGER schema_migrations_no_update BEFORE UPDATE ON schema_migrations\n BEGIN SELECT RAISE(ABORT,'append-only-schema-migration'); END", 'schema_migrations_no_delete': "CREATE TRIGGER schema_migrations_no_delete BEFORE DELETE ON schema_migrations\n BEGIN SELECT RAISE(ABORT,'append-only-schema-migration'); END", 'status_transitions_no_insert': "CREATE TRIGGER status_transitions_no_insert BEFORE INSERT ON status_transitions\n BEGIN SELECT RAISE(ABORT,'immutable-status-policy'); END", 'status_transitions_no_update': "CREATE TRIGGER status_transitions_no_update BEFORE UPDATE ON status_transitions\n BEGIN SELECT RAISE(ABORT,'immutable-status-policy'); END", 'status_transitions_no_delete': "CREATE TRIGGER status_transitions_no_delete BEFORE DELETE ON status_transitions\n BEGIN SELECT RAISE(ABORT,'immutable-status-policy'); END", 'phase_gates_no_insert': "CREATE TRIGGER phase_gates_no_insert BEFORE INSERT ON phase_gates\n BEGIN SELECT RAISE(ABORT,'immutable-phase-policy'); END", 'phase_gates_no_update': "CREATE TRIGGER phase_gates_no_update BEFORE UPDATE ON phase_gates\n BEGIN SELECT RAISE(ABORT,'immutable-phase-policy'); END", 'phase_gates_no_delete': "CREATE TRIGGER phase_gates_no_delete BEFORE DELETE ON phase_gates\n BEGIN SELECT RAISE(ABORT,'immutable-phase-policy'); END", 'work_items_no_update': "CREATE TRIGGER work_items_no_update BEFORE UPDATE ON work_items\n BEGIN SELECT RAISE(ABORT,'append-only-item-identity'); END", 'work_items_no_delete': "CREATE TRIGGER work_items_no_delete BEFORE DELETE ON work_items\n BEGIN SELECT RAISE(ABORT,'append-only-item-identity'); END", 'revisions_no_update': "CREATE TRIGGER revisions_no_update BEFORE UPDATE ON item_revisions\n BEGIN SELECT RAISE(ABORT,'append-only-item-revision'); END", 'revisions_no_delete': "CREATE TRIGGER revisions_no_delete BEFORE DELETE ON item_revisions\n BEGIN SELECT RAISE(ABORT,'append-only-item-revision'); END", 'proofs_no_update': "CREATE TRIGGER proofs_no_update BEFORE UPDATE ON dispatch_proofs\n BEGIN SELECT RAISE(ABORT,'append-only-dispatch-proof'); END", 'proofs_no_delete': "CREATE TRIGGER proofs_no_delete BEFORE DELETE ON dispatch_proofs\n BEGIN SELECT RAISE(ABORT,'append-only-dispatch-proof'); END", 'dependencies_no_update': "CREATE TRIGGER dependencies_no_update BEFORE UPDATE ON work_item_dependencies\n BEGIN SELECT RAISE(ABORT,'append-only-dependency'); END", 'dependencies_no_delete': "CREATE TRIGGER dependencies_no_delete BEFORE DELETE ON work_item_dependencies\n BEGIN SELECT RAISE(ABORT,'append-only-dependency'); END", 'events_no_update': "CREATE TRIGGER events_no_update BEFORE UPDATE ON item_events\n BEGIN SELECT RAISE(ABORT,'append-only-event-log'); END", 'events_no_delete': "CREATE TRIGGER events_no_delete BEFORE DELETE ON item_events\n BEGIN SELECT RAISE(ABORT,'append-only-event-log'); END"}

======================================================================
FAIL: test_queued_to_active_without_proof_rejected_after_import (tests.test_v3r1_remediation.ImportTriggerPreservationTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 212, in test_queued_to_active_without_proof_rejected_after_import
    self.assertEqual(conn.execute("SELECT event_type FROM item_events WHERE item_id='Q'")
AssertionError: Lists differ: [] != [('created',)]

Second list contains 1 additional elements.
First extra element 0:
('created',)

- []
+ [('created',)]

======================================================================
FAIL: test_legitimate_successor_advancement_is_not_i007 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests)
Completion, then api.claim of the child: the successor was fresh
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 435, in test_legitimate_successor_advancement_is_not_i007
    self.assertEqual(invariant_violations(conn, now_rfc3339()), [])
AssertionError: Lists differ: [Diagnostic(diag_id='I007', subject='item-[53 chars]cf')] != []

First list contains 1 additional elements.
First extra element 0:
Diagnostic(diag_id='I007', subject='item-627a44f3', detail='9c373da6-0740-4344-9c9c-cfa97701bacf')

- [Diagnostic(diag_id='I007', subject='item-627a44f3', detail='9c373da6-0740-4344-9c9c-cfa97701bacf')]
+ []

======================================================================
FAIL: test_probe_null_successor_ownership_is_i008 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests) (column='successor_owner_item_id')
Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 426, in test_probe_null_successor_ownership_is_i008
    self.assertIn("I008", _ids(invariant_violations(conn, now_rfc3339())))
AssertionError: 'I008' not found in []

======================================================================
FAIL: test_probe_null_successor_ownership_is_i008 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests) (column='successor_owner_revision')
Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 426, in test_probe_null_successor_ownership_is_i008
    self.assertIn("I008", _ids(invariant_violations(conn, now_rfc3339())))
AssertionError: 'I008' not found in []

======================================================================
FAIL: test_probe_null_successor_ownership_is_i008 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests) (column='successor_owner_nonce')
Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 426, in test_probe_null_successor_ownership_is_i008
    self.assertIn("I008", _ids(invariant_violations(conn, now_rfc3339())))
AssertionError: 'I008' not found in []

======================================================================
FAIL: test_probe_null_successor_ownership_is_i008 (tests.test_v3r1_remediation.SuccessorInvariantScopeTests) (column=None)
Reviewer probe NULL_SUCCESSOR_OWNERSHIP: unknown is not satisfied.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_v3r1_remediation.py", line 426, in test_probe_null_successor_ownership_is_i008
    self.assertIn("I008", _ids(invariant_violations(conn, now_rfc3339())))
AssertionError: 'I008' not found in []

======================================================================
FAIL: test_naive_valid_until_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 218, in test_naive_valid_until_rejected
    api.claim(self.conn, item, 1, dict(self.PROOF, valid_until="2999-01-01T00:00:00"),
AssertionError: OperationError not raised

======================================================================
FAIL: test_raw_revision_with_caller_occurred_at_rejected (tests.test_dispatch_proof.DefectOneProbeTests)
A revision whose occurred_at is anything but op_now() is refused,
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_dispatch_proof.py", line 197, in test_raw_revision_with_caller_occurred_at_rejected
    self.raw_authorized_insert("transition", item, lambda ctx: [(
AssertionError: OperationError not raised

======================================================================
FAIL: test_api_takes_no_issuer_parameter (tests.test_block_authority.BlockAuthorityTests)
The issuer is service-owned: no typed-API entry point accepts an
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/brentthomas1/claude-workers/work-state-store-v3r1-remediation-build/baseline-3c104dc/tests/test_block_authority.py", line 152, in test_api_takes_no_issuer_parameter
    self.assertNotIn("issuer", inspect.signature(fn).parameters, fn.__name__)
AssertionError: 'issuer' unexpectedly found in mappingproxy(OrderedDict([('conn', <Parameter "conn: 'sqlite3.Connection'">), ('item_id', <Parameter "item_id: 'str'">), ('expected_revision', <Parameter "expected_revision: 'int'">), ('exact_ref', <Parameter "exact_ref: 'str'">), ('resolution', <Parameter "resolution: 'dict'">), ('owner_capability', <Parameter "owner_capability: 'str'">), ('issuer', <Parameter "issuer: 'OwnerCapabilityIssuer'">), ('occurred_at', <Parameter "occurred_at: 'Optional[str]' = None">)])) : resolve_block

----------------------------------------------------------------------
Ran 45 tests in 1.276s

FAILED (failures=28, errors=9)
exit=1
```
