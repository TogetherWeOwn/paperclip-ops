"""Independent offline acceptance; imports resolve only through run.py --source-dir."""
import ast
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import firstpass_review as fp
import pr_controller as controller
import review_dedup as dedup
import review_gate as gate

A = 'example-org/primary-service-a'
B = 'example-org/primary-service-b'
NOW = 2000000000
HEAD = 'a' * 40


def check(name='ci-ok', **kw):
    return {'name': name, 'status': 'COMPLETED', 'conclusion': 'SUCCESS', **kw}


def pr(number, **kw):
    return {'number': number, 'isDraft': False, 'headRefOid': HEAD,
            'isCrossRepository': False,
            'author': {'__typename': 'User', 'login': 'example-contributor'},
            'url': f'https://example.invalid/pull/{number}',
            'headRef': {'target': {'oid': HEAD, 'statusCheckRollup': {
                'contexts': {'pageInfo': {'hasNextPage': False}, 'nodes': [check()]}}}}, **kw}


def page(nodes, more=False, cursor=None):
    return {'data': {'repository': {'isArchived': False, 'pullRequests': {
        'nodes': nodes, 'totalCount': 999,
        'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}


class Pages:
    """Mock only gh transport: never replace the deployed fetch_repository helper."""
    def __init__(self, repositories):
        self.repositories = deepcopy(repositories)
        self.calls = []
        self.indices = {}

    def __call__(self, args, env):
        fields = dict(a.split('=', 1) for a in args if '=' in a)
        full = fields['o'] + '/' + fields['r']
        index = self.indices.get(full, 0)
        self.indices[full] = index + 1
        self.calls.append((full, fields.get('cursor'), fields['query'], dict(env)))
        payloads = self.repositories[full]
        if index >= len(payloads):
            raise AssertionError('inventory exceeded fixture pages')
        payload = payloads[index]
        return payload if isinstance(payload, str) else json.dumps(payload)


class GateFixture:
    """In-memory DB/dispatch plus scratch-only state; no service, token or host reads."""
    def __init__(self, case, repositories=None, **options):
        self.case = case
        self.repositories = repositories or {A: [page([pr(1)])]}
        self.pages = Pages(self.repositories)
        self.options = options
        self.logs = []
        self.batches = []
        self.sql = []
        self.dedup_pairs = []
        self.stack = ExitStack()
        case.addCleanup(self.stack.close)
        self.root = Path(tempfile.mkdtemp(prefix='tick-', dir=gate.runtime.ROOT))
        self.state = self.root / 'state.json'
        if 'state' in options:
            self.state.write_text(json.dumps(options['state']))
        self.set(gate, 'STATE', self.state)
        self.set(gate, 'PAUSE', self.root / 'PAUSE')
        self.set(gate.runtime, 'REVIEW_HOLD', self.root / 'HOLD')
        self.set(gate, 'log', self.logs.append)
        self.set(gate, 'q', self.query)
        self.set(gate, 'sh', self.command)
        self.set(gate, 'scan_docs_lane', lambda **kw: ({'eligible': [], 'errors': []}, set()))
        self.set(gate, 'required_checks', lambda full, env: {'ci-ok'})
        self.set(gate, 'reviewer_active', lambda: options.get('active', 0))
        self.set(gate, 'gate_batches_inflight', lambda: options.get('inflight', 0))
        self.stack.enter_context(patch.object(gate.time, 'time', return_value=NOW))
        self.set(controller, 'gh', self.pages)
        self.set(dedup, 'coverage', self.coverage)
        self.set(gate.findings, 'recent_round2', lambda since: [])
        self.set(gate.findings, 'rounds_info', lambda pairs: {})
        self.set(gate.findings, 'in_scope', lambda full: False)
        self.set(fp, 'load_config', lambda: {'enabled': options.get('firstpass', False),
                 'repos': ['primary-service-a', 'primary-service-b'],
                 'priority_prs': options.get('priority', [])})
        self.set(fp, 'PAUSE', self.root / 'FIRSTPASS_PAUSE')
        self.set(fp, 'meta_records', lambda: [])
        self.set(fp, 'meta_available', lambda records: options.get('meta', True))
        self.set(fp, 'host_readings', lambda records: (1, 20, 0.1, 1))
        self.set(fp, 'guards', lambda *args: options.get('guards', True))
        self.set(fp, 'status', lambda full, n, head: options.get('fp_status', {}).get((full, n)))

    def set(self, obj, name, value):
        self.stack.enter_context(patch.object(obj, name, value))

    def query(self, sql):
        self.sql.append(sql)
        if 'maxConcurrentRuns' in sql:
            return [[str(self.options.get('mcr', 4))]]
        if "status='queued'" in sql and 'count(*)' in sql:
            return [[str(self.options.get('queued', 0))]]
        if 'provider_resource_id from chat_endpoint_resources' in sql:
            return [[r] for r in self.repositories]
        if 'select lower(g.repository), g.pull_number, g.head_sha' in sql:
            return self.options.get('reviewed', [])
        if 'select lower(g.repository), g.pull_number::text' in sql:
            return self.options.get('busy', [])
        if 'select lower(split_part' in sql:
            return self.options.get('queued_busy', [])
        if 'select project_id' in sql:
            return [['00000000-0000-0000-0000-000000000005']]
        raise AssertionError('unexpected SQL fixture route: ' + sql)

    def command(self, args, **kw):
        if args == gate.runtime.TOKEN_COMMAND:
            return subprocess.CompletedProcess(args, 0, 'offline-placeholder', '')
        if args[0] == 'curl':
            body = json.loads(kw['input'])
            self.batches.append(body)
            receipt = {**body, 'identifier': 'EXAMPLE-2'}
            if self.options.get('bad_receipt'):
                receipt['title'] = 'mismatched receipt'
            return subprocess.CompletedProcess(args, 0, json.dumps(receipt), '')
        raise AssertionError('unexpected command fixture route')

    def coverage(self, pairs):
        self.dedup_pairs.extend(pairs)
        if self.options.get('dedup_error'):
            raise RuntimeError('offline dedup unreadable')
        return {(full.lower(), n, head.lower()): self.options.get('coverage', {}).get((full, n), 'retryable')
                for full, n, head in pairs}

    def run(self, cli=False):
        try:
            if cli:
                tree = ast.parse(Path(gate.__file__).read_text())
                entry = [node for node in tree.body if isinstance(node, ast.If)
                         and isinstance(node.test, ast.Compare)
                         and ast.unparse(node.test) == "__name__ == '__main__'"]
                self.case.assertEqual(len(entry), 1, 'one executable CLI entrypoint required')
                # Execute the real CLI block with the mocked module globals, not a substitute CLI.
                namespace = {**vars(gate), '__name__': '__main__'}
                with patch.object(sys, 'argv', [gate.__file__]):
                    exec(compile(ast.Module(body=entry, type_ignores=[]), '<gate-cli>', 'exec'), namespace)
                return None
            return gate.main()
        except SystemExit as exc:
            return exc.code

    def numbers(self):
        import re
        return [int(n) for body in self.batches
                for n in re.findall(r'https://example.invalid/pull/(\d+) \(head ', body['description'])]


class InventoryTests(unittest.TestCase):
    def fetch(self, pages):
        transport = Pages({A: pages})
        with patch.object(controller, 'gh', transport):
            result = controller.fetch_repository(A, {'OFFLINE': '1'}, query=gate.QUERY)
        return result, transport

    def test_three_pages_all_unique_numbers_and_late_candidate(self):
        result, transport = self.fetch([page([pr(n) for n in range(1, 21)], True, 'p1'),
                                       page([pr(n) for n in range(20, 40)], True, 'p2'),
                                       page([pr(n) for n in range(40, 47)])])
        self.assertEqual([p['number'] for p in result['pullRequests']['nodes']], list(range(1, 47)))
        self.assertEqual(result['pullRequests']['totalCount'], 46)
        self.assertEqual([c[1] for c in transport.calls], [None, 'p1', 'p2'])
        for _, _, query, env in transport.calls:
            self.assertIn('first:20,after:$cursor', query)
            self.assertEqual(query, gate.QUERY)
            self.assertEqual(env, {'OFFLINE': '1'})

    def test_invalid_next_cursors_refuse_partial_inventory(self):
        for cursor in (None, ''):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.fetch([page([pr(1)], True, cursor)])
        missing = page([pr(1)], True)
        del missing['data']['repository']['pullRequests']['pageInfo']['endCursor']
        with self.assertRaises(ValueError):
            self.fetch([missing])

    def test_repeated_cursor_refuses_partial_inventory(self):
        with self.assertRaises(ValueError):
            self.fetch([page([pr(1)], True, 'same'), page([pr(2)], True, 'same'), page([pr(3)])])

    def test_page_bound_exhaustion_refuses_partial_inventory(self):
        transport = Pages({A: [page([pr(n)], True, f'p{n}') for n in range(100)]})
        with patch.object(controller, 'gh', transport), self.assertRaises(ValueError):
            controller.fetch_repository(A, {})
        self.assertEqual(len(transport.calls), 100)

    def test_errors_and_malformed_later_pages_never_return_partial(self):
        broken = [ {'errors': [{'message': 'offline error'}], **page([pr(2)])},
                   {}, {'data': {}}, {'data': {'repository': {'isArchived': False}}},
                   {'data': {'repository': {'pullRequests': {'nodes': None}}}}, '{invalid json']
        for payload in broken:
            with self.subTest(payload=payload), self.assertRaises((ValueError, KeyError, TypeError)):
                self.fetch([page([pr(1)], True, 'p1'), payload])

    def test_null_and_archived_repositories(self):
        result, _ = self.fetch([{'data': {'repository': None}}])
        self.assertIsNone(result)
        archived = page([])
        archived['data']['repository']['isArchived'] = True
        result, _ = self.fetch([archived])
        self.assertTrue(result['isArchived'])

    def test_gate_uses_paginated_helper_and_late_page_candidate(self):
        f = GateFixture(self, {A: [page([pr(n, isDraft=True) for n in range(1, 21)], True, 'p1'),
                                  page([pr(n, isDraft=True) for n in range(21, 41)], True, 'p2'),
                                  page([pr(41)])]})
        f.run()
        self.assertEqual(f.numbers(), [41])
        self.assertEqual([c[1] for c in f.pages.calls], [None, 'p1', 'p2'])
        self.assertEqual(f.dedup_pairs, [(A, 41, HEAD)])

    def test_failed_repository_cannot_dispatch_partial_but_healthy_repo_continues(self):
        f = GateFixture(self, {A: [page([pr(1)], True, 'p1'), {'errors': [{'message': 'offline'}]}],
                               B: [page([pr(99)])]})
        f.run()
        self.assertEqual(f.numbers(), [99])
        self.assertEqual(f.dedup_pairs, [(B, 99, HEAD)])
        self.assertTrue(any(A in line and 'failed' in line.lower() for line in f.logs))
        saved = json.loads(f.state.read_text())
        self.assertNotIn(f'{A}#1@{HEAD}', saved)
        self.assertIn(f'{B}#99@{HEAD}', saved)


class CandidateAdmissionTests(unittest.TestCase):
    """Independent source-only contract; run with run.py --source-dir ops/review_gate --legacy.

    The GraphQL boundary supplies PullRequest.isCrossRepository: Boolean! and
    author: Actor (login: String!, concrete __typename). Only explicit False and
    a known User with a nonblank string login qualify. Bot and unknown actor
    types never qualify; login substrings are not an actor-type oracle.
    Schema: https://docs.github.com/en/graphql/reference/pulls
            https://docs.github.com/en/graphql/reference/users
    """

    def admission_state(self, fixture):
        state = json.loads(fixture.state.read_text()) if fixture.state.exists() else {}
        # The existing background findings sweep timestamp is not admission evidence.
        state.pop('__findings_postcheck', None)
        return state

    def assert_rejected(self, candidate, *, neighbor=False, **options):
        # Catch admission/receipt/state writes, not just a predicate's return value.
        sentinel = f'{B}#90@{HEAD}'
        nodes = [candidate, pr(99)] if neighbor else [candidate]
        f = GateFixture(self, {A: [page(nodes)]}, state={sentinel: NOW - 1}, **options)
        f.run()
        expected = [99] if neighbor else []
        self.assertEqual(f.numbers(), expected, 'ineligible heads must not request a review')
        self.assertEqual(len(f.batches), int(neighbor), 'no dispatch/receipt for a rejected head')
        saved = self.admission_state(f)
        expected_state = {sentinel: NOW - 1}
        if neighbor:
            expected_state[f'{A}#99@{HEAD}'] = NOW
        self.assertEqual(saved, expected_state, 'skip is not requested, covered, or favorable evidence')
        f.stack.close()

    def test_cross_repository_same_oid_and_green_ci_never_dispatches(self):
        self.assert_rejected(pr(1, isCrossRepository=True))

    def test_bot_same_repository_same_oid_and_green_ci_never_dispatches(self):
        self.assert_rejected(pr(1, author={'__typename': 'Bot', 'login': 'release-maintainer'}))

    def test_rejected_candidates_do_not_starve_valid_late_page_head(self):
        f = GateFixture(self, {A: [page([pr(1, isCrossRepository=True),
                                        pr(2, author={'__typename': 'Bot', 'login': 'release-maintainer'})],
                                       True, 'next'), page([pr(3)])]})
        f.run()
        self.assertEqual(f.numbers(), [3])
        self.assertEqual(len(f.batches), 1)
        self.assertEqual(self.admission_state(f), {f'{A}#3@{HEAD}': NOW})
        self.assertEqual([c[1] for c in f.pages.calls], [None, 'next'])

    def test_same_repository_user_exact_green_head_is_requested_and_marked(self):
        f = GateFixture(self, {A: [page([pr(1)])]})
        f.run()
        self.assertEqual(f.numbers(), [1])
        self.assertEqual(len(f.batches), 1)
        self.assertEqual(f.dedup_pairs, [(A, 1, HEAD)])
        self.assertEqual(self.admission_state(f), {f'{A}#1@{HEAD}': NOW})

    def test_user_login_containing_bot_is_not_a_bot_type(self):
        for login in ('botanist', 'robotics-maintainer', 'build-bot-maintainer'):
            with self.subTest(login=login):
                f = GateFixture(self, {A: [page([pr(1, author={'__typename': 'User', 'login': login})])]})
                f.run()
                self.assertEqual(f.numbers(), [1])
                self.assertEqual(self.admission_state(f), {f'{A}#1@{HEAD}': NOW})
                f.stack.close()

    def test_inventory_query_requests_authoritative_metadata_on_every_page(self):
        import re
        f = GateFixture(self, {A: [page([pr(1, isDraft=True)], True, 'next'), page([pr(2)])]})
        f.run()
        self.assertEqual(f.numbers(), [2], 'observe the real candidate-path inventory request')
        self.assertEqual(len(f.pages.calls), 2)
        for _, _, query, _ in f.pages.calls:
            with self.subTest(query=query):
                self.assertRegex(query, r'\bisCrossRepository\b', 'fetch authoritative fork metadata')
                authors = re.findall(r'\bauthor\s*\{([^{}]*)\}', query)
                self.assertTrue(authors, 'fetch Actor metadata at the transport boundary')
                self.assertTrue(any(re.search(r'\b__typename\b', fields) and re.search(r'\blogin\b', fields)
                                    for fields in authors), 'fetch schema-supported Actor type and identity together')

    def test_missing_cross_repository_metadata_fails_closed(self):
        candidate = pr(1)
        del candidate['isCrossRepository']
        self.assert_rejected(candidate, neighbor=True)

    def test_null_cross_repository_metadata_fails_closed(self):
        self.assert_rejected(pr(1, isCrossRepository=None), neighbor=True)

    def test_non_boolean_cross_repository_metadata_fails_closed(self):
        for value in ('false', 'true', '', 0, 1, [], {}):
            with self.subTest(value=value):
                self.assert_rejected(pr(1, isCrossRepository=value), neighbor=True)

    def test_missing_author_fails_closed(self):
        candidate = pr(1)
        del candidate['author']
        self.assert_rejected(candidate, neighbor=True)

    def test_null_author_fails_closed(self):
        self.assert_rejected(pr(1, author=None), neighbor=True)

    def test_non_object_author_fails_closed(self):
        for author in ('example-contributor', False, 0, []):
            with self.subTest(author=author):
                self.assert_rejected(pr(1, author=author), neighbor=True)

    def test_missing_actor_type_fails_closed(self):
        self.assert_rejected(pr(1, author={'login': 'example-contributor'}), neighbor=True)

    def test_null_actor_type_fails_closed(self):
        self.assert_rejected(pr(1, author={'__typename': None, 'login': 'example-contributor'}), neighbor=True)

    def test_malformed_actor_type_fails_closed(self):
        for typename in ('', 'user', ' User ', False, 0, [], {}):
            with self.subTest(typename=typename):
                self.assert_rejected(pr(1, author={'__typename': typename, 'login': 'example-contributor'}),
                                     neighbor=True)

    def test_unknown_actor_types_fail_closed(self):
        for typename in ('Organization', 'Mannequin', 'EnterpriseUserAccount', 'FutureActor'):
            with self.subTest(typename=typename):
                self.assert_rejected(pr(1, author={'__typename': typename, 'login': 'example-contributor'}),
                                     neighbor=True)

    def test_missing_author_identity_fails_closed(self):
        self.assert_rejected(pr(1, author={'__typename': 'User'}), neighbor=True)

    def test_null_author_identity_fails_closed(self):
        self.assert_rejected(pr(1, author={'__typename': 'User', 'login': None}), neighbor=True)

    def test_blank_author_identity_fails_closed(self):
        for login in ('', ' ', '\t\n'):
            with self.subTest(login=login):
                self.assert_rejected(pr(1, author={'__typename': 'User', 'login': login}), neighbor=True)

    def test_non_string_author_identity_fails_closed(self):
        for login in (False, 0, 123, [], {}):
            with self.subTest(login=login):
                self.assert_rejected(pr(1, author={'__typename': 'User', 'login': login}), neighbor=True)

    def test_firstpass_favorable_status_does_not_exempt_rejected_candidates(self):
        for candidate in (pr(1, isCrossRepository=True),
                          pr(1, author={'__typename': 'Bot', 'login': 'release-maintainer'})):
            with self.subTest(candidate=candidate):
                self.assert_rejected(candidate, firstpass=True, fp_status={(A, 1): 'passed'})

    def test_new_head_cannot_reuse_old_head_green_ci_or_favorable_review(self):
        new_head = 'b' * 40
        old_ci = pr(1, headRefOid=new_head)
        missing_ci = deepcopy(old_ci)
        missing_ci['headRef']['target']['oid'] = new_head
        missing_ci['headRef']['target']['statusCheckRollup']['contexts']['nodes'] = []
        for candidate in (old_ci, missing_ci):
            with self.subTest(candidate=candidate):
                self.assert_rejected(candidate, reviewed=[[A, '1', HEAD]])

    def test_old_head_review_and_backoff_do_not_cover_valid_new_head(self):
        new_head = 'b' * 40
        candidate = pr(1, headRefOid=new_head)
        candidate['headRef']['target']['oid'] = new_head
        old_key = f'{A}#1@{HEAD}'
        f = GateFixture(self, {A: [page([candidate])]}, reviewed=[[A, '1', HEAD]],
                        state={old_key: NOW - 1})
        f.run()
        self.assertEqual(f.numbers(), [1])
        self.assertEqual(f.dedup_pairs, [(A, 1, new_head)])
        self.assertEqual(self.admission_state(f), {old_key: NOW - 1, f'{A}#1@{new_head}': NOW})

    def test_red_or_missing_required_ci_still_blocks_valid_user(self):
        for contexts in ([check(conclusion='FAILURE')], [], [check('unrelated')]):
            with self.subTest(contexts=contexts):
                candidate = pr(1)
                candidate['headRef']['target']['statusCheckRollup']['contexts']['nodes'] = contexts
                self.assert_rejected(candidate)


class FailureSignalTests(unittest.TestCase):
    def failed_tick(self, cli):
        f = GateFixture(self, {A: [page([pr(1)], True, 'p1'), {'errors': [{'message': 'offline'}]}],
                               B: [page([pr(99)])]})
        result = f.run(cli=cli)
        self.assertEqual(f.numbers(), [99], 'healthy repositories remain isolated from inventory failure')
        self.assertTrue(any(A in line and 'failed' in line.lower() for line in f.logs),
                        'failed repository must be named explicitly')
        self.assertIs(type(result), int, 'log-and-success-only is not an approved alert path')
        self.assertGreater(result, 0, 'inventory failure must yield a non-success tick/CLI result')

    def test_tick_failure_has_failed_repo_signal_and_non_success(self):
        self.failed_tick(cli=False)

    def test_cli_failure_is_nonzero_not_just_logged(self):
        self.failed_tick(cli=True)


class FairnessTests(unittest.TestCase):
    def test_round_robin_precedes_existing_batch_cut(self):
        for mcr, queued, cut in [(1, 0, 4), (2, 0, 8), (4, 0, 15), (4, 10, 6)]:
            with self.subTest(cut=cut):
                f = GateFixture(self, {A: [page([pr(n) for n in range(1, 21)])],
                                       B: [page([pr(n) for n in range(101, 121)])]}, mcr=mcr, queued=queued)
                f.run()
                expected = [n for pair in zip(range(1, 21), range(101, 121)) for n in pair][:cut]
                self.assertEqual(f.numbers(), expected)
                f.stack.close()

    def test_three_same_rank_lanes_cross_batch_boundary(self):
        c = 'example-org/primary-service-c'
        f = GateFixture(self, {A: [page([pr(n) for n in [1, 2, 3]])],
                               B: [page([pr(n) for n in [101, 102]])],
                               c: [page([pr(201)])]}, mcr=1)
        f.run()
        self.assertEqual(f.numbers(), [1, 101, 201, 2])

    def test_uneven_populations_preserve_order_and_do_not_duplicate(self):
        f = GateFixture(self, {A: [page([pr(n) for n in [5, 3, 9, 2, 1]])],
                               B: [page([pr(n) for n in [108, 102]])]})
        f.run()
        self.assertEqual(f.numbers(), [5, 108, 3, 102, 9, 2, 1])

    def test_rank_priority_and_pin_win_over_repository_fairness(self):
        low = 'example-org/last-service-api'
        middle = 'example-org/secondary-api'
        f = GateFixture(self, {low: [page([pr(90), pr(91)])], middle: [page([pr(80)])],
                               A: [page([pr(1), pr(2)])], B: [page([pr(101)])]},
                        priority=[low + '#90'])
        f.run()
        self.assertEqual(f.numbers(), [90, 1, 101, 2, 80, 91])

    def test_firstpass_filter_does_not_move_batch_cut_before_fairness(self):
        f = GateFixture(self, {A: [page([pr(n) for n in range(1, 9)])],
                               B: [page([pr(n) for n in range(101, 109)])]}, mcr=1, firstpass=True,
                        fp_status={(A, n): 'passed' for n in range(1, 9)})
        f.run()
        self.assertEqual(f.numbers(), [1, 2, 3, 4])


class GuardTests(unittest.TestCase):
    def test_exact_head_draft_truncated_and_required_ci_guards(self):
        draft = pr(1, isDraft=True)
        wrong = pr(2)
        wrong['headRef']['target']['oid'] = 'b' * 40
        truncated = pr(3)
        truncated['headRef']['target']['statusCheckRollup']['contexts']['pageInfo']['hasNextPage'] = True
        absent = pr(4)
        absent['headRef']['target']['statusCheckRollup']['contexts']['nodes'] = [check('other')]
        pending = pr(5)
        pending['headRef']['target']['statusCheckRollup']['contexts']['nodes'] = [check(status='IN_PROGRESS', conclusion=None)]
        f = GateFixture(self, {A: [page([draft, wrong, truncated, absent, pending, pr(6)])]})
        f.run()
        self.assertEqual(f.numbers(), [6])
        self.assertEqual(f.dedup_pairs, [(A, 6, HEAD)])

    def test_required_check_lookup_uses_default_branch_and_excludes_review(self):
        answers = [subprocess.CompletedProcess([], 0, 'release\n', ''),
                   subprocess.CompletedProcess([], 0, '["ci-ok", "Paperclip Review"]', '')]
        with patch.object(gate, 'sh', side_effect=answers) as command:
            self.assertEqual(gate.required_checks(A, {}, cache={}), {'ci-ok'})
        self.assertIn(f'repos/{A}/rules/branches/release', command.call_args_list[1].args[0])

    def test_check_interpretation_and_latest_attempt(self):
        cases = [([check()], {'ci-ok'}, True), ([], {'ci-ok'}, False),
                 ([check(conclusion='NEUTRAL')], {'ci-ok'}, True),
                 ([check(conclusion='SKIPPED')], {'ci-ok'}, True),
                 ([check(conclusion='FAILURE')], {'ci-ok'}, False),
                 ([check(conclusion='CANCELLED')], {'ci-ok'}, False),
                 ([check(conclusion='STALE')], {'ci-ok'}, False),
                 ([check(), {'context': 'deploy', 'state': 'PENDING'}], set(), False),
                 ([check(), {'context': 'CodeRabbit', 'state': 'PENDING'}], set(), True),
                 ([check(), {'context': 'CodeRabbit', 'state': 'FAILURE'}], set(), False),
                 ([check('Paperclip Review')], set(), False),
                 ([check(conclusion='FAILURE', startedAt='2026-01-01'),
                   check(startedAt='2026-01-02')], {'ci-ok'}, True),
                 ([check(startedAt='2026-01-01'),
                   check(status='IN_PROGRESS', conclusion=None, startedAt='2026-01-02')], {'ci-ok'}, False)]
        for contexts, required, expected in cases:
            with self.subTest(contexts=contexts):
                self.assertEqual(gate.ci_green(contexts, required), expected)

    def test_reviewed_live_queued_dedup_and_backoff_exclude_only_matching_heads(self):
        f = GateFixture(self, {A: [page([pr(n) for n in range(1, 9)])]},
                        reviewed=[[A, '1', HEAD], [A, '8', 'b' * 40]], busy=[[A, '2']],
                        queued_busy=[[A, '3']], coverage={(A, 4): 'covered', (A, 5): 'in_flight', (A, 6): 'exhausted'},
                        state={f'{A}#7@{HEAD}': NOW - gate.RETRY_AFTER + 1})
        f.run()
        self.assertEqual(f.numbers(), [8])

    def test_healthy_tick_and_cli_do_not_signal_failure(self):
        for cli in (False, True):
            with self.subTest(cli=cli):
                f = GateFixture(self)
                self.assertIn(f.run(cli=cli), (None, 0))
                self.assertEqual(f.numbers(), [1])
                f.stack.close()

    def test_archived_inventory_and_live_shadow_query_fences(self):
        payload = page([pr(1)])
        payload['data']['repository']['isArchived'] = True
        f = GateFixture(self, {A: [payload]})
        f.run()
        self.assertEqual(f.batches, [])
        review_queries = [sql for sql in f.sql if 'chat_github_reviews' in sql]
        self.assertEqual(len(review_queries), 3)
        self.assertTrue(all('shadow_label is null' in sql for sql in review_queries))
        self.assertIn("h.status = 'running'", review_queries[1])
        self.assertIn("h.status='queued'", review_queries[2])

    def test_backoff_boundary_is_retryable(self):
        f = GateFixture(self, state={f'{A}#1@{HEAD}': NOW - gate.RETRY_AFTER})
        f.run()
        self.assertEqual(f.numbers(), [1])

    def test_unreadable_dedup_fails_closed(self):
        f = GateFixture(self, dedup_error=True)
        f.run()
        self.assertEqual(f.numbers(), [])

    def test_pause_and_hold_stop_before_inventory(self):
        for marker in ('pause', 'hold'):
            with self.subTest(marker=marker):
                f = GateFixture(self)
                (gate.PAUSE if marker == 'pause' else gate.runtime.REVIEW_HOLD).touch()
                f.run()
                self.assertEqual(f.pages.calls, [])
                self.assertEqual(f.batches, [])
                f.stack.close()

    def test_reviewer_load_and_inflight_fences(self):
        for options in ({'active': None}, {'inflight': None}, {'active': 4}, {'inflight': 4},
                        {'mcr': 2, 'active': 1, 'inflight': 1}):
            with self.subTest(options=options):
                f = GateFixture(self, **options)
                f.run()
                self.assertEqual(f.batches, [])
                f.stack.close()

    def test_focus_guard_at_existing_queue_threshold(self):
        other = 'example-org/other-service'
        f = GateFixture(self, {other: [page([pr(90)])], A: [page([pr(1)])]}, queued=gate.MAX_QUEUED)
        f.run()
        self.assertEqual(f.numbers(), [1])
        self.assertEqual({c[0] for c in f.pages.calls}, {A})

    def test_mismatched_dispatch_receipt_does_not_mark_requested(self):
        f = GateFixture(self, bad_receipt=True)
        f.run()
        self.assertNotIn(f'{A}#1@{HEAD}', json.loads(f.state.read_text()))

    def test_firstpass_existing_hold_fallback_and_pause_semantics(self):
        for status, meta, guards, expected in [(None, True, True, []), ('pending', True, True, []),
                ('passed', True, True, [1]), ('fallback', True, True, [1]),
                (None, False, True, [1]), (None, True, False, [1]), ('pending', True, False, [1])]:
            with self.subTest(status=status, meta=meta, guards=guards):
                f = GateFixture(self, firstpass=True, meta=meta, guards=guards, fp_status={(A, 1): status})
                f.run()
                self.assertEqual(f.numbers(), expected)
                f.stack.close()
        f = GateFixture(self, firstpass=True)
        fp.PAUSE.touch()
        f.run()
        self.assertEqual(f.numbers(), [1], 'first-pass PAUSE retains normal-review fallback')

    def test_existing_host_and_provider_thresholds_without_inventing_final_review_guard(self):
        self.assertTrue(fp.guards(1, 20, 0.1, 1))
        for readings in [(64, 20, 0.1, 1), (1, 90, 0.1, 1), (1, 20, 0.85, 1),
                         (1, 20, 0.1, 35), (None, 20, 0.1, 1)]:
            with self.subTest(readings=readings):
                self.assertFalse(fp.guards(*readings))
        self.assertFalse(fp.meta_available([]))
        self.assertTrue(fp.meta_available([{'health': 'healthy', 'weekly_utilization': 0.1, 'five_hour_utilization': 0.1}]))
        self.assertFalse(fp.meta_available([{'health': 'healthy', 'weekly_utilization': 0.95, 'five_hour_utilization': 0.1}]))


class HarnessTests(unittest.TestCase):
    def test_network_process_and_inherited_runtime_access_are_denied(self):
        import os
        import socket
        for action in (lambda: socket.socket(), lambda: socket.create_connection(('example.invalid', 443)),
                       lambda: subprocess.run(['false']), lambda: subprocess.Popen(['false']),
                       lambda: os.system('false')):
            with self.assertRaisesRegex(AssertionError, 'offline harness forbids'):
                action()
        self.assertEqual(set(os.environ), {'REVIEW_GATE_ROOT'})
        self.assertEqual(gate.runtime.DB_COMMAND, ['false'])
        self.assertEqual(gate.runtime.TOKEN_COMMAND, ['false'])

    def test_tick_fixture_state_is_unique_even_after_previous_fixture_closes(self):
        f = GateFixture(self)
        f.run()
        previous = f.state
        f.stack.close()
        g = GateFixture(self)
        g.run()
        self.assertNotEqual(previous, g.state)
        self.assertEqual(g.numbers(), [1])


class DedupTests(unittest.TestCase):
    def test_real_dedup_completed_live_stale_stranded_and_retry_exhaustion(self):
        rows = [[A, '1', HEAD, 'completed', str(NOW), 'completed'],
                [A, '2', HEAD, 'running', str(NOW), 'running'],
                [A, '3', HEAD, 'queued', str(NOW - 1), ''],
                [A, '4', HEAD, 'queued', str(NOW - 7 * 3600), ''],
                [A, '5', HEAD, 'running', str(NOW), 'error'],
                [A, '5', HEAD, 'error', str(NOW), 'error'],
                [A, '6', HEAD, 'error', str(NOW), 'error'],
                [A, '6', HEAD, 'incomplete', str(NOW), 'error']]
        with patch.object(dedup, 'q', return_value=rows), patch('time.time', return_value=NOW):
            got = dedup.coverage([(A, n, HEAD) for n in range(1, 8)])
        self.assertEqual([got[A, n, HEAD] for n in range(1, 8)],
                         ['covered', 'in_flight', 'in_flight', 'retryable', 'exhausted', 'exhausted', 'retryable'])
