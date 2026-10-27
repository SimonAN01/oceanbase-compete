import copy
import unittest

from oceanbase_compete.errors import AuthError, ProtocolError, ToolError
from oceanbase_compete.platform import Platform, collect_pages, repo_url, task_result


class APIClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def page(rows, total, number=1, size=50):
    return {'list': rows, 'total': total, 'pageNum': number, 'pageSize': size}


class PaginationTests(unittest.TestCase):
    def test_all_pages_and_parameters(self):
        pages, calls = [page([{'id': 1}], 2), page([{'id': 2}], 2, 2)], []
        def fetch(params):
            calls.append(params)
            return pages.pop(0)
        self.assertEqual([r['id'] for r in collect_pages(fetch, {'campId': 21}, 1)], [1, 2])
        self.assertEqual(calls[1], {'campId': 21, 'pageNo': 2, 'pageSize': 1})

    def test_live_empty_task_shape(self):
        self.assertEqual(collect_pages(lambda _: {'list': None, 'total': 0, 'pageNum': 0, 'pageSize': 0}), [])

    def test_repeated_page_rejected(self):
        pages = [page([{'id': 1}], 2), page([{'id': 1}], 2, 2)]
        with self.assertRaises(ProtocolError):
            collect_pages(lambda _: pages.pop(0))

    def test_changed_total_rejected(self):
        pages = [page([{'id': 1}], 2), page([{'id': 2}], 3, 2)]
        with self.assertRaises(ProtocolError):
            collect_pages(lambda _: pages.pop(0))

    def test_early_empty_page_not_complete(self):
        pages = [page([{'id': 1}], 2), page([], 2, 2)]
        with self.assertRaises(ProtocolError):
            collect_pages(lambda _: pages.pop(0))

    def test_wrong_page_number_rejected(self):
        with self.assertRaises(ProtocolError):
            collect_pages(lambda _: page([{'id': 1}], 1, 2))

    def test_unknown_total_reads_until_empty(self):
        pages = [{'list': [{'id': 1}]}, {'list': []}]
        self.assertEqual(len(collect_pages(lambda _: pages.pop(0))), 1)

    def test_page_limit_is_not_success(self):
        with self.assertRaises(ProtocolError):
            collect_pages(lambda _: page([{'id': 1}], 2), max_pages=1)


