#!/usr/bin/env python3
"""Reproduce baseline/defect evidence in disposable copies; never edit the input source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PINS = {
    'review_gate.py': '70f037b0bac7199b36477a5bfc6c7e90b06482b8a8c4de79571bd998bdeef4a5',
    'pr_controller.py': 'a90a5561cea29bf45a1acd48de10aa4e73ca1ac229257426e5128ad98f34850a',
    'review_dedup.py': 'd4c97bfd585872843fdaf11fe086e69b88bcb4939ec6f43fe4c88378fa1064ec',
    'review_findings.py': 'e40fb7200ba657cefb5aa2883cc610e90e6da88c19ea97dab4ad71919645bced',
    'firstpass_review.py': '83f8d15576a74e45e8ffc407cb945e6ee1cc4568e8a79a4cb731edabf690c8b2',
    'review_gate_runtime.py': 'adbe2b9411c56a21dcbbb4ef38d1ca05793f2586ce9e54fe78d02dbab0390ed1',
    'test_review_gate.py': '5fc98b7072b9a62eadada6055cb4db77eaa14a92fd5285eba727b113b6fd6e45',
    'test_review_gate_fairness.py': 'e14f5e6a416fbe24f36dc1c4a8c2574c0a8108cf3bc7b11f7a0ccf0d05d88cbc',
}
FAILURES = {'test_contract.FailureSignalTests.test_cli_failure_is_nonzero_not_just_logged',
            'test_contract.FailureSignalTests.test_tick_failure_has_failed_repo_signal_and_non_success'}
# name, member, exact old/new text, detecting case. These are test controls, not candidate repairs.
MUTANTS = [
    ('first-page-only', 'pr_controller.py', "if not page['hasNextPage']:", 'if True:',
     'InventoryTests.test_gate_uses_paginated_helper_and_late_page_candidate'),
    ('partial-return-on-error', 'pr_controller.py',
     "raise ValueError('GraphQL returned errors; partial inventory rejected')", 'return repo',
     'InventoryTests.test_failed_repository_cannot_dispatch_partial_but_healthy_repo_continues'),
    ('graphql-error-ignored', 'pr_controller.py', 'if payload.get(\'errors\'):', 'if False:',
     'InventoryTests.test_errors_and_malformed_later_pages_never_return_partial'),
    ('repeated-cursor-accepted', 'pr_controller.py', 'if not cursor or cursor in cursors:', 'if not cursor:',
     'InventoryTests.test_repeated_cursor_refuses_partial_inventory'),
    ('page-bound-partial-return', 'pr_controller.py',
     "raise ValueError('PR inventory exceeded pagination bound')", 'return repo',
     'InventoryTests.test_page_bound_exhaustion_refuses_partial_inventory'),
    ('rank-global-before-cut', 'review_gate.py', 'due = fair_order(due, _info, _prio)',
     'due.sort(key=lambda d: due_rank(*_info[d[0]], _prio))',
     'FairnessTests.test_round_robin_precedes_existing_batch_cut'),
    ('batch-cut-before-firstpass', 'review_gate.py', 'due = fair_order(due, _info, _prio)',
     'due = fair_order(due, _info, _prio)[:BATCH]',
     'FairnessTests.test_firstpass_filter_does_not_move_batch_cut_before_fairness'),
    ('exact-head-bypassed', 'review_gate.py', "if commit.get('oid') != head:", 'if False:',
     'GuardTests.test_exact_head_draft_truncated_and_required_ci_guards'),
    ('draft-bypassed', 'review_gate.py', "if pr['isDraft']:", 'if False:',
     'GuardTests.test_exact_head_draft_truncated_and_required_ci_guards'),
    ('required-check-bypassed', 'review_gate.py',
     'return all((results.get(r) is True for r in required))', 'return True',
     'GuardTests.test_check_interpretation_and_latest_attempt'),
    ('dedup-bypassed', 'review_gate.py', "if st != 'retryable':", 'if False:',
     'GuardTests.test_reviewed_live_queued_dedup_and_backoff_exclude_only_matching_heads'),
    ('live-run-bypassed', 'review_gate.py',
     "if (full.lower(), pr['number'], head) in reviewed or (full.lower(), pr['number']) in busy:", 'if False:',
     'GuardTests.test_reviewed_live_queued_dedup_and_backoff_exclude_only_matching_heads'),
    ('backoff-bypassed', 'review_gate.py', 'if now - state.get(key, 0) < RETRY_AFTER:', 'if False:',
     'GuardTests.test_reviewed_live_queued_dedup_and_backoff_exclude_only_matching_heads'),
    ('reviewer-fence-bypassed', 'review_gate.py', 'if not gate_admit(_inflight, _free):', 'if False:',
     'GuardTests.test_reviewer_load_and_inflight_fences'),
    ('hold-bypassed', 'review_gate.py', 'if runtime.REVIEW_HOLD.exists():', 'if False:',
     'GuardTests.test_pause_and_hold_stop_before_inventory'),
    ('host-threshold-bypassed', 'firstpass_review.py',
     'return float(cpu60) < CPU_SOME60_MAX and float(load5) < LOAD5_CEILING and (float(home_pct) < HOME_PCT_MAX) and (float(max5h) < META_5H_MAX)',
     'return True', 'GuardTests.test_existing_host_and_provider_thresholds_without_inventing_final_review_guard'),
    ('silent-success-failure', 'review_gate.py', "if __name__ == '__main__':",
     "_offline_original_main = main\ndef main():\n    try:\n        _offline_original_main()\n    except SystemExit:\n        pass\n    return 0\n\nif __name__ == '__main__':",
     'FailureSignalTests.test_tick_failure_has_failed_repo_signal_and_non_success'),
]


def hashes(source):
    return {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in PINS}


def run(source, output, label, selections=(), legacy=False):
    command = [sys.executable, '-B', str(Path(__file__).with_name('run.py')),
               '--source-dir', str(source), '--json', str(output / (label + '.json'))]
    for selection in selections:
        command += ['--select', selection]
    if legacy:
        command += ['--legacy']
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    (output / (label + '.log')).write_text(result.stdout + result.stderr)
    report = json.loads((output / (label + '.json')).read_text())
    return {'label': label, 'exit': result.returncode, 'tests': report['tests'],
            'passed': len(report['passed']), 'failures': [f['case'] for f in report['failures']],
            'errors': [e['case'] for e in report['errors']], 'skipped': report['skipped']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True, help='exact pinned sanitized bundle')
    parser.add_argument('--output-dir', type=Path, required=True, help='new evidence directory')
    args = parser.parse_args()
    source = args.source_dir.resolve(strict=True)
    before = hashes(source)
    if before != PINS:
        parser.error('mutation replay requires the exact sanitized baseline pins, not a Builder candidate')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = run(source, output, 'baseline', legacy=True)
    assert baseline['exit'] == 1 and set(baseline['failures']) == FAILURES, baseline
    assert not baseline['errors'] and not baseline['skipped'], baseline
    results = [baseline]
    scratch = os.environ.get('PAPERCLIP_RUN_SCRATCH_DIR') or os.environ.get('PAPERCLIP_SCRATCH_DIR')
    if not scratch:
        parser.error('set an owned Paperclip scratch directory')
    with tempfile.TemporaryDirectory(prefix='review-gate-mutants-', dir=scratch) as temporary:
        for name, member, old, new, case in MUTANTS:
            target = Path(temporary) / name
            shutil.copytree(source, target)
            path = target / member
            text = path.read_text()
            assert text.count(old) == 1, (name, 'mutation anchor changed')
            path.write_text(text.replace(old, new))
            row = run(target, output, name, [case])
            expected = 'test_contract.' + case
            assert row['exit'] == 1 and any(f == expected or f.startswith(expected + ' (')
                                           for f in row['failures']), row
            assert not row['errors'] and not row['skipped'], row
            row['baseline_already_red'] = name == 'silent-success-failure'
            results.append(row)
            print(name + ': DETECTED (' + case + ')')
    assert hashes(source) == before, 'input baseline was modified'
    # Re-run all previously green contracts on the untouched source after removing mutant copies.
    clean = run(source, output, 'restored',
                ['InventoryTests', 'FairnessTests', 'GuardTests', 'DedupTests', 'HarnessTests'], legacy=True)
    assert clean['exit'] == 0 and not clean['errors'] and not clean['skipped'], clean
    results.append(clean)
    (output / 'matrix.json').write_text(json.dumps({'source_sha256': before, 'results': results}, indent=2) + '\n')
    print(f"baseline: {baseline['passed']}/{baseline['tests']} pass; exactly two deliberate loudness failures")
    print(f"restored: {clean['passed']}/{clean['tests']} pass; {len(MUTANTS)}/{len(MUTANTS)} controls detected")
    return 0


if __name__ == '__main__':
    sys.exit(main())
