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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse
from browserforge.fingerprints import Screen
from camoufox.async_api import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

STATE_FILE = Path(__file__).resolve().parent / 'renewal_state.json'
DASHBOARD_URL = 'https://secure.xserver.ne.jp/xapanel/xvps/index'
LOGIN_URL = 'https://secure.xserver.ne.jp/xapanel/login/xvps/'
OTP_PATH = '/xapanel/myaccount/twostepauth/index'
OTP_DO_PATH = '/xapanel/myaccount/twostepauth/do'

ENV_KEYS = (
    'EMAIL',
    'PASSWORD',
    'AUTH_LOGIN_OTP',
    'PROXY_SERVER',
    'NOTICE_TG_TOKEN',
    'NOTICE_TG_USERID',
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


def parse_japanese_date(raw: str) -> date:
    normalized = raw.strip().replace('年', '-').replace('月', '-').replace('日', '')
    return datetime.strptime(normalized, '%Y-%m-%d').date()


def today_in_jst() -> date:
    return datetime.now(ZoneInfo('Asia/Tokyo')).date()


def load_local_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception as exc:
        logging.warning(f'Failed to read local renewal state: {exc}')
        return {}


def save_local_state(info: dict, today_jst: date):
    if not info.get('expiry_date_raw'):
        return

    previous = load_local_state()
    payload = {
        'next_expiry_date': parse_japanese_date(info['expiry_date_raw']).isoformat(),
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


def should_attempt_login_from_state(state: dict, today_jst: date) -> bool:
    next_expiry = state.get('next_expiry_date')
    if not next_expiry:
        return True

    try:
        expiry_date = datetime.strptime(next_expiry, '%Y-%m-%d').date()
    except ValueError:
        return True

    renewal_open_date = expiry_date - timedelta(days=1)
    return renewal_open_date <= today_jst <= expiry_date


def format_server_info_message(prefix: str, info: dict, today_jst: date, should_renew: bool | None = None) -> str:
    lines = [prefix]

    if info.get('server_name'):
        lines.append(f"server: {info['server_name']}")
    if info.get('service_code'):
        lines.append(f"service_code: {info['service_code']}")
    if info.get('uuid'):
        lines.append(f"uuid: {info['uuid']}")
    if info.get('expiry_date_raw'):
        lines.append(f"expiry: {info['expiry_date_raw']}")
    if info.get('update_date_raw'):
        lines.append(f"last_update: {info['update_date_raw']}")

    lines.append(f"today_jst: {today_jst.isoformat()}")
    if should_renew is not None:
        lines.append(f"should_renew: {should_renew}")

    return '\n'.join(lines)


async def send_tg_notice(token: str, user_id: str, message: str):
    if not token or not user_id:
        return

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = {
        'chat_id': user_id,
        'text': message,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logging.warning(f'Telegram notice failed: {resp.status} {body}')
    except Exception as exc:
        logging.warning(f'Telegram notice raised an exception: {exc}')


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


async def complete_optional_otp(page, otp_secret: str):
    for _ in range(60):
        current_url = page.url
        if '/xapanel/xvps/' in current_url:
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
        if OTP_DO_PATH in page.url:
            logging.info('OTP submit endpoint is still open. Trying dashboard URL directly...')
            await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
            return

        logging.info('Two-step authentication detected. Submitting TOTP code (attempt %s/%s)...', attempt, max_attempts)
        await page.wait_for_selector('input[name="auth_code"]', timeout=10000)

        auth_input = page.locator('input[name="auth_code"]')
        submit_button = page.locator('input[type="submit"][value="ログイン"]')
        error_message = page.locator('text="認証コードが一致しません"')
        otp_code = generate_totp(otp_secret)
        await auth_input.fill(otp_code)
        await click_submit_resiliently(submit_button, 'OTP login button', timeout=5000)

        retried_click = False
        for _ in range(30):
            current_url = page.url
            if '/xapanel/xvps/' in current_url:
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
                logging.info('OTP page is still open; retrying the login click once more...')
                await click_submit_resiliently(submit_button, 'OTP login button retry', timeout=5000)
                retried_click = True

            await asyncio.sleep(0.5)
        else:
            logging.warning('OTP page did not advance after submission attempt %s.', attempt)

    raise RuntimeError('Failed to complete two-step authentication after multiple attempts.')


async def submit_primary_login(page, email: str, password: str):
    await page.wait_for_selector('#memberid', timeout=10000)
    await page.locator('#memberid').fill(email)
    await page.locator('#user_password').fill(password)
    await click_submit_resiliently(page.locator('text="ログインする"'), 'Primary login button', timeout=5000)


async def ensure_dashboard_loaded(page, otp_secret: str, email: str, password: str, timeout_ms: int = 90000):
    dashboard_link = page.locator('a[href^="/xapanel/xvps/server/detail?id="]')
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

        if OTP_PATH in current_url:
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
                logging.warning(f'Login resubmit did not finish cleanly: {exc}')

            logging.info('Trying to open XVPS dashboard directly...')
            await page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
            last_forced_dashboard_visit = time.time()
            continue

        await asyncio.sleep(1)

    raise TimeoutError('Timed out waiting for the XServer dashboard to become available.')


async def click_submit_resiliently(locator, description: str, timeout: int = 8000):
    try:
        await locator.click(timeout=timeout, no_wait_after=True)
        return
    except Exception as exc:
        logging.warning(f'{description} click did not finish cleanly: {exc}')

    try:
        await locator.evaluate('(el) => el.click()')
        logging.info('%s clicked via DOM fallback.', description)
    except Exception as exc:
        logging.warning(f'{description} DOM click fallback failed: {exc}')


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
    notice_tg_token = os.getenv('NOTICE_TG_TOKEN', '')
    notice_tg_userid = os.getenv('NOTICE_TG_USERID', '')
    proxy_server = os.getenv('PROXY_SERVER')
    debug_mode = os.getenv('DEBUG', 'false').lower() == 'true'
    today_jst = today_in_jst()
    local_state = load_local_state()

    if not should_attempt_login_from_state(local_state, today_jst):
        logging.info(
            'SKIP: Local state says renewal window is not open yet. next_expiry_date=%s today_jst=%s',
            local_state.get('next_expiry_date', '-'),
            today_jst.isoformat(),
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
        context = await browser.new_context()
        page = await context.new_page()
        framework = FrameworkType.CAMOUFOX

        try:
            logging.info('Navigating to login...')
            # Use domcontentloaded to avoid getting stuck on tracking pixels
            await page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_selector('#memberid', timeout=30000)
            
            logging.info('Logging in...')
            await submit_primary_login(page, email, password)

            logging.info('Ensuring dashboard is fully reachable...')
            await ensure_dashboard_loaded(page, auth_login_otp, email, password)
            await close_free_user_campaign_modal(page)

            logging.info('Navigating server details...')
            await page.locator('a[href^="/xapanel/xvps/server/detail?id="]').first.click(no_wait_after=True)
            
            logging.info('Waiting for server detail page...')
            await page.wait_for_selector('text="更新する"', timeout=30000)

            detail_url = page.url
            server_info_raw = await extract_server_info(page)
            server_info = normalize_server_info(server_info_raw)

            if not server_info['expiry_date_raw']:
                raise RuntimeError('Could not find 利用期限 on the server detail page.')

            expiry_date = parse_japanese_date(server_info['expiry_date_raw'])
            renewal_open_date = expiry_date - timedelta(days=1)
            should_renew = renewal_open_date <= today_jst <= expiry_date
            save_local_state(server_info, today_jst)

            logging.info(
                'Server detail: service_code=%s expiry=%s last_update=%s today_jst=%s renewal_open_date=%s should_renew=%s',
                server_info['service_code'] or '-',
                server_info['expiry_date_raw'],
                server_info['update_date_raw'] or '-',
                today_jst.isoformat(),
                renewal_open_date.isoformat(),
                should_renew,
            )

            if not should_renew:
                logging.info('SKIP: Today is outside the renewal window (day before expiry through expiry day).')
                notice_reason = 'skip_outside_renewal_window'
                if should_send_daily_notice(today_jst, notice_reason):
                    await send_tg_notice(
                        notice_tg_token,
                        notice_tg_userid,
                        format_server_info_message('XServer VPS renewal skipped.', server_info, today_jst, should_renew=False),
                    )
                    mark_notice_sent(today_jst, notice_reason)
                else:
                    logging.info('SKIP: Daily Telegram notice already sent for %s.', notice_reason)
                await page.screenshot(path='skip_renewal.png', full_page=True)
                return

            await page.locator('text="更新する"').click()

            logging.info('Proceeding to renewal selection...')
            await page.locator('text="引き続き無料VPSの利用を継続する"').click(no_wait_after=True)
            
            logging.info('Waiting for renewal page or status...')
            
            # Wait for either the captcha image OR the suspension notice section
            # This will raise TimeoutError if neither appears within 30s (correct behavior)
            await page.wait_for_selector('img[src^="data:"], .newApp__suspended', timeout=30000)
            
            # If the suspension notice is visible, skip renewal gracefully
            if await page.locator('.newApp__suspended').is_visible():
                logging.info('SKIP: Renewal is not yet available (detected .newApp__suspended).')
                logging.info('XServer: "利用期限の1日前から更新手続きが可能です。"')
                notice_reason = 'skip_not_yet_available'
                if should_send_daily_notice(today_jst, notice_reason):
                    await send_tg_notice(
                        notice_tg_token,
                        notice_tg_userid,
                        format_server_info_message('XServer VPS renewal skipped: not yet available.', server_info, today_jst, should_renew=True),
                    )
                    mark_notice_sent(today_jst, notice_reason)
                else:
                    logging.info('SKIP: Daily Telegram notice already sent for %s.', notice_reason)
                await page.screenshot(path='skip_renewal.png', full_page=True)
                return

            logging.info('Retrieving captcha...')
            body = await page.eval_on_selector('img[src^="data:"]', 'img => img.src')
            
            # Solve custom image captcha
            async with aiohttp.ClientSession() as session:
                async with session.post('https://captcha-120546510085.asia-northeast1.run.app', data=body) as resp:
                    code = await resp.text()
            
            logging.info(f'Resolved captcha code: {code}')
            
            input_loc = page.locator('[placeholder="上の画像の数字を入力"]')
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

            await page.wait_for_selector('text="無料VPSの利用を継続する"', timeout=60000)
            await page.screenshot(path='before_click.png', full_page=True)
            
            button = page.locator('text="無料VPSの利用を継続する"')
            logging.info('Waiting for final renewal button to become enabled...')
            is_enabled = await wait_for_effectively_enabled(button, timeout_ms=60000, poll_ms=500)

            if not is_enabled:
                err_msg = 'Final button is DISABLED! Renewal failed or Turnstile verification was unsuccessful.'
                logging.error(err_msg)
                if not debug_mode:
                    raise Exception(err_msg)
            else:
                if debug_mode:
                    logging.info('DEBUG MODE: Final button is ENABLED. Skipping final click to preserve daily limit.')
                    await send_tg_notice(
                        notice_tg_token,
                        notice_tg_userid,
                        format_server_info_message('XServer VPS renewal ready in debug mode.', server_info, today_jst, should_renew=True),
                    )
                else:
                    logging.info('Executing final renewal submission...')
                    await button.click(timeout=30000, no_wait_after=True)
                    await asyncio.sleep(3)
                    await page.screenshot(path='after_click.png', full_page=True)
                    logging.info('Captured post-click screenshot after 3 seconds.')

                    logging.info('Refreshing detail page to fetch latest renewal data...')
                    await page.goto(detail_url, wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_selector('table.table', timeout=30000)
                    latest_server_info = normalize_server_info(await extract_server_info(page))
                    save_local_state(latest_server_info, today_jst)

                    logging.info(
                        'Latest detail after renewal: service_code=%s expiry=%s last_update=%s',
                        latest_server_info['service_code'] or '-',
                        latest_server_info['expiry_date_raw'] or '-',
                        latest_server_info['update_date_raw'] or '-',
                    )
                    await send_tg_notice(
                        notice_tg_token,
                        notice_tg_userid,
                        format_server_info_message(
                            'XServer VPS renewal submitted successfully.',
                            latest_server_info,
                            today_jst,
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
            try:
                today_jst = datetime.now(ZoneInfo('Asia/Tokyo')).date()
                await send_tg_notice(
                    notice_tg_token,
                    notice_tg_userid,
                    f'XServer VPS renewal failed.\nerror: {e}\ntoday_jst: {today_jst.isoformat()}',
                )
            except Exception:
                pass
            sys.exit(1)
        finally:
            await asyncio.sleep(2)
            await context.close()

async def test_tg():
    email = os.getenv('EMAIL', '')
    password = os.getenv('PASSWORD', '')
    auth_login_otp = os.getenv('AUTH_LOGIN_OTP', '')
    notice_tg_token = os.getenv('NOTICE_TG_TOKEN', '')
    notice_tg_userid = os.getenv('NOTICE_TG_USERID', '')
    proxy_server = os.getenv('PROXY_SERVER')
    debug_mode = os.getenv('DEBUG', 'false').lower() == 'true'
    await send_tg_notice(
                    notice_tg_token,
                    notice_tg_userid,
                    'XServer VPS tg test',
                )
if __name__ == '__main__':
    asyncio.run(main())
    # asyncio.run(test_tg())