class ProtocolTests(unittest.TestCase):
    def test_anonymous_http_success_is_not_authenticated(self):
        with self.assertRaises(AuthError):
            Platform(APIClient([{'success': False}])).user()

    def test_identity_is_account_bound_without_exposing_uid(self):
        identity = Platform(APIClient([{'success': True, 'data': {'uid': 'synthetic-user', 'nickName': 'Example'}}])).user()
        self.assertEqual(len(identity['account']), 24)
        self.assertNotIn('synthetic-user', str(identity))

    def test_failed_task_never_exposes_stale_score(self):
        result = task_result({'id': 101, 'taskStatus': 3, 'strMachineScore': '99.000', 'failMsg': 100})
        self.assertEqual(result['state'], 'FAILED')
        self.assertIsNone(result['score_text'])
        self.assertEqual(result['failure_code'], 100)

    def test_successful_zero_score_is_valid(self):
        result = task_result({'id': 101, 'taskStatus': 2, 'strMachineScore': '0.000', 'machineScore': 0})
        self.assertEqual(result['score_text'], '0.000')
        self.assertEqual(result['score_source'], 'strMachineScore')

    def test_all_pending_and_cancelled_states_hide_scores(self):
        for status in (0, 1, 4):
            with self.subTest(status=status):
                self.assertIsNone(task_result({'id': 101, 'taskStatus': status, 'strMachineScore': '99'})['score_text'])

    def test_unknown_state_is_an_error(self):
        with self.assertRaises(ProtocolError):
            task_result({'id': 101, 'taskStatus': 8, 'strMachineScore': '99'})

    def test_repo_normalization_does_not_allow_embedded_tokens(self):
        self.assertEqual(repo_url('https://github.com/example/project.git/'), 'https://github.com/example/project')
        for value in ['https://token@github.com/example/project', 'https://github.com/example/project?token=abc', 'https://github.com/example/..']:
            with self.subTest(value=value), self.assertRaises(ToolError):
                repo_url(value)

    def test_unknown_submission_fields_are_rejected(self):
        options = {'code': 200, 'data': {'managed': True, 'available': True, 'questionId': 101, 'campId': 21, 'extraFields': ['unimplementedVerifierField'], 'requiredFields': []}}
        with self.assertRaises(ProtocolError):
            Platform(APIClient([options])).options(101)

    def test_options_cannot_switch_questions(self):
        options = {'code': 200, 'data': {'managed': True, 'available': True, 'questionId': 999, 'extraFields': [], 'requiredFields': []}}
        with self.assertRaises(ProtocolError):
            Platform(APIClient([options])).options(101)

    def test_live_leaderboard_broken_metadata_reads_past_first_page(self):
        a = {'id': 1, 'groupId': 10, 'questionId': 101, 'webTotalScore': '12.000', 'totalScore': 12000}
        b = {'id': 2, 'groupId': 20, 'questionId': 101, 'webTotalScore': '0.000'}
        envelopes = [{'code': 200, 'data': page([a], 1)}, {'code': 200, 'data': page([b], 1)}, {'code': 200, 'data': page(None, 0, 0)}, {'code': 200, 'data': page([a], 1)}]
        client = APIClient(copy.deepcopy(envelopes))
        result = Platform(client).leaderboard(101, 1)
        self.assertEqual(result['count'], 2)
        self.assertTrue(result['complete'])
        self.assertEqual(result['rows'][0]['score_text'], '12.000')
        self.assertIsNone(result['rows'][0]['rank'])
        self.assertEqual(result['rows'][1]['position'], 2)
        self.assertEqual(client.calls[1][2]['params']['pageNo'], 2)

    def test_leaderboard_repeating_rows_is_not_complete(self):
        a = {'id': 1, 'questionId': 101, 'webTotalScore': '12'}
        envelopes = [{'code': 200, 'data': page([a], 1)}, {'code': 200, 'data': page([a], 1)}]
        with self.assertRaises(ProtocolError):
            Platform(APIClient(envelopes)).leaderboard(101, 1)

    def test_short_last_page_completes_without_triggering_beyond_end_error(self):
        a = {'id': 1, 'questionId': 101, 'webTotalScore': '12'}
        b = {'id': 2, 'questionId': 101, 'webTotalScore': '11'}
        c = {'id': 3, 'questionId': 101, 'webTotalScore': '10'}
        envelopes = [{'code': 200, 'data': page([a, b], 2)}, {'code': 200, 'data': page([c], 1)}, {'code': 200, 'data': page([a, b], 2)}, {'code': 200, 'data': page([c], 1)}]
        result = Platform(APIClient(envelopes)).leaderboard(101, 2)
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['terminal_reason'], 'short_page')

    def test_changing_leaderboard_during_capture_is_rejected(self):
        a = {'id': 1, 'questionId': 101, 'webTotalScore': '12'}
        envelopes = [{'code': 200, 'data': page([a], 1)}, {'code': 200, 'data': page([], 0)}, {'code': 200, 'data': page([dict(a, webTotalScore='13')], 1)}]
        with self.assertRaises(ProtocolError):
            Platform(APIClient(envelopes)).leaderboard(101, 1)

    def test_wrong_question_in_leaderboard_is_rejected(self):
        envelopes = [{'code': 200, 'data': page([{'id': 1, 'questionId': 999}], 1)}]
        with self.assertRaises(ProtocolError):
            Platform(APIClient(envelopes)).leaderboard(101)

    def test_details_only_returns_evaluation_fields(self):
        client = APIClient([{'code': 200, 'data': [{'test_case': 'case', 'score': '1', 'message': 'ok', 'phoneNumber': 'synthetic-contact'}]}])
        self.assertEqual(Platform(client).details(101), [{'test_case': 'case', 'score': '1', 'message': 'ok'}])

    def test_personal_rank_uses_only_server_rank_and_display_score(self):
        data = {'id': 1, 'groupId': 10, 'userRank': 7, 'webTotalScore': '0.000', 'totalScore': 0, 'phoneNumber': 'synthetic'}
        result = Platform(APIClient([{'code': 200, 'data': data}])).my_rank(101)
        self.assertEqual(result['rank'], 7)
        self.assertEqual(result['score_text'], '0.000')
        self.assertNotIn('phoneNumber', result)

    def test_missing_personal_rank_is_not_fabricated(self):
        self.assertIsNone(Platform(APIClient([{'code': 200, 'data': None}])).my_rank(101))

    def test_practice_verification_format_and_verifier(self):
        option = {'managed': True, 'available': True, 'questionId': 101, 'verifier': 'SEEKDB_PRACTICE', 'extraFields': ['forkUrl'], 'requiredFields': [], 'submissionType': 'ARTIFACT'}
        client = APIClient([{'code': 200, 'data': option}, {'code': 200, 'data': {'verificationText': 'OB-P:' + 'a' * 22}}])
        self.assertEqual(Platform(client).verification(101)['verification_text'], 'OB-P:' + 'a' * 22)
        self.assertEqual(client.calls[-1][0], 'POST')

    def test_practice_verification_rejects_wrong_question_kind(self):
        option = {'managed': True, 'available': True, 'questionId': 101, 'extraFields': [], 'requiredFields': []}
        with self.assertRaises(ToolError):
            Platform(APIClient([{'code': 200, 'data': option}])).verification(101)


if __name__ == '__main__':
    unittest.main()
