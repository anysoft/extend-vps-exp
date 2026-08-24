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


if __name__ == '__main__':
    unittest.main()
