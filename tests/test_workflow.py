"""Synthetic service fixtures exercise the complete workflow without real writes."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from oceanbase_compete.errors import DeadlineReached, OutcomeUnknown, Pending, ProtocolError, ToolError
from oceanbase_compete.storage import Store, now, read_json, write_json
from oceanbase_compete.transport import Redactor
from oceanbase_compete.workflow import Workflow, decimal_delta, extra_params, snapshot_diff, validate_version

REPOSITORY = 'https://github.com/example/competition-team'
COMMIT_A, COMMIT_B = 'a' * 40, 'b' * 40


class FakeAPI:
    def __init__(self):
        self.account = 'synthetic-account'
        self.rows, self.submissions = [], []
        self.client = SimpleNamespace(deadline=None, redactor=Redactor())
        self.option = {'managed': True, 'available': True, 'questionId': 600070, 'campId': 21, 'submissionType': 'CODE', 'resultFormat': 'CASES', 'extraFields': [], 'requiredFields': []}
        self.camp = {'id': 21, 'commitStartTime': None, 'commitEndTime': None}
        self.question = {'id': 600070, 'questionName': 'Synthetic question'}
        self.next_status, self.next_score = 2, '12.300'
        self.behavior, self.response_id = None, True
        self.board_rows = [{'entry_id': '1', 'team_id': '10', 'team_name': 'Synthetic team', 'position': 1, 'rank': None, 'score_text': '1.000'}]

    def user(self):
        return {'account': self.account, 'display_name': 'Synthetic user'}

    def camps(self):
        return [copy.deepcopy(self.camp)]

    def questions(self, camp_id):
        return [copy.deepcopy(self.question)]

    def options(self, question_id):
        return copy.deepcopy(self.option)

    def registration(self, question_id):
        return {'gitUrl': REPOSITORY, 'branch': None, 'commitId': None, 'isMegagame': 1}

    def tasks(self, question_id):
        return copy.deepcopy(self.rows)

    def submit(self, payload):
        self.submissions.append(copy.deepcopy(payload))
        if self.behavior == 'rejected':
            raise ProtocolError('definite synthetic validation rejection')
        if self.behavior == 'unknown_without_task':
            raise OutcomeUnknown('synthetic timeout')
        row = dict(payload, id=len(self.rows) + 101, taskStatus=self.next_status, strMachineScore=self.next_score, gmtCreate=now(), gmtModified=now(), deleteFlag=0)
        self.rows.append(row)
        if self.behavior == 'unknown_with_task':
            raise OutcomeUnknown('synthetic timeout after acceptance')
        if self.behavior == 'interrupt_after_acceptance':
            self.behavior = None
            raise KeyboardInterrupt()
        return {'taskId': row['id']} if self.response_id else None

    def details(self, task_id):
        return [{'test_case': 'synthetic-case', 'score': '12.300', 'message': 'synthetic result'}]

    def leaderboard(self, question_id, page_size, max_pages):
        return {'rows': copy.deepcopy(self.board_rows), 'complete': True, 'coverage': 'all_rows_exposed_by_question_endpoint', 'atomic': False, 'pages': 2, 'count': len(self.board_rows)}


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state')
        self.api, self.clock = FakeAPI(), Clock()
        self.workflow = Workflow(self.api, self.store, remote_check=lambda *args: {'verified': True, 'remote_head': args[2]}, sleep=self.clock.sleep, clock=self.clock)
        self.workflow.create_profile('example', track='powercontext', branch='feature/csv', commit=COMMIT_A)

    def submit(self, **kwargs):
        return self.workflow.submit('example', **kwargs)

    def plan(self, commits):
        path = Path(self.temp.name) / 'queue-plan.json'
        write_json(path, {'profile': 'example', 'jobs': [{'branch': 'feature/csv', 'commit': c} for c in commits]})
        return path

    def test_profile_is_bound_to_account_and_registered_repository(self):
        self.assertEqual(self.workflow.profile('example')['repository'], REPOSITORY)
        self.api.account = 'different-synthetic-account'
        with self.assertRaises(ToolError):
            Workflow(self.api, self.store).profile('example')

    def test_profile_rejects_wrong_registered_repo(self):
        with self.assertRaises(ToolError):
            self.workflow.create_profile('another', track='powercontext', repository='https://github.com/example/wrong')

    def test_profile_does_not_overwrite_existing_target(self):
        with self.assertRaises(ToolError):
            self.workflow.create_profile('example', track='powercontext')

    def test_explicit_ids_and_track_cannot_conflict(self):
        with self.assertRaises(ToolError):
            self.workflow.create_profile('another', track='powercontext', camp_id=99)

    def test_dry_run_makes_no_submission_or_receipt(self):
        result = self.submit(dry_run=True)
        self.assertEqual(result['outcome'], 'dry_run')
        self.assertEqual(result['payload']['commitId'], COMMIT_A)
        self.assertEqual(self.api.submissions, [])
        self.assertEqual(self.store.list('receipts'), [])

    def test_submission_payload_and_receipt_association(self):
        result = self.submit()
        self.assertEqual(self.api.submissions[0], {'questionId': 600070, 'gitRepo': REPOSITORY, 'branch': 'feature/csv', 'commitId': COMMIT_A})
        self.assertEqual(result['task_id'], '101')
        self.assertEqual(read_json(result['receipt'])['status'], 'CONFIRMED')
        self.assertEqual(self.workflow.score(result['receipt'])['score_text'], '12.300')

    def test_no_task_id_in_success_response_reconciles_new_record(self):
        self.api.response_id = False
        result = self.submit()
        self.assertEqual(result['task_id'], '101')

    def test_identical_version_is_not_written_twice(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(first['receipt'], second['receipt'])
        self.assertEqual(second['outcome'], 'already_submitted')
        self.assertEqual(len(self.api.submissions), 1)

    def test_explicit_resubmit_creates_another_task_and_receipt(self):
        first, second = self.submit(), self.submit(resubmit=True)
        self.assertNotEqual(first['receipt'], second['receipt'])
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual(len(self.api.submissions), 2)

    def test_version_change_creates_distinct_submission(self):
        a, b = self.submit(), self.submit(commit=COMMIT_B)
        self.assertNotEqual(a['task_id'], b['task_id'])

    def test_prepared_receipt_exists_before_network_write(self):
        original = self.api.submit
        def checked_submit(payload):
            rows = self.store.list('receipts')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], 'PREPARED')
            return original(payload)
        self.api.submit = checked_submit
        self.submit()

    def test_unknown_write_blocks_further_submissions(self):
        self.api.behavior = 'unknown_without_task'
        with self.assertRaises(OutcomeUnknown) as context:
            self.submit()
        receipt = context.exception.details['receipt']
        self.assertEqual(read_json(receipt)['status'], 'UNCONFIRMED')
        with self.assertRaises(OutcomeUnknown):
            self.submit(commit=COMMIT_B)
        self.assertEqual(len(self.api.submissions), 1)

    def test_unknown_write_can_reconcile_without_another_write(self):
        self.api.behavior = 'unknown_with_task'
        with self.assertRaises(OutcomeUnknown) as context:
            self.submit()
        receipt = context.exception.details['receipt']
        result = self.workflow.reconcile(receipt)
        self.assertEqual(result['task_id'], '101')
        self.assertEqual(len(self.api.submissions), 1)

    def test_new_ambiguous_records_do_not_get_guessed(self):
        self.api.behavior = 'unknown_without_task'
        with self.assertRaises(OutcomeUnknown) as context:
            self.submit()
        path = context.exception.details['receipt']
        payload = read_json(path)['payload']
        self.api.rows = [dict(payload, id=101, taskStatus=0), dict(payload, id=102, taskStatus=0)]
        with self.assertRaises(OutcomeUnknown):
            self.workflow.reconcile(path)
        self.assertEqual(self.workflow.reconcile(path, task_id=102)['task_id'], '102')

    def test_definite_rejection_is_saved(self):
        self.api.behavior = 'rejected'
        with self.assertRaises(ProtocolError):
            self.submit()
        self.assertEqual(self.store.list('receipts')[0]['status'], 'REJECTED')

    def test_task_id_with_wrong_commit_is_rejected(self):
        result = self.submit()
        self.api.rows[0]['commitId'] = COMMIT_B
        with self.assertRaises(ProtocolError):
            self.workflow.score(result['receipt'])

    def test_new_submission_cannot_be_bound_to_a_preexisting_task(self):
        first = self.submit()
        original = self.api.submit
        def wrong_id(payload):
            original(payload)
            return {'taskId': 101}
        self.api.submit = wrong_id
        with self.assertRaises(ProtocolError):
            self.submit(resubmit=True)
        self.assertEqual(self.workflow.score(first['receipt'])['task_id'], '101')

    def test_failed_task_keeps_failure_and_hides_numeric_score(self):
        self.api.next_status, self.api.next_score = 3, '0.000'
        result = self.submit()
        score = self.workflow.score(result['receipt'])
        self.assertEqual(score['outcome'], 'evaluation_failed')
        self.assertIsNone(score['score_text'])

    def test_success_zero_is_a_completed_score(self):
        self.api.next_score = '0.000'
        result = self.submit()
        self.assertEqual(self.workflow.score(result['receipt'])['score_text'], '0.000')

    def test_adopt_only_uses_authenticated_target_records(self):
        self.api.rows = [{'id': 101, 'questionId': 600070, 'gitRepo': REPOSITORY, 'branch': 'feature/csv', 'commitId': COMMIT_A, 'taskStatus': 3}]
        result = self.workflow.adopt('example', 101)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.api.submissions, [])

    def test_repeated_adoption_reuses_the_same_receipt(self):
        submitted = self.submit()
        adopted = self.workflow.adopt('example', 101)
        self.assertEqual(submitted['receipt'], adopted['receipt'])
        self.assertEqual(len(self.store.list('receipts')), 1)

    def test_other_question_record_cannot_be_adopted(self):
        self.api.rows = [{'id': 101, 'questionId': 999, 'gitRepo': REPOSITORY, 'taskStatus': 2}]
        with self.assertRaises(ToolError):
            self.workflow.adopt('example', 101)

    def test_existing_remote_version_is_adopted_without_new_write(self):
        self.api.rows = [{'id': 101, 'questionId': 600070, 'gitRepo': REPOSITORY, 'branch': 'feature/csv', 'commitId': COMMIT_A, 'taskStatus': 3}]
        result = self.submit()
        self.assertEqual(result['outcome'], 'already_submitted')
        self.assertEqual(self.api.submissions, [])

    def test_wait_is_bounded_and_does_not_resubmit(self):
        self.api.next_status = 0
        result = self.submit()
        events = []
        score = self.workflow.wait_score(result['receipt'], interval=3, timeout=7, emit=events.append)
        self.assertTrue(score['timed_out'])
        self.assertEqual(self.clock.value, 7)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.api.submissions), 1)
        self.assertIsNone(self.api.client.deadline)

    def test_wait_query_deadline_returns_pending_and_restores_deadline(self):
        result = self.submit()
        self.api.tasks = lambda _: (_ for _ in ()).throw(DeadlineReached('synthetic deadline'))
        with self.assertRaises(Pending):
            self.workflow.wait_score(result['receipt'])
        self.assertIsNone(self.api.client.deadline)

    def test_watch_reports_query_deadline_without_claiming_successful_capture(self):
        self.api.leaderboard = lambda *args: (_ for _ in ()).throw(DeadlineReached('synthetic deadline'))
        result = self.workflow.watch('example')
        self.assertEqual(result['outcome'], 'watch_deadline_reached')
        self.assertEqual(result['cycles'], 0)
        self.assertIsNone(result['last_snapshot'])
        self.assertIsNone(self.api.client.deadline)

    def test_closed_submission_window_is_blocked(self):
        self.api.camp['commitEndTime'] = '2000-01-01T00:00:00Z'
        with self.assertRaises(ToolError):
            self.submit()
        self.assertEqual(self.api.submissions, [])

    def test_unavailable_question_is_blocked(self):
        self.api.option['available'] = False
        with self.assertRaises(ToolError):
            self.submit()

    def test_empty_explicit_branch_does_not_fall_back_silently(self):
        with self.assertRaises(ToolError):
            self.submit(branch='')

    def test_unpushed_remote_version_stops_before_post(self):
        def reject(*args):
            raise ToolError('remote did not contain the version')
        self.workflow.remote_check = reject
        with self.assertRaises(ToolError):
            self.submit()
        self.assertEqual(self.api.submissions, [])

    def test_snapshot_persists_complete_result(self):
        result = self.workflow.snapshot('example')
        self.assertTrue(Path(result['snapshot']).is_file())
        self.assertEqual(read_json(result['snapshot'])['count'], 1)

    def test_snapshot_attaches_to_terminal_submission_receipt(self):
        submitted = self.submit()
        capture = self.workflow.snapshot('example', receipt_path=submitted['receipt'])
        receipt = read_json(submitted['receipt'])
        self.assertEqual(receipt['leaderboard_snapshot']['path'], capture['snapshot'])

    def test_snapshot_does_not_attach_to_pending_task(self):
        self.api.next_status = 0
        submitted = self.submit()
        with self.assertRaises(ToolError):
            self.workflow.snapshot('example', receipt_path=submitted['receipt'])

    def test_watch_suppresses_unchanged_events_and_stops_at_cycle_bound(self):
        events = []
        result = self.workflow.watch('example', interval=2, timeout=100, max_cycles=3, emit=events.append)
        self.assertEqual(result['cycles'], 3)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.api.submissions, [])
        self.assertIsNone(self.api.client.deadline)

    def test_experiments_excludes_failed_results_from_best(self):
        self.api.next_score = '0.000'
        self.submit()
        self.api.next_status, self.api.next_score = 3, '999.000'
        self.submit(commit=COMMIT_B)
        result = self.workflow.experiments('example')
        self.assertEqual(result['best']['score_text'], '0.000')
        self.assertEqual(len(result['records']), 2)

    def test_version_comparison_uses_receipts_and_exact_decimal_delta(self):
        self.api.next_score = '1.00000000000000000001'
        a = self.submit()
        self.api.next_score = '1.00000000000000000003'
        b = self.submit(commit=COMMIT_B)
        result = self.workflow.compare(a['receipt'], b['receipt'])
        self.assertEqual(result['score_delta'], '2E-20')

    def test_queue_submission_cap_and_resume(self):
        plan = self.plan([COMMIT_A, COMMIT_B])
        first = self.workflow.run_queue(plan, max_submissions=1, interval=0)
        self.assertEqual(first['outcome'], 'queue_submission_cap_reached')
        self.assertEqual(len(self.api.submissions), 1)
        second = self.workflow.run_queue(plan, max_submissions=2, interval=0)
        self.assertEqual(second['outcome'], 'queue_completed')
        self.assertEqual(len(self.api.submissions), 2)
        third = self.workflow.run_queue(plan, max_submissions=2, interval=0)
        self.assertEqual(third['outcome'], 'queue_completed')
        self.assertEqual(len(self.api.submissions), 2)

    def test_queue_stop_on_failure(self):
        self.api.next_status = 3
        result = self.workflow.run_queue(self.plan([COMMIT_A, COMMIT_B]), interval=0)
        self.assertEqual(result['outcome'], 'queue_stopped_on_failure')
        self.assertEqual(len(self.api.submissions), 1)

    def test_queue_continue_on_failure_remains_bounded(self):
        self.api.next_status = 3
        result = self.workflow.run_queue(self.plan([COMMIT_A, COMMIT_B]), interval=0, continue_on_failure=True)
        self.assertEqual(result['outcome'], 'queue_completed_with_failures')
        self.assertEqual(len(self.api.submissions), 2)
        self.assertEqual([j['status'] for j in result['jobs']], ['FAILED', 'FAILED'])

    def test_queue_interrupt_after_acceptance_resumes_without_duplicate(self):
        self.api.behavior = 'interrupt_after_acceptance'
        plan = self.plan([COMMIT_A])
        with self.assertRaises(KeyboardInterrupt):
            self.workflow.run_queue(plan, max_submissions=1, interval=0)
        self.assertEqual(len(self.api.submissions), 1)
        resumed = self.workflow.run_queue(plan, max_submissions=1, interval=0)
        self.assertEqual(resumed['outcome'], 'queue_completed')
        self.assertEqual(len(self.api.submissions), 1)

    def test_queue_unknown_write_stops_and_does_not_retry(self):
        self.api.behavior = 'unknown_without_task'
        plan = self.plan([COMMIT_A, COMMIT_B])
        for _ in range(2):
            with self.assertRaises(OutcomeUnknown):
                self.workflow.run_queue(plan, interval=0)
        self.assertEqual(len(self.api.submissions), 1)

    def test_queue_rejects_duplicate_and_invalid_later_job_before_first_write(self):
        for commits in ([COMMIT_A, COMMIT_A], [COMMIT_A, 'invalid']):
            with self.subTest(commits=commits), self.assertRaises(ToolError):
                self.workflow.run_queue(self.plan(commits), interval=0)
        self.assertEqual(self.api.submissions, [])


class MaterialAndDiffTests(unittest.TestCase):
    def test_high_precision_delta_does_not_round_to_default_decimal_context(self):
        score = '123.123456789012345678901234567890123456789'
        self.assertEqual(decimal_delta(score, '0'), score)

    def test_full_immutable_version_required(self):
        for branch, commit in [('main', 'a' * 7), ('../main', COMMIT_A), ('bad branch', COMMIT_A), ('-bad', COMMIT_A)]:
            with self.subTest(branch=branch), self.assertRaises(ToolError):
                validate_version(branch, commit)

    def test_seekdb_material_normalization(self):
        options = {'verifier': 'SEEKDB_PRACTICE', 'submissionType': 'ARTIFACT', 'extraFields': ['deploymentUuid', 'prUrl', 'forkUrl'], 'requiredFields': []}
        extras = extra_params(options, {'deploymentUuid': 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE', 'prUrl': 'https://github.com/oceanbase/seekdb/pull/123', 'forkUrl': 'https://github.com/Example/SeekDB.git'})
        self.assertEqual(extras['deploymentUuid'], 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        self.assertEqual(extras['forkUrl'], 'https://github.com/example/seekdb')

    def test_seekdb_rejects_official_repo_as_fork(self):
        options = {'verifier': 'SEEKDB_PRACTICE', 'submissionType': 'ARTIFACT', 'extraFields': ['forkUrl'], 'requiredFields': []}
        with self.assertRaises(ToolError):
            extra_params(options, {'forkUrl': 'https://github.com/oceanbase/seekdb'})

    def test_artifact_requires_material_and_rejects_unknown_fields(self):
        options = {'submissionType': 'ARTIFACT', 'extraFields': ['workUrl'], 'requiredFields': []}
        for fields in [{}, {'unknown': 'value'}, {'workUrl': 'http://example.com'}, {'workUrl': 'https://secret@example.com'}]:
            with self.subTest(fields=fields), self.assertRaises(ToolError):
                extra_params(options, fields)

    def test_snapshot_delta_distinguishes_position_from_official_rank(self):
        scope = {'camp_id': 21, 'question_id': 600070}
        a = {'scope': scope, 'complete': True, 'rows': [{'entry_id': '1', 'team_id': '10', 'team_name': 'A', 'position': 3, 'rank': None, 'score_text': '0.000'}]}
        b = {'scope': scope, 'complete': True, 'rows': [{'entry_id': '2', 'team_id': '10', 'team_name': 'A renamed', 'position': 1, 'rank': None, 'score_text': '1.000'}]}
        result = snapshot_diff(a, b)
        self.assertEqual(len(result['changed']), 1)
        self.assertEqual(result['added'], [])
        self.assertEqual(result['changed'][0]['score_delta'], '1.000')
        self.assertIsNone(result['changed'][0]['rank_after'])

    def test_snapshots_from_different_targets_cannot_be_compared(self):
        with self.assertRaises(ToolError):
            snapshot_diff({'scope': {'question_id': 1}, 'complete': True}, {'scope': {'question_id': 2}, 'complete': True})

    def test_incomplete_snapshot_cannot_be_compared(self):
        with self.assertRaises(ToolError):
            snapshot_diff({'scope': {}, 'complete': False}, {'scope': {}, 'complete': True})

    def test_personal_official_rank_change_is_reported_even_if_public_rows_unchanged(self):
        before = {'scope': {}, 'complete': True, 'account': 'synthetic', 'rows': [], 'personal': {'rank': 5}}
        after = dict(before, personal={'rank': 3})
        self.assertEqual(snapshot_diff(before, after)['personal_change']['after']['rank'], 3)


if __name__ == '__main__':
    unittest.main()
