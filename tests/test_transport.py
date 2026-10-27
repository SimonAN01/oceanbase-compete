import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from oceanbase_compete.errors import AuthError, DeadlineReached, OutcomeUnknown, ProtocolError, ToolError
from oceanbase_compete.transport import Client, NoRedirect, Redactor, checked, dumps, load_cookie, loads


class Response(io.BytesIO):
    def __init__(self, body=b'{"code":200,"data":{}}', status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class CookieTests(unittest.TestCase):
    def test_raw_header_and_prefix(self):
        self.assertEqual(load_cookie(stdin=True, stream=io.StringIO('Cookie: a=b; c=d\n')), 'a=b; c=d')

    def test_browser_json_filters_domains_and_expired_values(self):
        raw = dumps([{"domain": ".oceanbase.com", "name": "a", "value": "b"}, {"domain": ".attacker.example", "name": "bad", "value": "x"}, {"domain": "webapi.oceanbase.com", "name": "old", "value": "x", "expirationDate": 1}])
        self.assertEqual(load_cookie(stdin=True, stream=io.StringIO(raw)), 'a=b')

    def test_netscape_http_only_session(self):
        raw = '# Netscape HTTP Cookie File\n#HttpOnly_.oceanbase.com\tTRUE\t/\tTRUE\t0\ta\tb\n.example.com\tTRUE\t/\tTRUE\t0\tx\ty'
        self.assertEqual(load_cookie(stdin=True, stream=io.StringIO(raw)), 'a=b')

    def test_rejects_header_injection(self):
        with self.assertRaises(ToolError):
            load_cookie(stdin=True, stream=io.StringIO('a=b\nX-Evil: injected'))

    def test_cookie_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'local.txt'
            p.write_text('a=b')
            self.assertEqual(load_cookie(str(p)), 'a=b')

    def test_no_implicit_browser_access(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_cookie(), '')

    def test_mutually_exclusive_inputs(self):
        with self.assertRaises(ToolError):
            load_cookie('unused', True, io.StringIO('a=b'))

    def test_redacts_encoded_and_decoded_authentication(self):
        redactor = Redactor('authorization=hmac%20synthetic-secret; RDS-TOKEN=synthetic-session')
        value = redactor.clean({'message': 'hmac synthetic-secret and synthetic-session', 'token': 'another-value'})
        self.assertNotIn('synthetic-secret', value['message'])
        self.assertNotIn('synthetic-session', value['message'])
        self.assertEqual(value['token'], '[redacted]')

    def test_preference_cookie_does_not_corrupt_task_ids_or_commits(self):
        redactor = Redactor('receive-cookie-deprecation=1; authorization=synthetic-secret')
        value = {'task_id': '101', 'commit': '1' * 40, 'message': 'synthetic-secret'}
        clean = redactor.clean(value)
        self.assertEqual(clean['task_id'], '101')
        self.assertEqual(clean['commit'], '1' * 40)
        self.assertEqual(clean['message'], '[redacted]')


class HTTPTests(unittest.TestCase):
    url = 'https://openwebapi.oceanbase.com/api/test'

    def test_https_host_allowlist_prevents_credential_forwarding(self):
        for url in ['http://openwebapi.oceanbase.com/api/test', 'https://evil.example/api/test', 'https://openwebapi.oceanbase.com.evil.example/api/test', 'https://u:p@openwebapi.oceanbase.com/api/test', 'https://openwebapi.oceanbase.com:444/api/test', 'https://openwebapi.oceanbase.com/login']:
            opener = Opener()
            with self.subTest(url=url), self.assertRaises(ToolError):
                Client('a=b', opener=opener).request('GET', url)
            self.assertEqual(opener.calls, [])

    def test_redirect_handler_never_forwards_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example'))

    def test_exact_wire_payload_and_cookie(self):
        opener = Opener(Response())
        Client('a=b', opener=opener).request('POST', self.url, body={'questionId': 123, 'commitId': 'a' * 40}, uncertain=True)
        req, _ = opener.calls[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Cookie'), 'a=b')
        self.assertEqual(loads(req.data), {'questionId': 123, 'commitId': 'a' * 40})

    def test_get_query_encoding(self):
        opener = Opener(Response())
        Client(opener=opener).request('GET', self.url, params={'branch': 'feature/a b', 'missing': None})
        self.assertTrue(opener.calls[0][0].full_url.endswith('?branch=feature%2Fa+b'))

    def test_read_retries_429_with_bounded_retry_after(self):
        delays, opener = [], Opener(Response(status=429, headers={'Retry-After': '100'}), Response())
        Client(opener=opener, sleep=delays.append).request('GET', self.url)
        self.assertEqual(delays, [30])
        self.assertEqual(len(opener.calls), 2)

    def test_read_network_retry_exhaustion(self):
        opener = Opener(URLError('synthetic'), URLError('synthetic'), URLError('synthetic'))
        with self.assertRaises(ToolError):
            Client(opener=opener, sleep=lambda _: None).request('GET', self.url)
        self.assertEqual(len(opener.calls), 3)

    def test_mutation_timeout_is_unknown_and_never_retried(self):
        opener = Opener(TimeoutError('synthetic'))
        with self.assertRaises(OutcomeUnknown):
            Client(opener=opener).request('POST', self.url, body={}, uncertain=True)
        self.assertEqual(len(opener.calls), 1)

    def test_mutation_server_error_is_unknown(self):
        opener = Opener(Response(status=503))
        with self.assertRaises(OutcomeUnknown):
            Client(opener=opener).request('POST', self.url, body={}, uncertain=True)
        self.assertEqual(len(opener.calls), 1)

    def test_definite_validation_rejection(self):
        with self.assertRaises(ProtocolError):
            Client(opener=Opener(Response(status=400))).request('POST', self.url, body={}, uncertain=True)

    def test_mutation_malformed_json_is_unknown(self):
        with self.assertRaises(OutcomeUnknown):
            Client(opener=Opener(Response(b'<html>error</html>'))).request('POST', self.url, body={}, uncertain=True)

    def test_expired_login_is_not_retried(self):
        opener = Opener(Response(status=401))
        with self.assertRaises(AuthError):
            Client(opener=opener).request('GET', self.url)
        self.assertEqual(len(opener.calls), 1)

    def test_read_redirect_is_rejected(self):
        with self.assertRaises(ProtocolError):
            Client(opener=Opener(Response(status=302))).request('GET', self.url)

    def test_mutation_redirect_is_unknown(self):
        with self.assertRaises(OutcomeUnknown):
            Client(opener=Opener(Response(status=302))).request('POST', self.url, body={}, uncertain=True)

    def test_business_error_and_unknown_write_error(self):
        with self.assertRaises(ProtocolError):
            checked({'code': 400, 'message': 'invalid input'}, uncertain=True)
        with self.assertRaises(OutcomeUnknown):
            checked({'code': 508, 'message': 'unknown error'}, uncertain=True)
        with self.assertRaises(OutcomeUnknown):
            checked({'data': None}, uncertain=True)

    def test_decimal_precision_and_non_finite_rejection(self):
        data = loads('{"score":0.12345678901234567890123456789}')
        self.assertEqual(str(data['score']), '0.12345678901234567890123456789')
        for value in ['NaN', 'Infinity', '-Infinity']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                loads('{"score":' + value + '}')

    def test_expired_read_deadline_does_not_start_another_request(self):
        client = Client(opener=Opener())
        client.deadline = 0
        with self.assertRaises(DeadlineReached):
            client.request('GET', self.url)
        self.assertEqual(client.opener.calls, [])


if __name__ == '__main__':
    unittest.main()
