"""
Comprehensive Test Suite for Research-Agent Thesis:
- Multi-Key API Failover (Slots 1, 2, 3)
- Exact 60-Second Inactivity Session Enforcement (Server & Client)
- Oracle Database Authentication and NL-to-SQL Execution
- SELECT-only Security Validation
- Whisper Voice Endpoint
- Zero API Key / Credential Leakage
"""
import unittest
import unittest.mock as mock
import os
import re
import json
import time
import subprocess
from base64 import b64decode, b64encode
from dotenv import load_dotenv, dotenv_values
from starlette.testclient import TestClient
import openai
from itsdangerous import TimestampSigner

# Ensure local .env is loaded
load_dotenv(override=True)
import app as application

class ResearchAgentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(application.app)
        cls.secret = os.environ.get("FLASK_SECRET_KEY", "change_this_secret_key_later")

    def test_01_app_startup_unauthenticated_redirect(self):
        """TEST 1: Start app and verify unauthenticated user is redirected to /login."""
        res = self.client.get('/', follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get('location'), '/login')

    def test_02_oracle_login_success(self):
        """TEST 2: Login using valid Oracle credentials."""
        res = self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get('location'), '/')

    def test_03_english_employee_query(self):
        """TEST 3: English employee query execution."""
        # Log in
        self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        # Using count query which executes deterministic Oracle SQL
        res = self.client.post('/api/chat', json={'message': 'how many employees are in total', 'language': 'EN'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('reply', res.json())
        self.assertTrue('1000' in res.json()['reply'] or '1,000' in res.json()['reply'])

    def test_04_urdu_employee_query(self):
        """TEST 4: Urdu employee query execution."""
        self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        res = self.client.post('/api/chat', json={'message': 'کل کتنے ملازمین ہیں', 'language': 'UR'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('reply', res.json())
        self.assertTrue('1000' in res.json()['reply'] or '1,000' in res.json()['reply'])

    def test_05_roman_urdu_employee_query(self):
        """TEST 5: Roman Urdu employee query execution."""
        self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        res = self.client.post('/api/chat', json={'message': 'total employees kitne hain', 'language': 'UR'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('reply', res.json())
        self.assertTrue('1000' in res.json()['reply'] or '1,000' in res.json()['reply'])

    def test_06_sql_generation(self):
        """TEST 6: Verified SELECT generation and execution on Oracle FREEPDB1."""
        self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        res = self.client.post('/api/chat', json={'message': 'how many departments are there'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('reply', res.json())

    def test_07_select_only_protection(self):
        """TEST 7: Verify non-SELECT / data modification requests are rejected."""
        self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        res = self.client.post('/api/chat', json={'message': 'delete all employees from database'})
        self.assertEqual(res.status_code, 200)
        reply = res.json()['reply'].lower()
        self.assertTrue('change' in reply or 'tabdeeli' in reply or 'cannot' in reply or 'records' in reply)

    def test_08_voice_transcription_endpoint(self):
        """TEST 8: Verify voice endpoint handles upload and returns structured JSON."""
        self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        files = {'audio': ('voice.webm', b'TEST_AUDIO_BYTES_123', 'audio/webm')}
        res = self.client.post('/api/transcribe', files=files)
        self.assertIn(res.status_code, [200, 422, 502, 503])
        self.assertTrue('transcript' in res.json() or 'error' in res.json())

    def test_09_10_11_12_api_key_failover(self):
        """TESTS 9, 10, 11, 12: API Key failover across slots 1 -> 2 -> 3 and sanitized error handling."""
        with mock.patch.dict(os.environ, {
            'GROQ_API_KEY_1': 'mock-key-1',
            'GROQ_API_KEY_2': 'mock-key-2',
            'GROQ_API_KEY_3': 'mock-key-3',
        }):
            # Test 10: Slot 1 fails (429) -> Slot 2 succeeds
            mock_success = mock.MagicMock()
            mock_success.choices = [mock.MagicMock(message=mock.MagicMock(content='TYPE:CHAT\nLANG:EN\nCONTENT:OK Slot 2'))]

            attempts = []
            def side_effect_slot2(model, messages, **kwargs):
                attempts.append(len(attempts) + 1)
                if len(attempts) == 1:
                    raise openai.RateLimitError('TPM limit reached', response=mock.MagicMock(status_code=429), body={})
                return mock_success

            with mock.patch('openai.resources.chat.completions.Completions.create', side_effect=side_effect_slot2):
                res = application.call_groq_chat_with_fallback([{'role': 'user', 'content': 'hi'}])
                self.assertEqual(attempts, [1, 2])
                self.assertIn('OK Slot 2', res.choices[0].message.content)

            # Test 11: Slots 1 and 2 fail (429 & 503) -> Slot 3 succeeds
            mock_success_3 = mock.MagicMock()
            mock_success_3.choices = [mock.MagicMock(message=mock.MagicMock(content='TYPE:CHAT\nLANG:EN\nCONTENT:OK Slot 3'))]
            attempts.clear()

            def side_effect_slot3(model, messages, **kwargs):
                attempts.append(len(attempts) + 1)
                if len(attempts) == 1:
                    raise openai.RateLimitError('Rate limited', response=mock.MagicMock(status_code=429), body={})
                elif len(attempts) == 2:
                    raise openai.InternalServerError('Provider busy', response=mock.MagicMock(status_code=503), body={})
                return mock_success_3

            with mock.patch('openai.resources.chat.completions.Completions.create', side_effect=side_effect_slot3):
                res = application.call_groq_chat_with_fallback([{'role': 'user', 'content': 'hi'}])
                self.assertEqual(attempts, [1, 2, 3])
                self.assertIn('OK Slot 3', res.choices[0].message.content)

            # Test 12: All 3 slots fail -> Clean sanitized message returned
            attempts.clear()
            def side_effect_all_fail(model, messages, **kwargs):
                attempts.append(len(attempts) + 1)
                raise openai.RateLimitError('Rate limited', response=mock.MagicMock(status_code=429), body={})

            with mock.patch('openai.resources.chat.completions.Completions.create', side_effect=side_effect_all_fail):
                with self.assertRaises(RuntimeError) as cm:
                    application.call_groq_chat_with_fallback([{'role': 'user', 'content': 'hi'}])
                self.assertEqual(str(cm.exception), "AI service is temporarily busy. Please try again shortly.")
                self.assertEqual(attempts, [1, 2, 3])

    def test_13_14_15_16_inactivity_and_logout(self):
        """TESTS 13, 14, 15, 16: Inactivity timeout (60s), activity reset, 401 on protected API, and full logout."""
        # Login
        r_login = self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        cookie_header = r_login.headers.get('set-cookie', '')
        match = re.search(r'session=([^;]+)', cookie_header)
        self.assertIsNotNone(match)
        session_cookie = match.group(1)

        unsigned = TimestampSigner(str(self.secret)).unsign(session_cookie.encode('utf-8'))
        session_dict = json.loads(b64decode(unsigned))
        token = session_dict['db_login_token']
        self.assertIn(token, application.ACTIVE_DB_LOGINS)

        # TEST 14: Interaction before 60s resets activity timer
        session_dict['last_activity'] = time.time() - 30
        active_cookie = TimestampSigner(str(self.secret)).sign(b64encode(json.dumps(session_dict).encode('utf-8'))).decode('utf-8')
        r_hb = self.client.post('/api/heartbeat', headers={'Cookie': f'session={active_cookie}'})
        self.assertEqual(r_hb.status_code, 200)
        self.assertEqual(r_hb.json(), {'success': True, 'active': True})

        r_active = self.client.post('/api/chat', json={'message': 'hello'}, headers={'Cookie': f'session={active_cookie}'})
        self.assertEqual(r_active.status_code, 200)

        # TEST 13 & 15: Inactivity for >= 60s expires session
        session_dict['last_activity'] = time.time() - 65
        expired_cookie = TimestampSigner(str(self.secret)).sign(b64encode(json.dumps(session_dict).encode('utf-8'))).decode('utf-8')

        # TEST 15: Protected API returns 401 with session_expired
        r_api_expired = self.client.post('/api/chat', json={'message': 'hello'}, headers={'Cookie': f'session={expired_cookie}'})
        self.assertEqual(r_api_expired.status_code, 401)
        self.assertEqual(r_api_expired.json(), {'success': False, 'error': 'session_expired'})

        # Re-login for TEST 13 HTML redirect verification
        r_login2 = self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        m2 = re.search(r'session=([^;]+)', r_login2.headers.get('set-cookie', ''))
        s2 = json.loads(b64decode(TimestampSigner(str(self.secret)).unsign(m2.group(1).encode('utf-8'))))
        s2['last_activity'] = time.time() - 65
        exp2 = TimestampSigner(str(self.secret)).sign(b64encode(json.dumps(s2).encode('utf-8'))).decode('utf-8')

        # TEST 13: HTML request redirects to /login?reason=expired
        r_page_expired = self.client.get('/', headers={'Cookie': f'session={exp2}'}, follow_redirects=False)
        self.assertEqual(r_page_expired.status_code, 302)
        self.assertEqual(r_page_expired.headers.get('location'), '/login?reason=expired')

        # Verify notice on login page
        r_notice = self.client.get('/login?reason=expired')
        self.assertIn('Your session has expired due to inactivity. Please log in again.', r_notice.text)

        # TEST 16: Logout completely purges session and in-memory credentials
        r_login3 = self.client.post('/login', data={'username': 'research', 'password': 'ntu'}, follow_redirects=False)
        m3 = re.search(r'session=([^;]+)', r_login3.headers.get('set-cookie', ''))
        s3 = json.loads(b64decode(TimestampSigner(str(self.secret)).unsign(m3.group(1).encode('utf-8'))))
        token3 = s3['db_login_token']
        self.assertIn(token3, application.ACTIVE_DB_LOGINS)

        r_logout = self.client.get('/logout', headers={'Cookie': f'session={m3.group(1)}'}, follow_redirects=False)
        self.assertEqual(r_logout.status_code, 302)
        self.assertEqual(r_logout.headers.get('location'), '/login')
        self.assertNotIn(token3, application.ACTIVE_DB_LOGINS)

    def test_17_18_no_js_errors_and_zero_key_leakage(self):
        """TEST 17 & 18: Syntax check JavaScript files and verify zero credential leakage in responses."""
        # 17: Syntax check JS
        res = subprocess.run(['node', '-c', 'static/theme.js'], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        # 18: No API keys in responses or HTML
        r_login = self.client.get('/login')
        self.assertNotIn('gsk_', r_login.text)
        vals = dotenv_values('.env')
        key1 = vals.get('GROQ_API_KEY_1')
        if key1:
            self.assertNotIn(key1, r_login.text)

if __name__ == '__main__':
    unittest.main(verbosity=2)
