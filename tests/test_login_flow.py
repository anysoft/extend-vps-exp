import json
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    def rejection_page(self, message):
        page = MagicMock()
        page.url = main.LOGIN_URL
        page.locator.return_value.all_inner_texts = AsyncMock(return_value=[message])
        return page

    async def test_locked_account_stops_before_credentials_or_solver(self):
        page = self.rejection_page('アカウントを一時的にロックしました。24時間後に解除されます')
        with patch.object(main, 'ensure_login_turnstile', new=AsyncMock()) as verify:
            with self.assertRaisesRegex(main.LoginRejectedError, '24 小时'):
                await main.submit_primary_login(page, 'user', 'password')
        verify.assert_not_awaited()
        page.locator.return_value.fill.assert_not_called()

    async def test_password_error_after_click_prevents_dom_fallback_and_retries(self):
        page = self.rejection_page('メールアドレスまたはパスワードが正しくありません')
        button = MagicMock()
        button.click = AsyncMock()
        button.evaluate = AsyncMock()
        with self.assertRaisesRegex(main.LoginRejectedError, '密码校验失败'):
            await main.click_submit_resiliently(
                button, 'Primary login button',
                success_check=lambda: main.is_login_transition_started(page),
            )
        button.click.assert_awaited_once()
        button.evaluate.assert_not_awaited()

    async def test_browser_retry_does_not_retry_login_rejection(self):
        operation = AsyncMock(side_effect=main.LoginRejectedError('locked'))
        with self.assertRaises(main.LoginRejectedError):
            await main.retry_browser_operation(operation, 'dashboard')
        operation.assert_awaited_once()

    async def test_dashboard_recovery_does_not_swallow_login_rejection(self):
        page = self.rejection_page('アカウントをロックしました')
        with (
            patch.object(main, 'safe_is_visible', new=AsyncMock(return_value=False)),
            patch.object(main, 'goto_with_retries', new=AsyncMock()) as goto,
        ):
            page.locator.return_value.first.is_visible = AsyncMock(return_value=False)
            with self.assertRaises(main.LoginRejectedError):
                await main.ensure_dashboard_loaded(page, '', 'user', 'password')
        goto.assert_not_awaited()

    async def test_empty_error_message_allows_login(self):
        await main.raise_if_login_rejected(self.rejection_page('  '))

    async def test_login_without_widget_does_not_invoke_solver(self):
        page = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=0)
        with patch.object(main, 'ClickSolver') as solver:
            await main.ensure_login_turnstile(page)
        solver.assert_not_called()

    async def test_already_verified_widget_does_not_invoke_solver(self):
        page = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.evaluate = AsyncMock(return_value=True)
        with patch.object(main, 'ClickSolver') as solver:
            await main.ensure_login_turnstile(page)
        solver.assert_not_called()

    async def test_solver_error_with_valid_token_still_allows_login(self):
        page = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.evaluate = AsyncMock(side_effect=[False, False, True])
        solver = AsyncMock()
        solver.solve_captcha.side_effect = RuntimeError('success element missing')
        manager = AsyncMock()
        manager.__aenter__.return_value = solver
        with patch.object(main, 'ClickSolver', return_value=manager):
            await main.ensure_login_turnstile(page)
        solver.solve_captcha.assert_awaited_once()

    async def test_solver_success_without_token_is_rejected(self):
        page = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.evaluate = AsyncMock(return_value=False)
        with (
            patch.object(main, 'ClickSolver', return_value=AsyncMock()),
            patch.object(main, 'wait_for_success', new=AsyncMock(return_value=False)),
        ):
            with self.assertRaisesRegex(TimeoutError, 'credentials were not submitted'):
                await main.ensure_login_turnstile(page)

    async def test_token_arriving_during_checkbox_wait_cancels_and_cleans_solver(self):
        page = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        verified = asyncio.Event()
        cancelled = asyncio.Event()

        async def completed(page):
            return verified.is_set()

        async def solve(**kwargs):
            verified.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        solver = AsyncMock()
        solver.solve_captcha.side_effect = solve
        manager = AsyncMock()
        manager.__aenter__.return_value = solver

        async def cleanup(*args):
            self.assertTrue(cancelled.is_set())
            return False

        manager.__aexit__.side_effect = cleanup
        with (
            patch.object(main, 'ClickSolver', return_value=manager),
            patch.object(main, 'login_turnstile_completed', side_effect=completed),
        ):
            await asyncio.wait_for(main.ensure_login_turnstile(page), timeout=2)
        manager.__aexit__.assert_awaited_once()

    async def test_failed_verification_prevents_login_click(self):
        page = MagicMock()
        page.locator.return_value.fill = AsyncMock()
        with (
            patch.object(main, 'wait_for_login_entry_state', new=AsyncMock(return_value='login_form')),
            patch.object(main, 'ensure_login_turnstile', new=AsyncMock(side_effect=TimeoutError('verification'))),
            patch.object(main, 'click_submit_resiliently', new=AsyncMock()) as click,
        ):
            with self.assertRaises(TimeoutError):
                await main.submit_primary_login(page, 'test', 'test')
        click.assert_not_awaited()

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
