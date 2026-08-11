import os
import unittest
from unittest.mock import patch

import main


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bark_sends_to_comma_separated_device_keys(self):
        requests = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def text(self):
                return '{"code": 200}'

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def post(self, endpoint, json):
                requests.append((endpoint, json))
                return Response()

        with patch.object(main.aiohttp, 'ClientSession', return_value=Session()):
            result = await main.send_bark_notice(
                'https://api.day.app',
                ' first-key, , second-key ',
                'Title\nMessage',
            )

        self.assertTrue(result)
        self.assertEqual(
            [payload['device_key'] for _, payload in requests],
            ['first-key', 'second-key'],
        )
        self.assertTrue(all(endpoint.endswith('/push') for endpoint, _ in requests))

    async def test_enabled_channels_are_sent_serially_in_documented_order(self):
        send_order = []

        async def telegram(*args):
            send_order.append('telegram')
            return True

        async def dingtalk(*args):
            send_order.append('dingtalk')
            return True

        async def bark(*args):
            send_order.append('bark')
            return True

        async def lark(*args):
            send_order.append('lark')
            return True

        env = {
            'NOTICE_TG_ENABLED': 'true',
            'NOTICE_TG_TOKEN': 'token',
            'NOTICE_TG_USERID': 'chat',
            'NOTICE_DINGTALK_ENABLED': 'true',
            'NOTICE_DINGTALK_WEBHOOK': 'https://example.com/dingtalk',
            'NOTICE_BARK_ENABLED': 'true',
            'NOTICE_BARK_DEVICE_KEY': 'device',
            'NOTICE_LARK_ENABLED': 'true',
            'NOTICE_LARK_WEBHOOK': 'https://example.com/lark',
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main, 'send_telegram_notice', side_effect=telegram),
            patch.object(main, 'send_dingtalk_notice', side_effect=dingtalk),
            patch.object(main, 'send_bark_notice', side_effect=bark),
            patch.object(main, 'send_lark_notice', side_effect=lark),
        ):
            results = await main.send_notice('✅ test')

        self.assertEqual(send_order, ['telegram', 'dingtalk', 'bark', 'lark'])
        self.assertEqual(
            results,
            {'telegram': True, 'dingtalk': True, 'bark': True, 'lark': True},
        )

    async def test_channel_failure_does_not_block_later_channels(self):
        send_order = []

        async def telegram(*args):
            send_order.append('telegram')
            return False

        async def bark(*args):
            send_order.append('bark')
            return True

        env = {
            'NOTICE_TG_ENABLED': 'true',
            'NOTICE_TG_TOKEN': 'token',
            'NOTICE_TG_USERID': 'chat',
            'NOTICE_BARK_ENABLED': 'true',
            'NOTICE_BARK_DEVICE_KEY': 'device',
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main, 'send_telegram_notice', side_effect=telegram),
            patch.object(main, 'send_bark_notice', side_effect=bark),
        ):
            results = await main.send_notice('❌ test')

        self.assertEqual(send_order, ['telegram', 'bark'])
        self.assertEqual(results, {'telegram': False, 'bark': True})

    def test_legacy_telegram_credentials_enable_telegram_when_switch_is_absent(self):
        with patch.dict(
            os.environ,
            {'NOTICE_TG_TOKEN': 'token', 'NOTICE_TG_USERID': 'chat'},
            clear=True,
        ):
            self.assertTrue(
                main.env_flag(
                    'NOTICE_TG_ENABLED',
                    default=bool(
                        os.getenv('NOTICE_TG_TOKEN') and os.getenv('NOTICE_TG_USERID')
                    ),
                )
            )

    def test_lark_signature_matches_official_hmac_format(self):
        self.assertEqual(
            main.build_lark_signature('demo', 1599360473),
            'l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=',
        )

    def test_error_notice_uses_only_the_first_line(self):
        error = RuntimeError('Locator.click: Timeout 30000ms exceeded.\nCall log:\n- waiting')

        self.assertEqual(
            main.summarize_error_for_notice(error),
            'Locator.click: Timeout 30000ms exceeded.',
        )


if __name__ == '__main__':
    unittest.main()
