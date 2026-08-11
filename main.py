import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import sys
import logging
import aiohttp
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from browserforge.fingerprints import Screen
from camoufox.async_api import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path
from renewal_timing import (
    is_in_renewal_window,
    now_in_jst,
    renewal_window_from_state,
    should_attempt_login_from_state,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

STATE_FILE = Path(__file__).resolve().parent / 'renewal_state.json'
AUTH_STATE_FILE = Path(__file__).resolve().parent / 'browser_state.json'
DASHBOARD_URL = 'https://secure.xserver.ne.jp/xapanel/xvps/index'
LOGIN_URL = 'https://secure.xserver.ne.jp/xapanel/login/xvps/'
OTP_PATH = '/xapanel/myaccount/twostepauth/index'
OTP_DO_PATH = '/xapanel/myaccount/twostepauth/do'
AGREEMENT_PATH = '/xapanel/myaccount/agreement/index'
DASHBOARD_DETAIL_LINK_SELECTOR = 'a[href^="/xapanel/xvps/server/detail?id="]'
LOGIN_MEMBER_ID_SELECTOR = '#memberid'
LOGIN_PASSWORD_SELECTOR = '#user_password'
OTP_INPUT_SELECTOR = 'input[name="auth_code"]'
OTP_REMEMBER_DEVICE_SELECTOR = 'input[name="remember_device"][value="1"]'
OTP_SUBMIT_SELECTOR = (
    'input[type="submit"][value="ログイン"], '
    'button:has-text("ログイン"), '
    'input[type="submit"], '
    'button[type="submit"]'
)
OTP_SUBMIT_DOM_SELECTOR = 'input[type="submit"][value="ログイン"], input[type="submit"], button[type="submit"]'
CAPTCHA_IMAGE_SELECTOR = 'img[src^="data:"]'
SUSPENSION_SELECTOR = '.newApp__suspended'
RENEWAL_CONFIRM_PATH = '/xapanel/xvps/server/freevps/extend/conf'
FREE_RENEW_SELECTION_TEXT = '引き続き無料VPSの利用を継続する'
FREE_RENEW_SELECTION_BUTTON_SELECTOR = (
    f'button[formaction="{RENEWAL_CONFIRM_PATH}"], '
    'form.freeVpsBtnForm button[type="submit"]'
)
FREE_RENEW_SELECTION_TEXT_SELECTOR = f'text="{FREE_RENEW_SELECTION_TEXT}"'
FINAL_RENEW_BUTTON_TEXT = '無料VPSの利用を継続する'
FINAL_RENEW_BUTTON_SELECTOR = (
    f'input[type="submit"][value="{FINAL_RENEW_BUTTON_TEXT}"], '
    f'button:has-text("{FINAL_RENEW_BUTTON_TEXT}"), '
    f'a:has-text("{FINAL_RENEW_BUTTON_TEXT}")'
)
FINAL_RENEW_ATTEMPTS = 3

ENV_KEYS = (
    'EMAIL',
    'PASSWORD',
    'AUTH_LOGIN_OTP',
    'PROXY_SERVER',
    'NOTICE_TG_TOKEN',
    'NOTICE_TG_USERID',
    'NOTICE_TG_ENABLED',
    'NOTICE_DINGTALK_ENABLED',
    'NOTICE_DINGTALK_WEBHOOK',
    'NOTICE_DINGTALK_SECRET',
    'NOTICE_BARK_ENABLED',
    'NOTICE_BARK_SERVER',
    'NOTICE_BARK_DEVICE_KEY',
    'NOTICE_BARK_GROUP',
    'NOTICE_BARK_SOUND',
    'NOTICE_LARK_ENABLED',
    'NOTICE_LARK_WEBHOOK',
    'NOTICE_LARK_SECRET',
    'DEBUG',
)


def load_local_env():
    missing_keys = [key for key in ENV_KEYS if not os.getenv(key)]
    if not missing_keys:
        return

    env_path = Path(__file__).resolve().parent / '.env'
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in missing_keys and value:
            os.environ[key] = value


def generate_totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    normalized = ''.join(secret.split()).upper()
    padding = '=' * ((8 - len(normalized) % 8) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except binascii.Error as exc:
        raise ValueError('AUTH_LOGIN_OTP is not a valid base32 TOTP secret.') from exc

    counter = int(time.time() // interval)
    counter_bytes = counter.to_bytes(8, 'big')
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = int.from_bytes(digest[offset:offset + 4], 'big') & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


async def wait_for_fresh_totp_window(interval: int = 30, min_remaining: int = 8):
    remaining = interval - (int(time.time()) % interval)
    if remaining < min_remaining:
        wait_seconds = remaining + 1
        logging.info('TOTP code is near expiry (%ss remaining). Waiting %ss for a fresh window...', remaining, wait_seconds)
        await asyncio.sleep(wait_seconds)


def parse_japanese_date(raw: str) -> date:
    normalized = raw.strip().replace('年', '-').replace('月', '-').replace('日', '')
    return datetime.strptime(normalized, '%Y-%m-%d').date()


def load_local_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception as exc:
        logging.warning(f'Failed to read local renewal state: {exc}')
        return {}


async def create_browser_context(browser):
    context_options = {'no_viewport': True}
    if AUTH_STATE_FILE.exists():
        context_options['storage_state'] = str(AUTH_STATE_FILE)

    try:
        context = await browser.new_context(**context_options)
        if 'storage_state' in context_options:
            logging.info('Loaded saved browser authentication state from %s.', AUTH_STATE_FILE.name)
        return context
    except Exception as exc:
        if 'storage_state' not in context_options:
            raise
        logging.warning('Saved browser authentication state could not be loaded; using a fresh context: %s', exc)
        return await browser.new_context(no_viewport=True)


async def save_browser_auth_state(context):
    try:
        browser_state = await context.storage_state()
        current_epoch = time.time()
        trusted_device_cookies = [
            cookie
            for cookie in browser_state.get('cookies', [])
            if (
                cookie.get('domain', '').lstrip('.').lower() == 'xserver.ne.jp'
                or cookie.get('domain', '').lstrip('.').lower().endswith('.xserver.ne.jp')
            )
            and isinstance(cookie.get('expires'), (int, float))
            and cookie['expires'] > current_epoch
        ]
        trusted_device_state = {
            'cookies': trusted_device_cookies,
            'origins': [],
        }
        temp_state_file = AUTH_STATE_FILE.with_suffix('.tmp')
        temp_state_file.write_text(
            json.dumps(trusted_device_state, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        temp_state_file.chmod(0o600)
        temp_state_file.replace(AUTH_STATE_FILE)
        logging.info(
            'Saved %s persistent XServer trusted-device cookie(s) to %s.',
            len(trusted_device_cookies),
            AUTH_STATE_FILE.name,
        )
    except Exception as exc:
        logging.warning('Could not save trusted-device browser state: %s', exc)


def save_local_state(info: dict, today_jst: date):
    if not info.get('expiry_date_raw'):
        return

    previous = load_local_state()
    next_expiry_date = parse_japanese_date(info['expiry_date_raw']).isoformat()
    payload = {
        'next_expiry_date': next_expiry_date,
        'expiry_date_raw': info['expiry_date_raw'],
        'update_date_raw': info.get('update_date_raw', ''),
        'service_code': info.get('service_code', ''),
        'server_name': info.get('server_name', ''),
        'uuid': info.get('uuid', ''),
        'last_checked_jst': today_jst.isoformat(),
        'updated_at_utc': datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    }
    for key in ('last_notice_jst', 'last_notice_reason'):
        if previous.get(key):
            payload[key] = previous[key]

    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    logging.info('Local renewal state updated: %s', payload['next_expiry_date'])


def mark_notice_sent(today_jst: date, reason: str):
    state = load_local_state()
    state['last_notice_jst'] = today_jst.isoformat()
    state['last_notice_reason'] = reason
    state['notice_updated_at_utc'] = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def should_send_daily_notice(today_jst: date, reason: str) -> bool:
    state = load_local_state()
    return not (
        state.get('last_notice_jst') == today_jst.isoformat()
        and state.get('last_notice_reason') == reason
    )


def format_server_info_message(prefix: str, info: dict, today_jst: date, should_renew: bool | None = None) -> str:
    lines = [prefix]

    if info.get('server_name'):
        lines.append(f"服务器: {info['server_name']}")
    if info.get('service_code'):
        lines.append(f"服务代码: {info['service_code']}")
    if info.get('uuid'):
        lines.append(f"UUID: {info['uuid']}")
    if info.get('expiry_date_raw'):
        lines.append(f"到期时间: {info['expiry_date_raw']}")
    if info.get('update_date_raw'):
        lines.append(f"上次更新: {info['update_date_raw']}")

    lines.append(f"日本日期: {today_jst.isoformat()}")
    if should_renew is not None:
        lines.append(f"处于续期窗口: {'是' if should_renew else '否'}")

    return '\n'.join(lines)


def summarize_error_for_notice(exc: Exception, max_length: int = 300) -> str:
    first_line = next((line.strip() for line in str(exc).splitlines() if line.strip()), '')
    summary = first_line or exc.__class__.__name__
    if len(summary) > max_length:
        return summary[:max_length - 1] + '…'
    return summary


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


async def send_telegram_notice(token: str, user_id: str, message: str) -> bool:
    if not token or not user_id:
        logging.warning('Telegram notification is enabled but token or user ID is missing.')
        return False

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = {
        'chat_id': user_id,
        'text': message,
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logging.warning('Telegram notification failed: HTTP %s %s', resp.status, body)
                    return False
                logging.info('Telegram notification sent successfully.')
                return True
    except Exception as exc:
        logging.warning('Telegram notification raised an exception: %s', exc)
        return False


def build_dingtalk_webhook(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    if not secret:
        return webhook

    timestamp_ms = timestamp_ms or int(time.time() * 1000)
    string_to_sign = f'{timestamp_ms}\n{secret}'.encode('utf-8')
    signature = base64.b64encode(
        hmac.new(secret.encode('utf-8'), string_to_sign, hashlib.sha256).digest()
    ).decode('utf-8')
    parsed = urlparse(webhook)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({'timestamp': str(timestamp_ms), 'sign': signature})
    return urlunparse(parsed._replace(query=urlencode(query)))


async def send_dingtalk_notice(webhook: str, secret: str, message: str) -> bool:
    if not webhook:
        logging.warning('DingTalk notification is enabled but webhook is missing.')
        return False

    url = build_dingtalk_webhook(webhook, secret)
    payload = {
        'msgtype': 'text',
        'text': {'content': message},
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logging.warning('DingTalk notification failed: HTTP %s %s', resp.status, body)
                    return False
                try:
                    result = json.loads(body)
                except json.JSONDecodeError:
                    result = {}
                if result.get('errcode', 0) != 0:
                    logging.warning('DingTalk notification rejected: %s', body)
                    return False
                logging.info('DingTalk notification sent successfully.')
                return True
    except Exception as exc:
        logging.warning('DingTalk notification raised an exception: %s', exc)
        return False


async def send_bark_notice(
    server: str,
    device_key: str,
    message: str,
    group: str = 'XServer VPS',
    sound: str = '',
) -> bool:
    keys = [key.strip() for key in device_key.split(',') if key.strip()]
    if not keys:
        logging.warning('Bark notification is enabled but device key is missing.')
        return False

    endpoint = (server or 'https://api.day.app').rstrip('/')
    if not endpoint.endswith('/push'):
        endpoint += '/push'

    message_lines = message.splitlines()
    title = message_lines[0] if message_lines else 'XServer VPS 通知'
    body = '\n'.join(message_lines[1:]) or title
    payload = {
        'title': title,
        'body': body,
        'group': group or 'XServer VPS',
    }
    if sound:
        payload['sound'] = sound

    all_succeeded = True
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            for key in keys:
                try:
                    async with session.post(endpoint, json={**payload, 'device_key': key}) as resp:
                        response_body = await resp.text()
                        if resp.status >= 400:
                            logging.warning(
                                'Bark notification failed: HTTP %s %s',
                                resp.status,
                                response_body,
                            )
                            all_succeeded = False
                            continue
                        try:
                            result = json.loads(response_body)
                        except json.JSONDecodeError:
                            result = {}
                        if result.get('code', 200) != 200:
                            logging.warning('Bark notification rejected: %s', response_body)
                            all_succeeded = False
                            continue
                        logging.info('Bark notification sent successfully.')
                except Exception as exc:
                    logging.warning('Bark notification raised an exception: %s', exc)
                    all_succeeded = False
            return all_succeeded
    except Exception as exc:
        logging.warning('Bark notification raised an exception: %s', exc)
        return False


def build_lark_signature(secret: str, timestamp: int) -> str:
    string_to_sign = f'{timestamp}\n{secret}'.encode('utf-8')
    signature = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(signature).decode('utf-8')


async def send_lark_notice(webhook: str, secret: str, message: str) -> bool:
    if not webhook:
        logging.warning('Lark notification is enabled but webhook is missing.')
        return False

    payload = {
        'msg_type': 'text',
        'content': {'text': message},
    }
    if secret:
        timestamp = int(time.time())
        payload.update({
            'timestamp': str(timestamp),
            'sign': build_lark_signature(secret, timestamp),
        })

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(webhook, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logging.warning('Lark notification failed: HTTP %s %s', resp.status, body)
                    return False
                try:
                    result = json.loads(body)
                except json.JSONDecodeError:
                    result = {}
                result_code = result.get('code', result.get('StatusCode', 0))
                if result_code != 0:
                    logging.warning('Lark notification rejected: %s', body)
                    return False
                logging.info('Lark notification sent successfully.')
                return True
    except Exception as exc:
        logging.warning('Lark notification raised an exception: %s', exc)
        return False


async def send_notice(message: str) -> dict[str, bool]:
    """Send enabled notifications serially: Telegram, DingTalk, Bark, then Lark."""
    tg_token = os.getenv('NOTICE_TG_TOKEN', '')
    tg_user_id = os.getenv('NOTICE_TG_USERID', '')
    channels = [
        (
            'telegram',
            env_flag('NOTICE_TG_ENABLED', default=bool(tg_token and tg_user_id)),
            lambda: send_telegram_notice(tg_token, tg_user_id, message),
        ),
        (
            'dingtalk',
            env_flag('NOTICE_DINGTALK_ENABLED'),
            lambda: send_dingtalk_notice(
                os.getenv('NOTICE_DINGTALK_WEBHOOK', ''),
                os.getenv('NOTICE_DINGTALK_SECRET', ''),
                message,
            ),
        ),
        (
            'bark',
            env_flag('NOTICE_BARK_ENABLED'),
            lambda: send_bark_notice(
                os.getenv('NOTICE_BARK_SERVER', 'https://api.day.app'),
                os.getenv('NOTICE_BARK_DEVICE_KEY', ''),
                message,
                os.getenv('NOTICE_BARK_GROUP', 'XServer VPS'),
                os.getenv('NOTICE_BARK_SOUND', ''),
            ),
        ),
        (
            'lark',
            env_flag('NOTICE_LARK_ENABLED'),
            lambda: send_lark_notice(
                os.getenv('NOTICE_LARK_WEBHOOK', ''),
                os.getenv('NOTICE_LARK_SECRET', ''),
                message,
            ),
        ),
    ]

    results = {}
    for channel_name, enabled, sender in channels:
        if not enabled:
            continue
        try:
            results[channel_name] = await sender()
        except Exception as exc:
            logging.warning('%s notification failed unexpectedly: %s', channel_name, exc)
            results[channel_name] = False
    return results


async def send_tg_notice(token: str, user_id: str, message: str) -> bool:
    """Backward-compatible Telegram-only wrapper."""
    return await send_telegram_notice(token, user_id, message)


async def extract_server_info(page) -> dict:
    return await page.eval_on_selector_all(
        'table.table tr',
        """
        rows => {
            const result = {};
            for (const row of rows) {
                const th = row.querySelector('th');
                const td = row.querySelector('td');
                if (!th || !td) continue;
                const key = th.textContent.trim();
                const value = td.innerText.trim().replace(/\\s+/g, ' ');
                result[key] = value;
            }
            return result;
        }
        """
    )


def normalize_server_info(server_info_raw: dict) -> dict:
    return {
        'uuid': server_info_raw.get('UUID', ''),
        'server_name': server_info_raw.get('サーバー名', ''),
        'expiry_date_raw': server_info_raw.get('利用期限', '').split('更新する')[0].strip(),
        'update_date_raw': server_info_raw.get('更新', ''),
        'service_code': server_info_raw.get('サービスコード', ''),
    }


async def safe_is_visible(locator, timeout: int = 500) -> bool:
    try:
        return await locator.first.is_visible(timeout=timeout)
    except Exception:
        return False


async def capture_diagnostics(page, prefix: str):
    try:
        logging.error('Diagnostic page URL: %s', page.url)
    except Exception:
        pass

    try:
        logging.error('Diagnostic page title: %s', await page.title())
    except Exception as exc:
        logging.warning('Could not read diagnostic page title: %s', exc)

    try:
        body_text = await page.locator('body').inner_text(timeout=2000)
        logging.error('Diagnostic body excerpt: %s', body_text[:1000].replace('\n', ' | '))
    except Exception as exc:
        logging.warning('Could not read diagnostic body text: %s', exc)

    try:
        await page.screenshot(path=f'{prefix}.png', full_page=True)
        logging.error('Diagnostic screenshot saved: %s.png', prefix)
    except Exception as exc:
        logging.warning('Could not save diagnostic screenshot: %s', exc)

    try:
        Path(f'{prefix}.html').write_text(await page.content(), encoding='utf-8')
        logging.error('Diagnostic HTML saved: %s.html', prefix)
    except Exception as exc:
        logging.warning('Could not save diagnostic HTML: %s', exc)


async def detect_login_state(page) -> str:
    current_url = page.url

    if await safe_is_visible(page.locator(DASHBOARD_DETAIL_LINK_SELECTOR)):
        return 'dashboard'
    if AGREEMENT_PATH in current_url:
        return 'agreement'
    if OTP_DO_PATH in current_url:
        return 'otp_submitted'
    if await safe_is_visible(page.locator(OTP_INPUT_SELECTOR)):
        return 'otp_form'
    if await safe_is_visible(page.locator(LOGIN_MEMBER_ID_SELECTOR)):
        return 'login_form'
    if '/xapanel/xvps/' in current_url:
        return 'xvps_area'
    if OTP_PATH in current_url:
        return 'otp_loading'
    if '/xapanel/login/' in current_url:
        return 'login_loading'
    return 'unknown'


async def wait_for_login_entry_state(page, timeout_ms: int = 45000) -> str:
    deadline = time.time() + timeout_ms / 1000
    last_logged_state = None

    while time.time() < deadline:
        state = await detect_login_state(page)
        if state != last_logged_state:
            logging.info('Login entry state: %s url=%s', state, page.url)
            last_logged_state = state

        if state in ('login_form', 'otp_form', 'otp_submitted', 'agreement', 'dashboard', 'xvps_area'):
            return state

        await asyncio.sleep(0.5)

    await capture_diagnostics(page, 'login_entry_timeout')
    raise TimeoutError(f'Timed out waiting for login, OTP, or dashboard state. Last state: {last_logged_state}. url={page.url}')


async def wait_for_otp_state(page, timeout_ms: int = 45000) -> str:
    deadline = time.time() + timeout_ms / 1000
    last_logged_state = None

    while time.time() < deadline:
        state = await detect_login_state(page)
        if state != last_logged_state:
            logging.info('OTP wait state: %s url=%s', state, page.url)
            last_logged_state = state

        if state in ('otp_form', 'otp_submitted', 'agreement', 'dashboard', 'xvps_area', 'login_form'):
            return state

        await asyncio.sleep(0.5)

    await capture_diagnostics(page, 'otp_wait_timeout')
    raise TimeoutError(f'Timed out waiting for OTP input or dashboard transition. Last state: {last_logged_state}. url={page.url}')


async def is_free_renewal_selection_visible(page) -> bool:
    return (
        await safe_is_visible(page.locator(FREE_RENEW_SELECTION_BUTTON_SELECTOR))
        or await safe_is_visible(page.locator(FREE_RENEW_SELECTION_TEXT_SELECTOR))
    )


async def wait_for_renewal_page_or_status(page, timeout_ms: int = 30000) -> str:
    deadline = time.time() + timeout_ms / 1000
    last_logged_state = None

    while time.time() < deadline:
        if await safe_is_visible(page.locator(SUSPENSION_SELECTOR)):
            state = 'suspended'
        elif await safe_is_visible(page.locator(CAPTCHA_IMAGE_SELECTOR)):
            state = 'captcha'
        elif await is_free_renewal_selection_visible(page):
            state = 'selection'
        elif RENEWAL_CONFIRM_PATH in page.url:
            state = 'confirmation_loading'
        elif await safe_is_visible(page.locator(FINAL_RENEW_BUTTON_SELECTOR)):
            state = 'final_button'
        else:
            state = 'waiting'

        if state != last_logged_state:
            logging.info('Renewal page state: %s url=%s', state, page.url)
            last_logged_state = state

        if state in ('captcha', 'selection', 'suspended'):
            return state

        await asyncio.sleep(0.5)

    await capture_diagnostics(page, 'renewal_page_timeout')
    raise TimeoutError(f'Timed out waiting for renewal confirmation page. Last state: {last_logged_state}. url={page.url}')


async def is_renewal_confirmation_transition_complete(page) -> bool:
    return (
        RENEWAL_CONFIRM_PATH in page.url
        or await safe_is_visible(page.locator(CAPTCHA_IMAGE_SELECTOR))
        or await safe_is_visible(page.locator(SUSPENSION_SELECTOR))
    )


async def submit_free_renewal_form_fallback(page) -> bool:
    return await page.evaluate(
        """
        (selector) => {
            const button = document.querySelector(selector);
            if (!button) return false;

            const form = button.form || button.closest('form');
            if (!form) {
                button.click();
                return true;
            }

            const action = button.formAction || button.getAttribute('formaction');
            if (action) form.action = action;

            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit(button);
            } else {
                form.submit();
            }
            return true;
        }
        """,
        FREE_RENEW_SELECTION_BUTTON_SELECTOR,
    )


async def open_free_renewal_confirmation(page, max_attempts: int = 3) -> str:
    for attempt in range(1, max_attempts + 1):
        state = await wait_for_renewal_page_or_status(page, timeout_ms=15000)
        if state in ('captcha', 'suspended'):
            return state

        logging.info('Clicking free renewal selection (attempt %s/%s)...', attempt, max_attempts)
        await click_submit_resiliently(
            page.locator(FREE_RENEW_SELECTION_BUTTON_SELECTOR).first,
            'Free renewal selection button',
            timeout=8000,
            success_check=lambda: is_renewal_confirmation_transition_complete(page),
        )

        state = await wait_for_renewal_page_or_status(page, timeout_ms=15000)
        if state in ('captcha', 'suspended'):
            return state

        if state == 'selection':
            logging.info('Free renewal selection click stayed on selection page; submitting the form via DOM fallback...')
            submitted = await submit_free_renewal_form_fallback(page)
            if not submitted:
                logging.warning('Could not find free renewal selection form for DOM fallback.')

            state = await wait_for_renewal_page_or_status(page, timeout_ms=20000)
            if state in ('captcha', 'suspended'):
                return state

        logging.warning('Free renewal selection did not advance to confirmation page on attempt %s.', attempt)

    await capture_diagnostics(page, 'renewal_selection_timeout')
    raise TimeoutError(f'Timed out opening free renewal confirmation page. url={page.url}')


async def wait_for_final_renew_button(page, timeout_ms: int = 60000):
    deadline = time.time() + timeout_ms / 1000
    button = page.locator(FINAL_RENEW_BUTTON_SELECTOR).first
    last_logged_state = None

    while time.time() < deadline:
        if await safe_is_visible(page.locator(SUSPENSION_SELECTOR)):
            state = 'suspended'
        elif await safe_is_visible(page.locator(FREE_RENEW_SELECTION_BUTTON_SELECTOR)):
            state = 'selection'
        elif await safe_is_visible(page.locator(FINAL_RENEW_BUTTON_SELECTOR)):
            return button
        elif RENEWAL_CONFIRM_PATH in page.url:
            state = 'confirmation_waiting_for_final_button'
        else:
            state = 'waiting'

        if state != last_logged_state:
            logging.info('Final renewal button wait state: %s url=%s', state, page.url)
            last_logged_state = state

        if state == 'suspended':
            await capture_diagnostics(page, 'final_button_suspended')
            raise RuntimeError('Renewal page became suspended while waiting for the final renewal button.')

        if state == 'selection':
            logging.info('Returned to renewal selection page while waiting for final button; reopening confirmation...')
            reopened_state = await open_free_renewal_confirmation(page)
            if reopened_state == 'suspended':
                await capture_diagnostics(page, 'final_button_suspended')
                raise RuntimeError('Renewal page became suspended while reopening confirmation.')

        await asyncio.sleep(0.5)

    await capture_diagnostics(page, 'final_button_timeout')
    raise TimeoutError(f'Timed out waiting for final renewal button. Last state: {last_logged_state}. url={page.url}')


async def redirect_agreement_to_dashboard(page) -> bool:
    if AGREEMENT_PATH not in page.url:
        return False

    logging.info('Agreement page detected after login. Opening XVPS dashboard directly...')
    await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
    return True


async def enable_otp_remember_device(page) -> bool:
    checkbox = page.locator(OTP_REMEMBER_DEVICE_SELECTOR).first

    try:
        await checkbox.wait_for(state='attached', timeout=3000)
    except Exception:
        logging.warning('OTP trusted-device checkbox was not found; continuing without 30-day trust.')
        return False

    try:
        if not await checkbox.is_checked():
            await checkbox.check(force=True, timeout=3000)
    except Exception as exc:
        logging.warning('Could not check trusted-device option normally; trying DOM fallback: %s', exc)
        try:
            await checkbox.evaluate(
                """
                el => {
                    el.checked = true;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """
            )
        except Exception as fallback_exc:
            logging.warning('Trusted-device DOM fallback failed: %s', fallback_exc)
            return False

    try:
        is_checked = await checkbox.is_checked()
    except Exception:
        is_checked = False

    if is_checked:
        logging.info('Enabled OTP trusted-device option for 30 days.')
        return True

    logging.warning('OTP trusted-device checkbox did not remain checked.')
    return False


async def complete_optional_otp(page, otp_secret: str):
    for _ in range(60):
        current_url = page.url
        if '/xapanel/xvps/' in current_url:
            return
        if await redirect_agreement_to_dashboard(page):
            return
        if OTP_PATH in current_url or OTP_DO_PATH in current_url:
            await submit_otp_with_retries(page, otp_secret)
            return
        await asyncio.sleep(0.5)

    raise TimeoutError('Timed out waiting for either the XServer dashboard or the two-step authentication page.')


async def submit_otp_with_retries(page, otp_secret: str, max_attempts: int = 3):
    if not otp_secret:
        raise RuntimeError('Two-step authentication page detected but AUTH_LOGIN_OTP is not set.')

    for attempt in range(1, max_attempts + 1):
        state = await wait_for_otp_state(page)

        if state in ('dashboard', 'xvps_area'):
            return

        if state == 'agreement':
            await redirect_agreement_to_dashboard(page)
            return

        if state == 'otp_submitted' or OTP_DO_PATH in page.url:
            logging.info('OTP submit endpoint is still open. Trying dashboard URL directly...')
            await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
            return

        if state == 'login_form':
            raise RuntimeError('Expected two-step authentication, but XServer returned to the primary login form.')

        logging.info('Two-step authentication detected. Submitting TOTP code (attempt %s/%s)...', attempt, max_attempts)

        auth_input = page.locator(OTP_INPUT_SELECTOR)
        submit_button = page.locator(OTP_SUBMIT_SELECTOR).first
        error_message = page.locator('text="認証コードが一致しません"')
        await fill_otp_code(page, auth_input, otp_secret)
        await enable_otp_remember_device(page)
        await submit_otp_and_wait(page, submit_button, 'OTP login button')

        retried_click = False
        for _ in range(30):
            current_url = page.url
            if '/xapanel/xvps/' in current_url:
                return

            if await redirect_agreement_to_dashboard(page):
                return

            if OTP_DO_PATH in current_url:
                logging.info('OTP form reached submit endpoint. Opening dashboard to continue...')
                await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
                return

            if OTP_PATH not in current_url:
                await asyncio.sleep(0.5)
                continue

            try:
                input_value = await auth_input.input_value()
            except Exception:
                input_value = ''

            try:
                has_error = await error_message.is_visible()
            except Exception:
                has_error = False

            if has_error or input_value == '':
                logging.warning('OTP was rejected or cleared by the page. Retrying with a fresh code...')
                break

            if not retried_click:
                logging.info('OTP page is still open; refilling a fresh code and retrying submit once more...')
                await fill_otp_code(page, auth_input, otp_secret)
                await enable_otp_remember_device(page)
                await submit_otp_and_wait(page, submit_button, 'OTP login button retry')
                retried_click = True
                if await is_otp_or_dashboard_transition_complete(page):
                    continue

            await asyncio.sleep(0.5)
        else:
            logging.warning('OTP page did not advance after submission attempt %s.', attempt)

    await capture_diagnostics(page, 'otp_submit_failed')
    raise RuntimeError('Failed to complete two-step authentication after multiple attempts.')


async def submit_primary_login(page, email: str, password: str):
    state = await wait_for_login_entry_state(page, timeout_ms=30000)
    if state != 'login_form':
        logging.info('Primary login form is not needed; current state is %s.', state)
        return

    await page.locator(LOGIN_MEMBER_ID_SELECTOR).fill(email)
    await page.locator(LOGIN_PASSWORD_SELECTOR).fill(password)
    await click_submit_resiliently(
        page.locator('text="ログインする"'),
        'Primary login button',
        timeout=5000,
        success_check=lambda: is_login_transition_started(page),
    )


async def ensure_dashboard_loaded(page, otp_secret: str, email: str, password: str, timeout_ms: int = 90000):
    dashboard_link = page.locator(DASHBOARD_DETAIL_LINK_SELECTOR)
    deadline = time.time() + timeout_ms / 1000
    last_forced_dashboard_visit = 0.0

    while time.time() < deadline:
        current_url = page.url
        can_force_dashboard = (time.time() - last_forced_dashboard_visit) >= 10

        try:
            if await dashboard_link.first.is_visible(timeout=1000):
                return
        except Exception:
            pass

        if AGREEMENT_PATH in current_url and can_force_dashboard:
            await redirect_agreement_to_dashboard(page)
            last_forced_dashboard_visit = time.time()
            continue

        if OTP_PATH in current_url or await safe_is_visible(page.locator(OTP_INPUT_SELECTOR)):
            await submit_otp_with_retries(page, otp_secret)
            continue

        if OTP_DO_PATH in current_url and can_force_dashboard:
            logging.info('Landed on OTP submit endpoint. Trying to open XVPS dashboard directly...')
            await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
            last_forced_dashboard_visit = time.time()
            continue

        if '/xapanel/xvps/' in current_url and can_force_dashboard:
            logging.info('Already inside XVPS area but dashboard is not ready yet. Refreshing dashboard URL...')
            await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
            last_forced_dashboard_visit = time.time()
            continue

        if (current_url == LOGIN_URL or '/xapanel/login/' in current_url) and can_force_dashboard:
            logging.info('Login page still appears active. Resubmitting credentials once before dashboard recovery...')
            try:
                await submit_primary_login(page, email, password)
                last_forced_dashboard_visit = time.time()
                continue
            except Exception as exc:
                if (
                    OTP_PATH in page.url
                    or OTP_DO_PATH in page.url
                    or AGREEMENT_PATH in page.url
                    or '/xapanel/xvps/' in page.url
                ):
                    logging.info('Login resubmit moved to %s; continuing recovery.', page.url)
                    continue
                logging.warning(f'Login resubmit did not finish cleanly: {exc}')

            logging.info('Trying to open XVPS dashboard directly...')
            await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
            last_forced_dashboard_visit = time.time()
            continue

        await asyncio.sleep(1)

    await capture_diagnostics(page, 'dashboard_timeout')
    raise TimeoutError('Timed out waiting for the XServer dashboard to become available.')


async def is_login_transition_started(page) -> bool:
    current_url = page.url
    return (
        OTP_PATH in current_url
        or OTP_DO_PATH in current_url
        or AGREEMENT_PATH in current_url
        or '/xapanel/xvps/' in current_url
    )


async def is_otp_or_dashboard_transition_complete(page) -> bool:
    current_url = page.url
    return (
        OTP_DO_PATH in current_url
        or AGREEMENT_PATH in current_url
        or '/xapanel/xvps/' in current_url
    )


async def wait_for_success(success_check, timeout_ms: int = 5000, poll_ms: int = 250) -> bool:
    if not success_check:
        return True

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if await success_check():
            return True
        await asyncio.sleep(poll_ms / 1000)
    return await success_check()


async def click_submit_resiliently(locator, description: str, timeout: int = 8000, success_check=None):
    try:
        await locator.click(timeout=timeout, no_wait_after=True)
        if await wait_for_success(success_check, timeout_ms=timeout):
            return
        logging.warning('%s click completed, but page did not advance before fallback timeout.', description)
    except Exception as exc:
        if success_check and await success_check():
            logging.info('%s click timed out, but page already advanced.', description)
            return
        logging.warning(f'{description} click did not finish cleanly: {exc}')

    try:
        await locator.evaluate('(el) => el.click()')
        if await wait_for_success(success_check, timeout_ms=timeout):
            logging.info('%s clicked via DOM fallback.', description)
            return
        logging.warning('%s DOM click fallback ran, but page still did not advance.', description)
    except Exception as exc:
        if success_check and await success_check():
            logging.info('%s DOM fallback skipped because page already advanced.', description)
            return
        logging.warning(f'{description} DOM click fallback failed: {exc}')


async def fill_otp_code(page, auth_input, otp_secret: str) -> str:
    await wait_for_fresh_totp_window()
    otp_code = generate_totp(otp_secret)
    await auth_input.fill('')
    await auth_input.focus()
    await auth_input.fill(otp_code)

    try:
        input_value = await auth_input.input_value(timeout=2000)
    except Exception:
        input_value = ''

    if input_value != otp_code:
        logging.warning('OTP fill verification failed (length=%s). Retrying with DOM assignment...', len(input_value))
        await auth_input.evaluate(
            """
            (el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """,
            otp_code,
        )
        input_value = await auth_input.input_value(timeout=2000)

    if input_value != otp_code:
        await capture_diagnostics(page, 'otp_fill_failed')
        raise RuntimeError(f'Failed to fill OTP input. Expected 6 digits, got length={len(input_value)}.')

    logging.info('OTP code filled successfully (6 digits).')
    return otp_code


async def submit_otp_form_fallback(page) -> bool:
    return await page.evaluate(
        """
        (submitSelector) => {
            const input = document.querySelector('input[name="auth_code"]');
            if (!input) return false;

            const submit = document.querySelector(submitSelector);
            const form = input.form || (submit && submit.form) || input.closest('form');
            if (!form) {
                if (submit) {
                    submit.click();
                    return true;
                }
                return false;
            }

            if (submit && typeof form.requestSubmit === 'function') {
                form.requestSubmit(submit);
            } else if (typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else {
                form.submit();
            }
            return true;
        }
        """,
        OTP_SUBMIT_DOM_SELECTOR,
    )


async def submit_otp_and_wait(page, submit_button, description: str) -> bool:
    await click_submit_resiliently(
        submit_button,
        description,
        timeout=7000,
        success_check=lambda: is_otp_or_dashboard_transition_complete(page),
    )

    if await is_otp_or_dashboard_transition_complete(page):
        return True

    logging.info('%s did not advance; submitting OTP form via requestSubmit fallback...', description)
    submitted = await submit_otp_form_fallback(page)
    if not submitted:
        logging.warning('OTP form fallback could not find a form or submit button.')

    return await wait_for_success(lambda: is_otp_or_dashboard_transition_complete(page), timeout_ms=7000)


async def is_effectively_enabled(locator) -> bool:
    try:
        disabled = await locator.is_disabled()
        if disabled:
            return False
    except Exception:
        pass

    try:
        aria_disabled = await locator.get_attribute('aria-disabled')
        if aria_disabled and aria_disabled.lower() == 'true':
            return False
    except Exception:
        pass

    try:
        button_class = await locator.get_attribute('class') or ''
        if 'disabled' in button_class.lower():
            return False
    except Exception:
        pass

    return True


async def wait_for_effectively_enabled(locator, timeout_ms: int = 20000, poll_ms: int = 500) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if await is_effectively_enabled(locator):
            return True
        await asyncio.sleep(poll_ms / 1000)
    return await is_effectively_enabled(locator)


async def log_final_button_state(button, attempt: int):
    try:
        disabled = await button.is_disabled(timeout=2000)
    except Exception as exc:
        disabled = f'unknown ({exc})'

    try:
        aria_disabled = await button.get_attribute('aria-disabled', timeout=2000)
    except Exception:
        aria_disabled = None

    try:
        button_class = await button.get_attribute('class', timeout=2000)
    except Exception:
        button_class = None

    logging.warning(
        'Final renewal button is still disabled on attempt %s: disabled=%s aria-disabled=%s class=%s',
        attempt,
        disabled,
        aria_disabled or '-',
        button_class or '-',
    )


async def close_free_user_campaign_modal(page):
    modal = page.locator('#campaignModalForFreeUsers')
    close_button = page.locator('#campaignModalForFreeUsers .modal__close')

    try:
        if not await modal.is_visible(timeout=2000):
            return
    except Exception:
        return

    logging.info('Closing free-user campaign modal...')

    try:
        await close_button.click(timeout=5000)
        await modal.wait_for(state='hidden', timeout=5000)
        logging.info('Free-user campaign modal closed via close button.')
        return
    except Exception as exc:
        logging.warning(f'Close button did not dismiss modal cleanly: {exc}')

    await page.evaluate(
        """
        () => {
            const modal = document.querySelector('#campaignModalForFreeUsers');
            if (!modal) return;
            modal.classList.remove('isOpen');
            modal.style.display = 'none';
            document.body.classList.remove('is-modal-open');
        }
        """
    )
    logging.info('Free-user campaign modal hidden via DOM fallback.')


async def main():
    load_local_env()

    email = os.getenv('EMAIL', '')
    password = os.getenv('PASSWORD', '')
    auth_login_otp = os.getenv('AUTH_LOGIN_OTP', '')
    proxy_server = os.getenv('PROXY_SERVER')
    debug_mode = os.getenv('DEBUG', 'false').lower() == 'true'
    current_jst = now_in_jst()
    today_jst = current_jst.date()
    local_state = load_local_state()

    if not should_attempt_login_from_state(local_state, current_jst):
        logging.info(
            'SKIP: Local state is outside the 12-hour renewal window. next_expiry_date=%s current_jst=%s',
            local_state.get('next_expiry_date', '-'),
            current_jst.isoformat(timespec='seconds'),
        )
        return

    options = {
        'headless': not debug_mode,
        'humanize': True,
        'geoip': True,
        'os': 'macos',
        'screen': Screen(max_width=1280, max_height=720),
        'window': (1280, 720),
        'locale': 'ja-JP',
        'disable_coop': True,
        'i_know_what_im_doing': True,
        'config': {'forceScopeAccess': True},
        'main_world_eval': True,
        'addons': [os.path.abspath(get_addon_path())]
    }
    
    if proxy_server:
        parsed = urlparse(proxy_server)
        proxy_config = {
            'server': f"{parsed.scheme}://{parsed.hostname}{f':{parsed.port}' if parsed.port else ''}"
        }
        if parsed.username:
            proxy_config['username'] = parsed.username
        if parsed.password:
            proxy_config['password'] = parsed.password
        options['proxy'] = proxy_config
    
    logging.info('Launching Camoufox in Python...')
    async with AsyncCamoufox(**options) as browser:
        context = await create_browser_context(browser)
        page = await context.new_page()
        #context = await browser.new_context()
        #page = await context.new_page()
        framework = FrameworkType.CAMOUFOX

        try:
            logging.info('Navigating to login...')
            # Use domcontentloaded to avoid getting stuck on tracking pixels
            await page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
            login_state = await wait_for_login_entry_state(page, timeout_ms=45000)
            
            if login_state == 'login_form':
                logging.info('Logging in...')
                await submit_primary_login(page, email, password)
            else:
                logging.info('Skipping primary login; current state is %s.', login_state)

            logging.info('Ensuring dashboard is fully reachable...')
            await ensure_dashboard_loaded(page, auth_login_otp, email, password)
            await save_browser_auth_state(context)
            await close_free_user_campaign_modal(page)

            logging.info('Navigating server details...')
            detail_link = page.locator(DASHBOARD_DETAIL_LINK_SELECTOR).first
            detail_href = await detail_link.get_attribute('href', timeout=5000)
            if not detail_href:
                raise RuntimeError('Could not read the server detail link URL.')
            await page.goto(
                urljoin(DASHBOARD_URL, detail_href),
                wait_until='domcontentloaded',
                timeout=60000,
            )
            
            logging.info('Waiting for server detail page...')
            await page.wait_for_selector('text="更新する"', timeout=30000)

            detail_url = page.url
            server_info_raw = await extract_server_info(page)
            server_info = normalize_server_info(server_info_raw)

            if not server_info['expiry_date_raw']:
                raise RuntimeError('Could not find 利用期限 on the server detail page.')

            current_jst = now_in_jst()
            today_jst = current_jst.date()
            expiry_date = parse_japanese_date(server_info['expiry_date_raw'])
            renewal_opens_at, expires_at = renewal_window_from_state(local_state, expiry_date)
            should_renew = is_in_renewal_window(expiry_date, current_jst, local_state)
            save_local_state(server_info, today_jst)

            logging.info(
                'Server detail: service_code=%s expiry=%s last_update=%s current_jst=%s renewal_opens_at=%s expires_at=%s should_renew=%s',
                server_info['service_code'] or '-',
                server_info['expiry_date_raw'],
                server_info['update_date_raw'] or '-',
                current_jst.isoformat(timespec='seconds'),
                renewal_opens_at.isoformat(timespec='seconds'),
                expires_at.isoformat(timespec='seconds'),
                should_renew,
            )

            if not should_renew:
                logging.info('SKIP: Current time is outside the final 12 hours of the expiry date.')
                notice_reason = 'skip_outside_renewal_window'
                if should_send_daily_notice(today_jst, notice_reason):
                    notice_results = await send_notice(
                        format_server_info_message(
                            '⚠️ XServer VPS 暂未续期：当前不在 12 小时续期窗口',
                            server_info,
                            today_jst,
                            should_renew=False,
                        )
                    )
                    if any(notice_results.values()):
                        mark_notice_sent(today_jst, notice_reason)
                    else:
                        logging.warning('No enabled notification channel delivered the skip notice.')
                else:
                    logging.info('SKIP: Daily notification already sent for %s.', notice_reason)
                await page.screenshot(path='skip_renewal.png', full_page=True)
                return

            await page.locator('text="更新する"').click()

            logging.info('Proceeding to renewal selection...')
            renewal_state = await open_free_renewal_confirmation(page)
            
            # If the suspension notice is visible, skip renewal gracefully
            if renewal_state == 'suspended':
                logging.info('SKIP: Renewal is not yet available (detected .newApp__suspended).')
                logging.info('XServer renewal is available only during the final 12 hours.')
                notice_reason = 'skip_not_yet_available'
                if should_send_daily_notice(today_jst, notice_reason):
                    notice_results = await send_notice(
                        format_server_info_message(
                            '⚠️ XServer VPS 暂未续期：续期入口尚未开放',
                            server_info,
                            today_jst,
                            should_renew=True,
                        )
                    )
                    if any(notice_results.values()):
                        mark_notice_sent(today_jst, notice_reason)
                    else:
                        logging.warning('No enabled notification channel delivered the skip notice.')
                else:
                    logging.info('SKIP: Daily notification already sent for %s.', notice_reason)
                await page.screenshot(path='skip_renewal.png', full_page=True)
                return

            button = page.locator(FINAL_RENEW_BUTTON_SELECTOR).first
            is_enabled = False

            for attempt in range(1, FINAL_RENEW_ATTEMPTS + 1):
                if attempt > 1:
                    logging.info(
                        'Refreshing renewal confirmation page before retrying captcha/Turnstile (attempt %s/%s)...',
                        attempt,
                        FINAL_RENEW_ATTEMPTS,
                    )
                    await page.reload(wait_until='domcontentloaded', timeout=60000)
                    renewal_state = await wait_for_renewal_page_or_status(page, timeout_ms=30000)
                    if renewal_state == 'selection':
                        renewal_state = await open_free_renewal_confirmation(page)

                    if renewal_state == 'suspended':
                        logging.info('SKIP: Renewal is not yet available after retry refresh (detected .newApp__suspended).')
                        await page.screenshot(path='skip_renewal.png', full_page=True)
                        return

                logging.info('Retrieving captcha (attempt %s/%s)...', attempt, FINAL_RENEW_ATTEMPTS)
                body = await page.eval_on_selector(CAPTCHA_IMAGE_SELECTOR, 'img => img.src')

                # Solve custom image captcha
                async with aiohttp.ClientSession() as session:
                    async with session.post('https://captcha-120546510085.asia-northeast1.run.app', data=body) as resp:
                        code = await resp.text()

                logging.info(f'Resolved captcha code: {code}')

                input_loc = page.locator('[placeholder="上の画像の数字を入力"]')
                await input_loc.fill('')
                await input_loc.focus()
                await input_loc.press_sequentially(code, delay=100)

                try:
                    # Use playwright-captcha library to handle the Turnstile challenge
                    async with ClickSolver(framework=framework, page=page) as solver:
                        await solver.solve_captcha(captcha_container=page, captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE)
                    logging.info('Turnstile interaction finished.')
                except Exception as e:
                    # Some solvers might throw errors even if the click was successful.
                    # We catch and log them as warnings to allow the script to proceed.
                    logging.warning(f'Turnstile solve loop exited: {e}')

                button = await wait_for_final_renew_button(page, timeout_ms=60000)
                await page.screenshot(path='before_click.png', full_page=True)

                logging.info('Waiting for final renewal button to become enabled (attempt %s/%s)...', attempt, FINAL_RENEW_ATTEMPTS)
                is_enabled = await wait_for_effectively_enabled(button, timeout_ms=60000, poll_ms=500)
                if is_enabled:
                    break

                await log_final_button_state(button, attempt)
                await page.screenshot(path=f'final_disabled_attempt_{attempt}.png', full_page=True)

            if not is_enabled:
                err_msg = f'Final button is DISABLED after {FINAL_RENEW_ATTEMPTS} attempts! Renewal failed or Turnstile verification was unsuccessful.'
                logging.error(err_msg)
                if not debug_mode:
                    raise Exception(err_msg)
                await send_notice(
                    format_server_info_message(
                        f'❌ XServer VPS 续期检查失败（DEBUG 模式）\n失败原因: {err_msg}',
                        server_info,
                        today_jst,
                        should_renew=True,
                    )
                )
            else:
                if debug_mode:
                    logging.info('DEBUG MODE: Final button is ENABLED. Skipping final click to preserve daily limit.')
                    await send_notice(
                        format_server_info_message(
                            '✅ XServer VPS 续期检查通过（DEBUG 模式，未提交）',
                            server_info,
                            today_jst,
                            should_renew=True,
                        )
                    )
                else:
                    logging.info('Executing final renewal submission...')
                    await button.click(timeout=30000, no_wait_after=True)
                    renewed_at_jst = now_in_jst()
                    await asyncio.sleep(3)
                    await page.screenshot(path='after_click.png', full_page=True)
                    logging.info('Captured post-click screenshot after 3 seconds.')

                    logging.info('Refreshing detail page to fetch latest renewal data...')
                    await page.goto(detail_url, wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_selector('table.table', timeout=30000)
                    latest_server_info = normalize_server_info(await extract_server_info(page))
                    if not latest_server_info['expiry_date_raw']:
                        raise RuntimeError('Renewal submission completed, but the refreshed expiry date is missing.')
                    latest_expiry_date = parse_japanese_date(latest_server_info['expiry_date_raw'])
                    if latest_expiry_date <= expiry_date:
                        raise RuntimeError(
                            'Renewal submission did not advance the expiry date: '
                            f'{expiry_date.isoformat()} -> {latest_expiry_date.isoformat()}'
                        )
                    save_local_state(latest_server_info, renewed_at_jst.date())

                    logging.info(
                        'Latest detail after renewal: service_code=%s expiry=%s last_update=%s',
                        latest_server_info['service_code'] or '-',
                        latest_server_info['expiry_date_raw'] or '-',
                        latest_server_info['update_date_raw'] or '-',
                    )
                    await send_notice(
                        format_server_info_message(
                            '✅ XServer VPS 续期成功',
                            latest_server_info,
                            renewed_at_jst.date(),
                            should_renew=True,
                        ),
                    )
                    logging.info('Final renewal submitted successfully!')
            
            logging.info('Done!')
            if debug_mode:
                logging.info('DEBUG MODE: Keeping browser open for 60 seconds for inspection...')
                await asyncio.sleep(60)
        
        except Exception as e:
            logging.error(f'Script Error: {e}')
            await capture_diagnostics(page, 'failure')
            try:
                today_jst = datetime.now(ZoneInfo('Asia/Tokyo')).date()
                await send_notice(
                    '❌ XServer VPS 续期失败\n'
                    f'失败原因: {summarize_error_for_notice(e)}\n'
                    f'日本日期: {today_jst.isoformat()}',
                )
            except Exception:
                pass
            sys.exit(1)
        finally:
            await asyncio.sleep(2)
            await context.close()

async def test_notice():
    load_local_env()
    await send_notice('✅ XServer VPS 通知测试成功')


if __name__ == '__main__':
    asyncio.run(main())
    # asyncio.run(test_notice())
