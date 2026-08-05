import json
import tempfile
import time
import unittest
from pathlib import Path

import main


class FakeLocator:
    def __init__(self, visible=False, checked=False):
        self.visible = visible
        self.checked = checked
        self.check_calls = 0

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=None):
        return self.visible

    async def wait_for(self, state=None, timeout=None):
        return None

    async def is_checked(self):
        return self.checked

    async def check(self, force=False, timeout=None):
        self.check_calls += 1
        self.checked = True

    async def evaluate(self, script):
        self.checked = True


class FakePage:
    def __init__(self, url, checkbox=None):
        self.url = url
        self.checkbox = checkbox or FakeLocator()
        self.goto_calls = []

    def locator(self, selector):
        if selector == main.OTP_REMEMBER_DEVICE_SELECTOR:
            return self.checkbox
        return FakeLocator()

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url


class FakeContext:
    async def storage_state(self):
        return {
            'cookies': [
                {
                    'name': 'trusted_device',
                    'value': 'trusted',
                    'domain': '.secure.xserver.ne.jp',
                    'path': '/',
                    'expires': time.time() + 30 * 24 * 60 * 60,
                },
                {
                    'name': 'login_session',
                    'value': 'session',
                    'domain': 'secure.xserver.ne.jp',
                    'path': '/',
                    'expires': -1,
                },
                {
                    'name': 'unrelated',
                    'value': 'other',
                    'domain': '.example.com',
                    'path': '/',
                    'expires': time.time() + 30 * 24 * 60 * 60,
                },
            ],
            'origins': [{'origin': 'https://secure.xserver.ne.jp', 'localStorage': []}],
        }


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_remember_device_before_otp_submission(self):
        checkbox = FakeLocator(checked=False)
        page = FakePage('https://secure.xserver.ne.jp/xapanel/myaccount/twostepauth/index', checkbox)

        result = await main.enable_otp_remember_device(page)

        self.assertTrue(result)
        self.assertTrue(checkbox.checked)
        self.assertEqual(checkbox.check_calls, 1)

    async def test_detects_agreement_as_a_post_login_state(self):
        page = FakePage('https://secure.xserver.ne.jp/xapanel/myaccount/agreement/index')

        self.assertEqual(await main.detect_login_state(page), 'agreement')
        self.assertTrue(await main.is_login_transition_started(page))
        self.assertTrue(await main.is_otp_or_dashboard_transition_complete(page))

    async def test_redirects_agreement_page_to_dashboard(self):
        page = FakePage('https://secure.xserver.ne.jp/xapanel/myaccount/agreement/index')

        redirected = await main.redirect_agreement_to_dashboard(page)

        self.assertTrue(redirected)
        self.assertEqual(page.goto_calls, [main.DASHBOARD_URL])

    async def test_saves_only_persistent_xserver_cookies(self):
        original_auth_state_file = main.AUTH_STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                main.AUTH_STATE_FILE = Path(temp_dir) / 'browser_state.json'

                await main.save_browser_auth_state(FakeContext())

                saved_state = json.loads(main.AUTH_STATE_FILE.read_text(encoding='utf-8'))
                self.assertEqual(
                    [cookie['name'] for cookie in saved_state['cookies']],
                    ['trusted_device'],
                )
                self.assertEqual(saved_state['origins'], [])
                self.assertEqual(main.AUTH_STATE_FILE.stat().st_mode & 0o777, 0o600)
        finally:
            main.AUTH_STATE_FILE = original_auth_state_file


if __name__ == '__main__':
    unittest.main()
