import contextlib
import csv
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oceanbase_compete.cli import _write_export, csv_text, exit_code, main, parser, version_args
from oceanbase_compete.errors import ToolError
from oceanbase_compete.storage import Store, read_json, write_json
from oceanbase_compete.workflow import verify_remote
from oceanbase_compete.cli import run
from oceanbase_compete.workflow import Workflow
from test_workflow import COMMIT_A, Clock, FakeAPI


class CLITests(unittest.TestCase):
    def test_read_command_dispatch_uses_the_real_workflow_with_synthetic_service(self):
        with tempfile.TemporaryDirectory() as td:
            api, clock = FakeAPI(), Clock()
            workflow = Workflow(api, Store(td), remote_check=lambda *a: {'verified': True}, sleep=clock.sleep, clock=clock)
            workflow.create_profile('example', track='powercontext', branch='feature/csv', commit=COMMIT_A)
            commands = [['auth', 'check'], ['doctor'], ['camps'], ['questions', '--camp-id', '21'], ['options', '--question-id', '600070'], ['profile', 'list'], ['profile', 'show', 'example'], ['preflight', '--profile', 'example'], ['leaderboard', '--profile', 'example'], ['experiments', '--profile', 'example'], ['watch', '--profile', 'example', '--max-cycles', '1']]
            for argv in commands:
                with self.subTest(argv=argv):
                    self.assertIsInstance(run(parser().parse_args(argv), workflow, lambda _: None), dict)
            self.assertEqual(api.submissions, [])

    def test_cli_submit_wait_snapshot_score_reconcile_and_queue_are_connected(self):
        with tempfile.TemporaryDirectory() as td:
            api, clock = FakeAPI(), Clock()
            workflow = Workflow(api, Store(td), remote_check=lambda *a: {'verified': True}, sleep=clock.sleep, clock=clock)
            workflow.create_profile('example', track='powercontext', branch='feature/csv', commit=COMMIT_A)
            def dispatch(argv):
                return run(parser().parse_args(argv), workflow, lambda _: None)
            dry = dispatch(['submit', '--profile', 'example', '--dry-run'])
            self.assertFalse(dry['submitted'])
            submitted = dispatch(['submit', '--profile', 'example', '--wait', '--snapshot'])
            receipt = submitted['receipt']
            self.assertTrue(read_json(receipt).get('leaderboard_snapshot'))
            self.assertEqual(dispatch(['score', '--receipt', receipt])['state'], 'SUCCEEDED')
            self.assertEqual(dispatch(['reconcile', '--receipt', receipt])['task_id'], '101')
            self.assertEqual(dispatch(['adopt', '--profile', 'example', '--task-id', '101'])['receipt'], receipt)
            self.assertTrue(dispatch(['compare', '--before', receipt, '--after', receipt])['comparable'])
            self.assertEqual(len(dispatch(['experiments', '--profile', 'example', '--refresh'])['records']), 1)
            plan = Path(td) / 'plan.json'
            write_json(plan, {'profile': 'example', 'jobs': [{'branch': 'feature/csv', 'commit': COMMIT_A}]})
            queue = dispatch(['queue', 'run', str(plan), '--interval', '0'])
            self.assertEqual(queue['outcome'], 'queue_completed')
            self.assertEqual(dispatch(['queue', 'status', queue['checkpoint']])['jobs'][0]['status'], 'DONE')
            self.assertEqual(len(api.submissions), 1)

    def test_flags_work_before_and_after_nested_subcommands(self):
        for args in [['--cookie-file', 'local.txt', 'auth', 'check'], ['auth', '--cookie-file', 'local.txt', 'check'], ['auth', 'check', '--cookie-file', 'local.txt']]:
            with self.subTest(args=args):
                self.assertEqual(parser().parse_args(args).cookie_file, 'local.txt')

    def test_unknown_auth_values_are_never_needed_as_arguments(self):
        parsed = parser().parse_args(['auth', 'check', '--cookie-stdin'])
        self.assertTrue(parsed.cookie_stdin)

    def test_non_finite_timeout_is_rejected_by_parser(self):
        for value in ['nan', 'inf', '-inf']:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(['watch', '--profile', 'example', '--timeout=' + value])

    def test_duplicate_extra_fields_are_rejected(self):
        args = parser().parse_args(['submit', '--profile', 'example', '--extra', 'prUrl=https://example.com/1', '--extra', 'prUrl=https://example.com/2'])
        with self.assertRaises(ToolError):
            version_args(args)

    def test_csv_protects_formula_names_and_preserves_numeric_zero(self):
        text = csv_text([{'team_name': '=1+2', 'score_text': '0.000', 'delta': '-0.250'}])
        row = list(csv.DictReader(io.StringIO(text)))[0]
        self.assertEqual(row['team_name'], "'=1+2")
        self.assertEqual(row['score_text'], '0.000')
        self.assertEqual(row['delta'], '-0.250')

    def test_export_will_not_overwrite_cookie_input(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'input.txt'
            path.write_text('synthetic-private-input')
            with self.assertRaises(ToolError):
                _write_export(path, 'new data', str(path))
            self.assertEqual(path.read_text(), 'synthetic-private-input')

    def test_exit_codes_report_failure_pending_and_partial_queue(self):
        cases = [({'state': 'FAILED'}, 2), ({'state': 'CANCELLED'}, 2), ({'state': 'RUNNING'}, 3), ({'outcome': 'queue_submission_cap_reached'}, 3), ({'outcome': 'queue_completed_with_failures'}, 2), ({'result': {'state': 'FAILED'}}, 2), ({'state': 'SUCCEEDED'}, 0)]
        for result, code in cases:
            with self.subTest(result=result):
                self.assertEqual(exit_code(result), code)

    def test_cli_json_output_redacts_credentials(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'OBCOMP_COOKIE': 'authorization=synthetic-auth-secret'}, clear=True), patch('oceanbase_compete.cli.run', return_value={'message': 'synthetic-auth-secret', 'task_id': '101'}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['auth', 'check', '--state-dir', td])
        self.assertEqual(code, 0)
        self.assertNotIn('synthetic-auth-secret', out.getvalue())
        self.assertIn('101', out.getvalue())

    def test_cli_error_redacts_credentials(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'OBCOMP_COOKIE': 'authorization=synthetic-auth-secret'}, clear=True), patch('oceanbase_compete.cli.run', side_effect=ToolError('synthetic-auth-secret')), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['auth', 'check', '--state-dir', td])
        self.assertEqual(code, 1)
        self.assertNotIn('synthetic-auth-secret', err.getvalue())
        self.assertEqual(out.getvalue(), '')


