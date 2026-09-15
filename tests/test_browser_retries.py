import unittest
from unittest.mock import AsyncMock, patch

import main


class RetryBrowserOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_twice_after_transient_failures(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TimeoutError(f'transient failure {attempts}')
            return 'loaded'

        with patch.object(main.asyncio, 'sleep', new=AsyncMock()) as sleep:
            result = await main.retry_browser_operation(
                operation,
                'Test browser operation',
            )

        self.assertEqual(result, 'loaded')
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_does_not_retry_when_failed_operation_already_succeeded(self):
        attempts = 0
        completed = False

        async def operation():
            nonlocal attempts, completed
            attempts += 1
            completed = True
            raise TimeoutError('response arrived after the browser timeout')

        async def success_check():
            return completed

        result = await main.retry_browser_operation(
            operation,
            'Test browser operation',
            success_check=success_check,
            retry_delay_seconds=0,
        )

        self.assertIsNone(result)
        self.assertEqual(attempts, 1)


class ResilientClickTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_uses_three_total_attempts(self):
        class Locator:
            def __init__(self):
                self.click_calls = 0
                self.evaluate_calls = 0
                self.completed = False

            async def click(self, **kwargs):
                self.click_calls += 1
                if self.click_calls < 3:
                    raise TimeoutError('click timed out')
                self.completed = True

            async def evaluate(self, script):
                self.evaluate_calls += 1
                raise TimeoutError('DOM click timed out')

        locator = Locator()

        async def success_check():
            return locator.completed

        with patch.object(main.asyncio, 'sleep', new=AsyncMock()):
            await main.click_submit_resiliently(
                locator,
                'Test button',
                timeout=1,
                success_check=success_check,
            )

        self.assertEqual(locator.click_calls, 3)
        self.assertEqual(locator.evaluate_calls, 2)

    async def test_click_timeout_does_not_repeat_after_page_advanced(self):
        class Locator:
            def __init__(self):
                self.click_calls = 0
                self.evaluate_calls = 0
                self.completed = False

            async def click(self, **kwargs):
                self.click_calls += 1
                self.completed = True
                raise TimeoutError('click timed out while navigation completed')

            async def evaluate(self, script):
                self.evaluate_calls += 1

        locator = Locator()

        async def success_check():
            return locator.completed

        await main.click_submit_resiliently(
            locator,
            'Test button',
            timeout=1,
            success_check=success_check,
        )

        self.assertEqual(locator.click_calls, 1)
        self.assertEqual(locator.evaluate_calls, 0)


class RenewalRetryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_is_detected_without_waiting_for_confirmation_timeout(self):
        class HiddenLocator:
            @property
            def first(self):
                return self

            async def is_visible(self, timeout=None):
                return False

        class Page:
            url = main.DASHBOARD_URL

            def locator(self, selector):
                return HiddenLocator()

        self.assertEqual(
            await main.wait_for_renewal_page_or_status(Page(), timeout_ms=1000),
            'dashboard',
        )

    async def test_retry_rebuilds_confirmation_from_detail_instead_of_reloading(self):
        class Page:
            def __init__(self):
                self.wait_for_selector = AsyncMock()
                self.reload = AsyncMock(side_effect=AssertionError('POST confirmation must not be reloaded'))

            def locator(self, selector):
                return f'locator:{selector}'

        page = Page()
        detail_url = 'https://secure.xserver.ne.jp/xapanel/xvps/server/detail?id=123'

        with (
            patch.object(main, 'goto_with_retries', new=AsyncMock()) as goto,
            patch.object(main, 'click_submit_resiliently', new=AsyncMock()) as click,
            patch.object(
                main,
                'open_free_renewal_confirmation',
                new=AsyncMock(return_value='captcha'),
            ) as open_confirmation,
        ):
            state = await main.reopen_renewal_confirmation_from_detail(page, detail_url)

        self.assertEqual(state, 'captcha')
        goto.assert_awaited_once_with(page, detail_url, 'Reopen server detail for renewal retry')
        page.wait_for_selector.assert_awaited_once_with('text="更新する"', timeout=30000)
        click.assert_awaited_once()
        open_confirmation.assert_awaited_once_with(page)
        page.reload.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
