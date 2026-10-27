"""RED acceptance handoff: only validated bytes may enter the queue namespace.

Run with Python -B and --artifact-root pointing at the extracted artifact tree.
No installed paths, real accounts, credentials, network, or root privileges.
Expectations are from the card contract; no implementation diff is inspected.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
VALIDATED = b'{"what":"offline dummy","why":"test only","rollback":"discard dummy"}\n'
CAP = 16384
ARGS, REMAINING = (None, None)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_classifier(root):
    spec = importlib.util.spec_from_file_location('snapshot_ci_changes', root / '.github/scripts/ci_changes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SnapshotAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() == 0:
            raise RuntimeError('offline acceptance refuses root')
        sys.path.insert(0, str(ARGS.artifact_root / 'tests'))
        global h
        import op_request_harness as h
        cls.source = h.Provenance.load(ARGS.artifact_root / 'ops/operator-safety/wrappers/op-request', 'fixed')
        cls.classifier = load_classifier(ARGS.artifact_root)
        # A setup refusal is an ERROR, never evidence that a product test failed.
        # All adverse cases depend on this control passing first.
        cls.control = cls.run_case('none')
        cls.require_control(cls.control)
        cls.evidence = {'uid': os.geteuid(), 'source_sha256': sha(cls.source.data),
                        'control': cls.control, 'cases': {}}

    @classmethod
    def tearDownClass(cls):
        if ARGS.evidence:
            ARGS.evidence.write_text(json.dumps(cls.evidence, indent=2) + '\n')

    @classmethod
    def run_case(cls, mutation):
        with h.publication_case(cls.source) as case:
            if case.src.read_bytes() != VALIDATED:
                raise RuntimeError('artifact harness changed its synthetic input; port the test seam explicitly')
            cp = case.path / 'bin/cp'
            cp.write_bytes((HERE / 'test_snapshot_copy_boundary.py').read_bytes())
            case.source.verify()
            h.verify_rewrite(case.source.data, h.regular_bytes(case.runner), case.replacements)
            env = h.clean_env(case.path, OPREQ_MODE='publication', OPREQ_FAULT='',
                              SNAPSHOT_MUTATION=mutation, SNAPSHOT_READ_PROBE='1',
                              TMPDIR=str(case.path), PYTHONPATH=str(HERE / 'snapshot_probe'),
                              PYTHONDONTWRITEBYTECODE='1')
            result = h.run_process(['/usr/bin/env', '-u', 'BASH_ENV', '/bin/bash',
                                    str(case.runner), str(case.src)], env)
            observation = case.path / 'copy-observation.json'
            if not observation.is_file():
                raise RuntimeError(f'copy boundary not exercised: rc={result.returncode}, stderr={result.stderr!r}')
            reads = case.path / 'read-observations.jsonl'
            trace = [json.loads(line) for line in reads.read_text().splitlines()] if reads.exists() else []
            if not any(row.get('event') == 'active' for row in trace):
                raise RuntimeError('read-size observer did not load into the wrapper Python process')
            files = []
            for dest in case.files():
                if not dest.is_file() or dest.is_symlink():
                    raise RuntimeError('unexpected non-regular queue entry')
                if dest.stat().st_size > 65536:
                    raise RuntimeError('probe scratch bound exceeded')
                data = dest.read_bytes()
                files.append({'name': dest.name, 'size': len(data), 'sha256': sha(data),
                              'mode': dest.stat().st_mode & 0o7777})
            return {'mutation': mutation, 'rc': result.returncode, 'stdout': result.stdout,
                    'stderr': result.stderr.replace(str(case.path), '<private-scratch>'),
                    'audit': case.audit(), 'copy': json.loads(observation.read_text()),
                    'reads': trace, 'retained': files}

    @classmethod
    def require_control(cls, result):
        files = result['retained']
        if not (result['rc'] == 0 and len(files) == 1 and
                files[0]['size'] == len(VALIDATED) and files[0]['sha256'] == sha(VALIDATED) and
                files[0]['mode'] == 0o640 and
                len([s for s in result['stdout'].splitlines() if s.startswith('queued ')]) == 1 and
                result['audit'].count('op-request ok') == 1 and
                result['copy']['copied_sha256'] == sha(VALIDATED) and
                any(row['event'] == 'read' for row in result['reads'])):
            raise RuntimeError(f'happy-path/observer control did not pass: {result!r}')

    def probe(self, mode):
        result = self.run_case(mode)
        self.evidence['cases'][self.id().split('.')[-1]] = result
        self.assertTrue(result['copy']['same_inode'], 'probe must not replace the source inode')
        self.assertEqual(result['copy']['source_before_size'], len(VALIDATED))
        self.assertEqual(result['copy']['source_before_sha256'], sha(VALIDATED))
        self.assertNotEqual(result['copy']['source_after_sha256'], sha(VALIDATED), 'mutation must change real source bytes')
        self.assertEqual(result['copy']['source_identity_before'], result['copy']['source_identity_after'])
        self.assertIn('request-ok', result['stdout'], 'mutation must occur after successful validation')
        # Preserve the existing false-success repair, independent of snapshot safety.
        if result['rc'] == 0:
            self.assertEqual(len(result['retained']), 1)
            self.assertEqual(result['retained'][0]['sha256'], sha(VALIDATED), 'success published unvalidated bytes')
            self.assertEqual(result['audit'].count('op-request ok'), 1)
            self.assertEqual(len([s for s in result['stdout'].splitlines() if s.startswith('queued ')]), 1)
        else:
            self.assertEqual(result['rc'], 1, 'post-validation publication failure must be truthful')
            self.assertFalse(any(s.startswith('queued ') for s in result['stdout'].splitlines()))
            self.assertNotIn('op-request ok', result['audit'])
            self.assertEqual(result['stderr'].count('op-request FAILED'), 1)
            if result['retained']:
                self.assertEqual(result['audit'].count('op-request FAILED'), 1)
                from op_request_publication import incomplete_publication_diagnostic
                # Existing assertion accepts equivalent wording, and checks the
                # full destination identity plus no-retry/operator/audit state.
                dest = Path('<private-scratch>') / 'queue' / result['retained'][0]['name']
                incomplete_publication_diagnostic(result['stderr'], dest, audit_confirmed=True)
        return result

    def assert_snapshot_only(self, result):
        # Inspect both the copy-boundary publication and retained queue contents.
        # Removing a bad destination afterwards cannot hide a transient leak.
        copied = result['copy']
        self.assertEqual(copied['copied_sha256'], sha(VALIDATED),
                         f'unvalidated bytes entered queue namespace: copied_size={copied["copied_size"]}')
        for dest in result['retained']:
            self.assertEqual(dest['sha256'], sha(VALIDATED), 'retained destination is not the validated snapshot')

    def test_00_happy_path_control(self):
        self.require_control(self.control)

    def test_same_inode_rewrite_cannot_publish_unvalidated_bytes(self):
        # Catches re-reading a mutable inode after validating its old bytes.
        result = self.probe('same-inode')
        self.assertEqual(result['copy']['source_after_size'], len(VALIDATED), 'probe must preserve source size too')
        self.assert_snapshot_only(result)

    def test_growth_cannot_publish_or_retain_more_than_validated_snapshot(self):
        # Catches whole-inode copy after the original bounded validation.
        result = self.probe('growth')
        self.assertEqual(result['copy']['source_after_size'], 65536)
        sizes = [result['copy']['copied_size'], *[d['size'] for d in result['retained']]]
        self.assertLessEqual(max(sizes), len(VALIDATED),
                             f'growth escaped validated snapshot: sizes={sizes}, validated={len(VALIDATED)}, cap={CAP}')
        self.assert_snapshot_only(result)

    def test_destination_read_back_never_uses_an_unbounded_read(self):
        # Catches read()/read(-1)/readall() on the published destination.
        result = self.probe('growth')
        requests = [row['requested'] for row in result['reads'] if row['event'] == 'read']
        self.assertTrue(requests, 'destination read-back was not observed; port probe if the IO API changes')
        self.assertTrue(all(isinstance(n, int) and 0 < n <= CAP + 1 for n in requests),
                        f'destination read-back requested unbounded/excessive bytes: {requests}')

    def test_executable_nested_readme_selects_core_ci(self):
        # Catches markdown being skipped before its executable ops input mapping.
        source = self.classifier.classify(['ops/operator-safety/wrappers/op-request'])
        prose = self.classifier.classify(['docs/non-executable-prose.md'])
        actual = self.classifier.classify(['ops/operator-safety/wrappers/README.md'])
        self.evidence['classification'] = {'source': source, 'prose': prose, 'executable_readme': actual}
        self.assertTrue(source['core'], 'classifier positive control failed')
        self.assertFalse(prose['core'], 'classifier prose negative control failed')
        self.assertTrue(actual['core'], 'executable README skips its consuming core suite')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-root', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    ARGS, REMAINING = parser.parse_known_args()
    ARGS.artifact_root = ARGS.artifact_root.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *REMAINING], verbosity=2)