class StorageTests(unittest.TestCase):
    def test_atomic_round_trip_and_private_file_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / 'state')
            file = store.save('receipts', 'example', {'score': '0.000'})
            self.assertEqual(read_json(file), {'score': '0.000'})
            if os.name != 'nt':
                self.assertEqual(Path(file).stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.root.stat().st_mode & 0o777, 0o700)

    def test_traversal_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(td)
            for name in ['../escape', '/absolute', '', 'a/b', '..']:
                with self.subTest(name=name), self.assertRaises(ToolError):
                    store.save('receipts', name, {})

    def test_corrupt_checkpoint_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'bad.json'
            path.write_text('{')
            with self.assertRaises(ToolError):
                read_json(path)

    def test_process_lock_prevents_overlapping_submissions(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(td)
            with store.lock('same-question'):
                with self.assertRaises(ToolError):
                    with store.lock('same-question'):
                        self.fail('second writer acquired the same lock')
            with store.lock('same-question'):
                pass

    def test_failed_atomic_serialization_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'state.json'
            write_json(path, {'valid': True})
            with self.assertRaises(ValueError):
                write_json(path, {'invalid': float('nan')})
            self.assertEqual(read_json(path), {'valid': True})


class GitVerificationTests(unittest.TestCase):
    def test_remote_tip_is_pinned_to_full_commit(self):
        sha = 'a' * 40
        result = subprocess.CompletedProcess([], 0, sha + '\trefs/heads/feature/csv\n', '')
        with patch('oceanbase_compete.workflow.subprocess.run', return_value=result):
            self.assertTrue(verify_remote('https://github.com/example/team', 'feature/csv', sha)['verified'])

    def test_unpublished_commit_is_rejected_without_local_ancestry_proof(self):
        result = subprocess.CompletedProcess([], 0, 'b' * 40 + '\trefs/heads/main\n', '')
        with patch('oceanbase_compete.workflow.subprocess.run', return_value=result), self.assertRaises(ToolError):
            verify_remote('https://github.com/example/team', 'main', 'a' * 40)

    def test_older_commit_needs_matching_origin_and_successful_ancestry(self):
        responses = [subprocess.CompletedProcess([], 0, 'b' * 40 + '\trefs/heads/main\n', ''), subprocess.CompletedProcess([], 0, 'git@github.com:example/team.git\n', ''), subprocess.CompletedProcess([], 0, b'', b'')]
        with patch('oceanbase_compete.workflow.subprocess.run', side_effect=responses):
            self.assertTrue(verify_remote('https://github.com/example/team', 'main', 'a' * 40, '/synthetic/checkout')['verified'])

    def test_different_local_origin_cannot_prove_remote_accessibility(self):
        responses = [subprocess.CompletedProcess([], 0, 'b' * 40 + '\trefs/heads/main\n', ''), subprocess.CompletedProcess([], 0, 'https://github.com/example/wrong\n', '')]
        with patch('oceanbase_compete.workflow.subprocess.run', side_effect=responses), self.assertRaises(ToolError):
            verify_remote('https://github.com/example/team', 'main', 'a' * 40, '/synthetic/checkout')


class InstallerTests(unittest.TestCase):
    def test_self_contained_install_update_and_entrypoint(self):
        source = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('obcomp_installer', source / 'install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            destination = module.install(td)
            entry = destination / 'scripts' / 'obcomp.py'
            result = subprocess.run([sys.executable, str(entry), '--version'], capture_output=True, text=True, timeout=15, cwd=td)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '0.1.0')
            self.assertTrue((destination / 'SKILL.md').is_file())
            self.assertFalse(any('cookie' in p.name.lower() for p in destination.rglob('*')))
            with self.assertRaises(ValueError):
                module.install(td)
            self.assertEqual(module.install(td, update=True), destination)


if __name__ == '__main__':
    unittest.main()
