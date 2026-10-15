"""Credential-free filesystem fixtures. No service, user, Docker or host mutations."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('migration', Path(__file__).with_name('isolated-migration.py'))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class MigrationTests(unittest.TestCase):
    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'legacy' / 'runner6'
        self.source.mkdir(parents=True)
        self.target = self.root / 'isolated' / 'coolify-vps-6'
        self.target.parent.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.units = self.root / 'units'
        self.units.mkdir()
        self.service = 'actions.runner.TogetherWeOwn.coolify-vps-6.service'
        self.unit = self.units / self.service
        self.before = '[Service]\nUser=fixture-old\nExecStart=/fixture/runsvc.sh\n'
        self.unit.write_text(self.before)
        self.unit.chmod(0o640)
        for name in m.PRESERVED:
            (self.source / name).write_text('fixture-not-a-credential\n')
            (self.source / name).chmod(0o640)
        (self.source / '.runner').write_text(json.dumps({'workFolder': '_work', 'agentName': 'coolify-vps-6'}))
        (self.source / 'runsvc.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.source / '_work').mkdir()
        (self.source / '_work' / 'file').write_text('fixture build bytes')
        (self.source / 'link').symlink_to('_work/file')
        self.data = {'version': 1, 'phase': 'planned', 'source': str(self.source),
                     'target': str(self.target), 'service': self.service,
                     'old_uid': os.getuid(), 'old_gid': os.getgid(),
                     'new_uid': os.getuid() + 1, 'new_gid': os.getgid() + 1,
                     'new_user': 'gha-isolated-6', 'home': '/var/lib/gha-isolated-6',
                     'inventory': m.inventory(self.source), 'digests': m.digests(self.source),
                     'unit_mode': 0o640}
        self.cmd = self.patch(mock.patch.object(m, 'command', return_value=''))
        self.chown = self.patch(mock.patch.object(m.os, 'chown'))
        self.patch(mock.patch.object(m, 'SYSTEMD', self.units))
        self.handles = self.patch(mock.patch.object(m, 'no_open_handles'))
        self.patch(mock.patch.object(m, 'no_mounts'))
        self.patch(mock.patch.object(m, 'stopped'))

    def test_round_trip_preserves_registration_and_unit_and_modes(self):
        m.stage(self.data, self.state)
        self.assertFalse(self.source.exists())
        self.assertEqual(m.digests(self.target), self.data['digests'])
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        self.assertIn('User=gha-isolated-6', self.unit.read_text())
        self.assertIn('/runsvc.sh', self.unit.read_text())
        self.assertEqual(self.data['phase'], 'prepared')
        m.rollback(self.data, self.state)
        self.assertFalse(self.target.exists())
        self.assertEqual(m.digests(self.source), self.data['digests'])
        self.assertEqual(self.unit.read_text(), self.before)
        self.assertEqual(stat.S_IMODE(self.unit.stat().st_mode), 0o640)
        self.assertEqual(m.inventory(self.source), self.data['inventory'])
        self.assertEqual(self.data['phase'], 'rolled_back')
        # A second rollback restores the same stopped state without unregistering.
        m.rollback(self.data, self.state)
        self.assertEqual(m.digests(self.source), self.data['digests'])
        for call in self.cmd.call_args_list:
            self.assertIn(call.args[:2], [('systemctl', 'daemon-reload'), ('systemd-analyze', 'verify')])

    def test_no_registration_values_in_metadata_journal(self):
        m.stage(self.data, self.state)
        journal = (self.state / 'state.json').read_text()
        self.assertNotIn('fixture-not-a-credential', journal)
        self.assertEqual(stat.S_IMODE((self.state / 'state.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / 'unit.before').stat().st_mode), 0o600)

    def test_symlink_owner_never_dereferenced(self):
        m.stage(self.data, self.state)
        calls = [c for c in self.chown.call_args_list if str(c.args[0]).endswith('/link')]
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].kwargs['follow_symlinks'], False)

    def test_refuses_existing_destination_without_mutating_source(self):
        self.target.mkdir()
        with self.assertRaises(m.Refused):
            m.stage(self.data, self.state)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.unit.read_text(), self.before)
        self.assertFalse((self.state / 'state.json').exists())

    def test_failure_after_move_has_journal_and_token_free_rollback(self):
        self.cmd.side_effect = m.Refused('verification failed')
        with self.assertRaises(m.Refused):
            m.stage(self.data, self.state)
        self.assertTrue(self.target.exists())
        self.assertTrue((self.state / 'state.json').exists())
        self.cmd.side_effect = None
        m.rollback(self.data, self.state)
        self.assertTrue(self.source.exists())
        self.assertEqual(m.digests(self.source), self.data['digests'])

    def test_open_handle_after_move_stays_private_until_rollback(self):
        self.handles.side_effect = m.Refused('open handle')
        with self.assertRaises(m.Refused):
            m.stage(self.data, self.state)
        self.assertTrue((self.state / 'tree').exists())
        self.assertFalse(self.target.exists())
        self.handles.side_effect = None
        m.rollback(self.data, self.state)
        self.assertTrue(self.source.exists())

    def test_registration_drift_refuses_rollback_without_overwrite(self):
        m.stage(self.data, self.state)
        (self.target / '.credentials').write_text('fixture changed')
        with self.assertRaises(m.Refused):
            m.rollback(self.data, self.state)
        self.assertTrue(self.target.exists())
        self.assertFalse(self.source.exists())

    def test_recreated_source_refuses_rollback(self):
        m.stage(self.data, self.state)
        self.source.mkdir()
        with self.assertRaises(m.Refused):
            m.rollback(self.data, self.state)
        self.assertTrue(self.target.exists())

    def test_hardlink_refused(self):
        os.link(self.source / '_work/file', self.source / 'hardlink')
        with self.assertRaises(m.Refused):
            m.inventory(self.source)

    def test_fifo_refused(self):
        os.mkfifo(self.source / 'fifo')
        with self.assertRaises(m.Refused):
            m.inventory(self.source)

    def test_setid_refused(self):
        (self.source / 'runsvc.sh').chmod(0o4755)
        with self.assertRaises(m.Refused):
            m.inventory(self.source)

    def test_symlinked_credential_refused(self):
        (self.source / '.credentials').unlink()
        (self.source / '.credentials').symlink_to('.env')
        with self.assertRaises(m.Refused):
            m.digests(self.source)

    def test_external_workfolder_refused(self):
        (self.source / '.runner').write_text('{"workFolder":"/shared"}')
        with self.assertRaises(m.Refused):
            m.digests(self.source)

    def test_disk_and_inode_checks(self):
        with mock.patch.object(m.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
            with self.assertRaises(m.Refused):
                m.check_resources(self.source, self.target.parent)
        with mock.patch.object(m.shutil, 'disk_usage', return_value=mock.Mock(free=6 * 1024**3)), \
                mock.patch.object(m.os, 'statvfs', return_value=mock.Mock(f_favail=1)):
            with self.assertRaises(m.Refused):
                m.check_resources(self.source, self.target.parent)

    def test_resource_thresholds_are_centralized(self):
        # case_35135887: one place for thresholds; the gate below must use them.
        self.assertEqual(m.DISK_FREE_MIN_BYTES, 5 * 1024**3)
        self.assertEqual(m.INODES_FREE_MIN, 10000)

    def test_host_load_never_gates_resources(self):
        # case_35135887 (case_be10e9e2 step 4): host loadavg counts runnable tasks
        # regardless of priority and says nothing about the stopped
        # migration target. An extreme host load must PASS when disk and
        # inodes are healthy; the old load5 <= 2xCPU leg refused here.
        with mock.patch.object(m.shutil, 'disk_usage', return_value=mock.Mock(free=6 * 1024**3)), \
                mock.patch.object(m.os, 'statvfs', return_value=mock.Mock(f_favail=20000)), \
                mock.patch.object(m.os, 'getloadavg', return_value=(100, 100, 100)), \
                mock.patch.object(m.os, 'cpu_count', return_value=2):
            m.check_resources(self.source, self.target.parent)

    def prepare_accept(self):
        m.stage(self.data, self.state)
        props = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '123',
                 'ControlPID': '0', 'ControlGroup': '/fixture', 'DropInPaths': ''}
        self.props = self.patch(mock.patch.object(m, 'properties', return_value=props))
        self.idle = self.patch(mock.patch.object(m, 'idle_processes', return_value={(123, '10')}))
        self.patch(mock.patch.object(m.pwd, 'getpwnam', return_value=mock.Mock(pw_dir='/fixture')))
        self.probe = self.patch(mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(returncode=0)))
        return mock.Mock(queue_held_idle_confirmed=True, pg_bin_dir='/fixture/pg')

    def assert_prepared(self):
        self.assertEqual(json.loads((self.state / 'state.json').read_text())['phase'], 'prepared')

    def test_accept_requires_attestation_before_probe(self):
        args = self.prepare_accept()
        args.queue_held_idle_confirmed = False
        with self.assertRaisesRegex(m.Refused, 'attestation'):
            m.accept(self.data, self.state, args)
        self.probe.assert_not_called()
        self.assert_prepared()

    def test_accept_healthy_revalidates_after_probe_with_changed_offline_modes(self):
        args = self.prepare_accept()
        self.assertNotEqual(m.inventory(self.target), self.data['inventory'])
        # Diagnostics are legitimate live writes, not evidence of a job.
        def probe(argv, **kwargs):
            (self.target / 'diagnostic.log').write_text('fixture log')
            self.assertEqual(self.idle.call_count, 1)
            self.assertIn('--unit-sha256', argv)
            return mock.Mock(returncode=0)
        self.probe.side_effect = probe
        m.accept(self.data, self.state, args)
        self.assertEqual(self.idle.call_count, 2)
        self.assertEqual(self.props.call_count, 2)
        self.assertEqual(json.loads((self.state / 'state.json').read_text())['phase'], 'accepted')

    def test_accept_registration_drift_after_probe_refuses(self):
        args = self.prepare_accept()
        def probe(*a, **k):
            (self.target / '.credentials').write_text('changed fixture')
            return mock.Mock(returncode=0)
        self.probe.side_effect = probe
        with self.assertRaisesRegex(m.Refused, 'registration changed'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_accept_unit_and_candidate_drift_after_probe_refuses(self):
        args = self.prepare_accept()
        def probe(*a, **k):
            self.unit.write_text('changed unit')
            (self.state / self.service).write_text('changed unit')
            return mock.Mock(returncode=0)
        self.probe.side_effect = probe
        with self.assertRaisesRegex(m.Refused, 'unit changed'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_accept_unit_drift_before_probe_refuses(self):
        args = self.prepare_accept()
        self.unit.write_text('changed unit')
        with self.assertRaisesRegex(m.Refused, 'live unit differs'):
            m.accept(self.data, self.state, args)
        self.probe.assert_not_called()
        self.assert_prepared()

    def test_accept_job_after_probe_refuses(self):
        args = self.prepare_accept()
        self.idle.side_effect = [{(123, '10')}, m.Refused('job process')]
        with self.assertRaisesRegex(m.Refused, 'job process'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_accept_listener_restart_after_probe_refuses(self):
        args = self.prepare_accept()
        self.idle.side_effect = [{(123, '10')}, {(123, '11')}]
        with self.assertRaisesRegex(m.Refused, 'chain changed'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_accept_effective_dropin_drift_after_probe_refuses(self):
        args = self.prepare_accept()
        before = self.props.return_value
        self.props.side_effect = [dict(before), dict(before, DropInPaths='/changed.conf')]
        with self.assertRaisesRegex(m.Refused, 'effective service changed'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_accept_failed_probe_never_advances(self):
        args = self.prepare_accept()
        self.probe.return_value.returncode = 1
        with self.assertRaisesRegex(m.Refused, 'acceptance failed'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_accept_old_uid_handle_after_probe_refuses(self):
        args = self.prepare_accept()
        self.handles.side_effect = [None, m.Refused('old handle')]
        with self.assertRaisesRegex(m.Refused, 'old handle'):
            m.accept(self.data, self.state, args)
        self.assert_prepared()

    def test_fsync_order_covers_backup_state_tree_and_unit(self):
        events = []
        real_sync, real_rename, real_replace = os.fsync, Path.rename, Path.replace
        def sync(fd):
            events.append(('sync', os.readlink('/proc/self/fd/' + str(fd))))
            real_sync(fd)
        def rename(src, dest):
            events.append(('rename', str(src), str(dest)))
            return real_rename(src, dest)
        def replace(src, dest):
            events.append(('replace', str(src), str(dest)))
            return real_replace(src, dest)
        with mock.patch.object(m.os, 'fsync', side_effect=sync), \
                mock.patch.object(Path, 'rename', rename), mock.patch.object(Path, 'replace', replace):
            m.stage(self.data, self.state)
            m.rollback(self.data, self.state)
        first_move = events.index(('rename', str(self.source), str(self.state / 'tree')))
        self.assertIn(('sync', str(self.state / 'unit.before')), events[:first_move])
        self.assertIn(('sync', str(self.state / 'state.new')), events[:first_move])
        self.assertIn(('sync', str(self.state)), events[:first_move])
        for index, event in enumerate(events):
            if event[0] not in ('rename', 'replace'):
                continue
            # Every rename synchronously persists BOTH affected parent dirs.
            parents = {str(Path(event[1]).parent), str(Path(event[2]).parent)}
            following = events[index + 1:index + 1 + len(parents)]
            self.assertEqual({('sync', p) for p in parents}, set(following))
            if event[0] == 'replace':
                self.assertEqual(events[index - 1], ('sync', event[1]))

    def test_backup_sync_failure_prevents_first_mutation(self):
        with mock.patch.object(m, 'fsync_file', side_effect=OSError('fixture sync failure')):
            with self.assertRaises(OSError):
                m.stage(self.data, self.state)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.unit.read_text(), self.before)
        self.assertFalse((self.state / 'state.json').exists())

    def test_unit_directory_sync_failure_preserves_planned_journal_for_rollback(self):
        real_sync = m.fsync_dir
        def sync(path):
            if path == self.units:
                raise OSError('fixture sync failure')
            real_sync(path)
        with mock.patch.object(m, 'fsync_dir', side_effect=sync):
            with self.assertRaises(OSError):
                m.stage(self.data, self.state)
        saved = json.loads((self.state / 'state.json').read_text())
        self.assertEqual(saved['phase'], 'planned')
        m.rollback(saved, self.state)
        self.assertEqual(self.unit.read_text(), self.before)
        self.assertEqual(m.inventory(self.source), self.data['inventory'])


class IdleChainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.cg = self.root / 'cgroup' / 'fixture'
        self.cg.mkdir(parents=True)
        (self.cg / 'cgroup.procs').write_text('100\n101\n102\n')
        self.tree = '/opt/two-isolated/coolify-vps-6'
        self.data = {'target': self.tree, 'new_uid': 1002}
        self.props = {'ActiveState': 'active', 'SubState': 'running', 'ControlPID': '0',
                      'ControlGroup': '/fixture', 'MainPID': '100'}
        for name, value in [('PROC', self.proc), ('CGROUP', self.cg.parent)]:
            p = mock.patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.process(100, 1, '/usr/bin/bash', ['/bin/bash', self.tree + '/runsvc.sh'])
        self.process(101, 100, self.tree + '/externals/node20/bin/node',
                     ['./externals/node20/bin/node', './bin/RunnerService.js'])
        self.process(102, 101, self.tree + '/bin/Runner.Listener',
                     [self.tree + '/bin/Runner.Listener', 'run', '--startuptype', 'service'])

    def process(self, pid, ppid, exe, argv):
        p = self.proc / str(pid)
        p.mkdir()
        (p / 'status').write_text('Uid:\t1002\t1002\t1002\t1002\nPPid:\t' + str(ppid))
        (p / 'exe').symlink_to(exe)
        (p / 'cwd').symlink_to(self.tree)
        (p / 'cmdline').write_bytes(('\0'.join(argv) + '\0').encode())
        (p / 'stat').write_text(str(pid) + ' (fixture name) S ' + '0 ' * 18 + '12345 0')

    def test_real_idle_wrapper_chain_passes(self):
        self.assertEqual(m.idle_processes(self.data, self.props),
                         {(100, '12345'), (101, '12345'), (102, '12345')})

    def test_extra_worker_in_child_cgroup_refuses(self):
        (self.cg / 'job').mkdir()
        (self.cg / 'job/cgroup.procs').write_text('103\n')
        with self.assertRaises(m.Refused):
            m.idle_processes(self.data, self.props)

    def test_worker_replacing_idle_node_refuses(self):
        (self.proc / '101/exe').unlink()
        (self.proc / '101/exe').symlink_to(self.tree + '/bin/Runner.Worker')
        with self.assertRaises(m.Refused):
            m.idle_processes(self.data, self.props)

    def test_empty_or_absent_cgroup_refuses(self):
        for group in ('', '/', '/missing'):
            with self.subTest(group=group), self.assertRaises(m.Refused):
                m.idle_processes(self.data, dict(self.props, ControlGroup=group))

    def test_wrong_parent_chain_refuses(self):
        (self.proc / '102/status').write_text('Uid: 1002 1002 1002 1002\nPPid: 1')
        with self.assertRaises(m.Refused):
            m.idle_processes(self.data, self.props)


class OfflineGateTests(unittest.TestCase):
    def test_active_runner_is_refused(self):
        with mock.patch.object(m, 'properties', return_value={'ActiveState': 'active', 'SubState': 'running',
                                                            'MainPID': '123', 'ControlPID': '0'}):
            with self.assertRaises(m.Refused):
                m.stopped('fixture.service')

    def test_control_process_is_refused(self):
        with mock.patch.object(m, 'properties', return_value={'ActiveState': 'inactive', 'SubState': 'dead',
                                                            'MainPID': '0', 'ControlPID': '456'}):
            with self.assertRaises(m.Refused):
                m.stopped('fixture.service')

    def test_incomplete_systemctl_is_refused(self):
        with mock.patch.object(m, 'command', return_value='ActiveState=inactive'):
            with self.assertRaises(m.Refused):
                m.properties('fixture.service')

    def test_script_contains_no_registration_or_start_stop_commands(self):
        # Behavioral round-trip above checks the emitted commands as well.
        text = Path(m.__file__).read_text()
        self.assertNotIn("command('systemctl', 'start'", text)
        self.assertNotIn("command('systemctl', 'stop'", text)
        self.assertNotIn("command('config.sh'", text)


if __name__ == '__main__':
    unittest.main()
