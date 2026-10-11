import io
import os
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import main


UNSIGNED = """
<form action='/search/'><input name='_csrf' value='search-token'></form>
<div class='large signin-streak-num'>43</div>
<span class='signin-stat-value extra'>43</span>
<span class='signin-stat-label'>最长连续</span>
<span class='signin-stat-value'>43</span>
<span class='signin-stat-label'>累计签到</span>
<form class='signin-form' method='post' action='/checkin/'>
<input type='hidden' value='checkin-token' name='_csrf'>
<button type='submit' class='btn-post'>立即签到</button>
<cap-widget></cap-widget>
</form>
"""
SIGNED = UNSIGNED.replace('43', '44').replace(
    "type='submit' class='btn-post'>立即签到",
    "type='button' class='btn-post' disabled>今日已签到",
)
POINTS = """
<span>可用烧饼</span><span class="value">1751</span>
<span>成长值</span><span class="value">301</span>
<span>等级</span><span class="value">Lv.6 经常露面</span>
<time datetime="2026-10-07T00:00:04Z">2026-10-07 08:00</time><td>每日签到
"""


class Response(io.BytesIO):
    def __init__(self, body='', status=200, location=None):
        super().__init__(body.encode())
        self.status = self.code = status
        self.headers = {'Location': location} if location else {}


class SigninFlowTest(unittest.TestCase):
    def setUp(self):
        # HTTP cases stay deterministic even inside the browser-enabled image.
        environment = patch.dict(os.environ, {'SBSB_BROWSER_SIGNIN': '0'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_cap_endpoint_comes_from_checkin_form_not_other_widgets(self):
        expected = 'https://capjs.net/synthetic-site/'
        html = UNSIGNED.replace('<cap-widget>', f'<cap-widget data-cap-api-endpoint="{expected}">')
        html += '<form action="/other/"><cap-widget data-cap-api-endpoint="https://evil.invalid/"></cap-widget></form>'
        page = main.SbsbSigninPageParser(html)
        self.assertEqual(expected, page.cap_endpoint)
        with patch.dict(os.environ, {'SBSB_BROWSER_SIGNIN': '1'}), patch.object(
            main,'run_sbsb_browser_checkin',return_value={'success':False,'needs_human':True}) as runner:
            self.run_flow([Response(html)])
        self.assertEqual(expected,runner.call_args.args[1])

    def test_browser_failure_does_not_fall_back_to_unverified_http_post(self):
        with patch.dict(os.environ, {'SBSB_BROWSER_SIGNIN': '1'}), patch.object(
            main, 'run_sbsb_browser_checkin', return_value={'success': False, 'needs_human': True,
            'browser_stage': 'cap_rejected', 'msg': 'rejected'}) as runner:
            result, opener = self.run_flow([Response(UNSIGNED)])
        self.assertFalse(result['success'])
        self.assertEqual(1, opener.open.call_count)
        runner.assert_called_once()

    def test_browser_success_is_rechecked_with_official_counters(self):
        with patch.dict(os.environ, {'SBSB_BROWSER_SIGNIN': '1'}), patch.object(
            main, 'run_sbsb_browser_checkin', return_value={'success': True, 'already': False,
            'browser_stage': 'submitted_confirmed'}):
            result, opener = self.run_flow([Response(UNSIGNED), Response(SIGNED), Response(POINTS)])
        self.assertTrue(result['success'])
        self.assertFalse(result['already'])
        self.assertEqual('44 天', result['consecutive_days'])
        self.assertTrue(all(c.args[0].get_method() == 'GET' for c in opener.open.call_args_list))

    def test_query_only_never_starts_browser_even_when_enabled(self):
        with patch.dict(os.environ, {'SBSB_BROWSER_SIGNIN': '1'}), patch.object(
            main, 'run_sbsb_browser_checkin') as runner:
            opener = Mock()
            opener.open.return_value = Response(UNSIGNED)
            with patch.object(main.urllib.request, 'build_opener', return_value=opener):
                result = main.do_sbsb_signin('session', allow_submit=False)
        self.assertFalse(result['success'])
        runner.assert_not_called()

    def run_flow(self, responses):
        opener = Mock()
        opener.open.side_effect = responses
        with patch.object(main.urllib.request, 'build_opener', return_value=opener):
            result = main.do_sbsb_signin('test-session')
        return result, opener

    def test_already_signed_reads_official_counters_and_does_not_post(self):
        result, opener = self.run_flow([Response(SIGNED), Response(POINTS)])
        self.assertTrue(result['success'])
        self.assertTrue(result['already'])
        self.assertEqual('44 天', result['consecutive_days'])
        self.assertEqual('44 天', result['total_days'])
        self.assertEqual('1751 饼', result['total_points'])
        self.assertTrue(all(call.args[0].get_method() == 'GET' for call in opener.open.call_args_list))
        self.assertEqual(main.SBSB_CHECKIN_URL, opener.open.call_args_list[0].args[0].full_url)

    def test_post_uses_only_official_form_csrf_and_requires_refreshed_confirmation(self):
        result, opener = self.run_flow([
            Response(UNSIGNED), Response(status=303, location='/checkin/'),
            Response(SIGNED), Response(POINTS),
        ])
        self.assertTrue(result['success'])
        self.assertFalse(result['already'])
        post = opener.open.call_args_list[1].args[0]
        self.assertEqual(main.SBSB_CHECKIN_URL, post.full_url)
        self.assertEqual(b'_csrf=checkin-token&message=', post.data)
        self.assertNotIn(b'search-token', post.data)
        self.assertEqual('44 天', result['consecutive_days'])

    def test_http_200_without_confirmed_signin_is_failure(self):
        result, opener = self.run_flow([Response(UNSIGNED), Response(), Response(UNSIGNED)])
        self.assertFalse(result['success'])
        self.assertIn('人机验证', result['msg'])
        self.assertEqual(3, opener.open.call_count)

    def test_query_only_after_captcha_does_not_post_again(self):
        opener = Mock()
        opener.open.return_value = Response(UNSIGNED)
        with patch.object(main.urllib.request, 'build_opener', return_value=opener):
            result = main.do_sbsb_signin('session', allow_submit=False)
        self.assertFalse(result['success'])
        self.assertTrue(result['needs_human'])
        self.assertEqual(1, opener.open.call_count)

    def test_query_only_can_confirm_manual_signin(self):
        opener = Mock()
        opener.open.side_effect = [Response(SIGNED), Response(POINTS)]
        with patch.object(main.urllib.request, 'build_opener', return_value=opener):
            result = main.do_sbsb_signin('session', allow_submit=False)
        self.assertTrue(result['success'])
        self.assertTrue(result['already'])

    def test_missing_form_csrf_does_not_use_other_form_or_report_success(self):
        page = UNSIGNED.replace("value='checkin-token' name='_csrf'", "value='unused' name='message'")
        result, opener = self.run_flow([Response(page)])
        self.assertFalse(result['success'])
        self.assertEqual(1, opener.open.call_count)

    def test_unknown_or_foreign_action_is_never_posted(self):
        for action in ('/signin/', 'https://example.invalid/checkin/'):
            with self.subTest(action=action):
                result, opener = self.run_flow([Response(UNSIGNED.replace("action='/checkin/'", f"action='{action}'"))])
                self.assertFalse(result['success'])
                self.assertEqual(1, opener.open.call_count)

    def test_301_post_is_failure_instead_of_followed_get_success(self):
        result, opener = self.run_flow([
            Response(UNSIGNED), Response(status=301, location='/checkin/'),
        ])
        self.assertFalse(result['success'])
        self.assertEqual(2, opener.open.call_count)

    def test_no_redirect_handler_disables_post_to_get_conversion(self):
        with patch.object(main.urllib.request, 'build_opener') as factory:
            factory.return_value.open.side_effect = OSError('offline')
            self.assertFalse(main.do_sbsb_signin('session')['success'])
        handler = factory.call_args.args[0]()
        req = main.urllib.request.Request(main.SBSB_CHECKIN_URL, data=b'_csrf=test')
        self.assertIsNone(handler.redirect_request(req, None, 301, 'Moved', {}, main.SBSB_CHECKIN_URL))

    def test_captcha_http_error_reports_failure(self):
        error = urllib.error.HTTPError(main.SBSB_CHECKIN_URL, 403, 'Forbidden', {}, io.BytesIO(b'captcha'))
        result, _ = self.run_flow([Response(UNSIGNED), error])
        self.assertFalse(result['success'])
        self.assertIn('HTTP 403', result['msg'])
        self.assertIn('人机验证', result['msg'])

    def test_login_or_foreign_redirect_does_not_forward_cookie(self):
        for location in ('/login/', 'https://example.invalid/checkin/'):
            with self.subTest(location=location):
                result, opener = self.run_flow([Response(status=302, location=location)])
                self.assertFalse(result['success'])
                self.assertEqual(1, opener.open.call_count)

    def test_unavailable_or_zero_counter_is_explicitly_unknown(self):
        for page in (SIGNED.replace('44', '0'), SIGNED.replace('44', 'unavailable')):
            result, _ = self.run_flow([Response(page), Response(POINTS)])
            self.assertTrue(result['success'])
            self.assertEqual('暂未获取', result['consecutive_days'])
            self.assertEqual('暂未获取', result['total_days'])

    def test_points_failure_does_not_fabricate_assets_or_time(self):
        result, _ = self.run_flow([Response(SIGNED), OSError('offline')])
        self.assertTrue(result['success'])
        self.assertEqual('暂未获取', result['total_points'])
        self.assertEqual('暂未获取', result['signin_time'])

    def test_old_ledger_time_is_not_reported_as_todays_signin(self):
        with patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 8, 9, tzinfo=timezone(timedelta(hours=8)))
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result, _ = self.run_flow([Response(SIGNED), Response(POINTS)])
        self.assertEqual('暂未获取', result['signin_time'])

    def test_todays_ledger_time_is_converted_to_beijing_time(self):
        with patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 7, 9, tzinfo=timezone(timedelta(hours=8)))
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result, _ = self.run_flow([Response(SIGNED), Response(POINTS)])
        self.assertEqual('2026-10-07 08:00:04', result['signin_time'])


class SigninSchedulerTest(unittest.TestCase):
    def test_due_at_0805_including_first_start_but_not_0800(self):
        for last_date in ('', '2026-10-07'):
            self.assertFalse(main.sbsb_checkin_due(datetime(2026, 10, 8, 8, 4), last_date))
            self.assertTrue(main.sbsb_checkin_due(datetime(2026, 10, 8, 8, 5), last_date))
        self.assertFalse(main.sbsb_checkin_due(datetime(2026, 10, 8, 9), '2026-10-08'))

    def run_scheduler(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'checkin_state.json'
            bot = SimpleNamespace(sbsb_cookie='session', admin_chat_id='test', send_msg=Mock())
            with (patch.object(main, 'DATA_DIR', tmp),
                  patch.object(main, 'CHECKIN_STATE_FILE', str(state_path)),
                  patch.object(main, 'datetime') as clock,
                  patch.object(main, 'do_sbsb_signin', return_value=result) as signin,
                  patch.object(main.time, 'sleep', side_effect=[None, StopIteration])):
                clock.now.return_value = datetime(2026, 10, 8, 9, tzinfo=timezone(timedelta(hours=8)))
                with self.assertRaises(StopIteration):
                    main.sbsb_checkin_scheduler_thread(bot)
            state = json.loads(state_path.read_text())
        return state, bot, signin

    def test_failed_signin_is_not_marked_done_and_warning_is_once_per_day(self):
        state, bot, signin = self.run_scheduler({'success': False, 'msg': '<captcha>'})
        self.assertNotEqual('2026-10-08', state.get('last_date'))
        self.assertEqual('2026-10-08', state['last_error_date'])
        self.assertEqual(2, signin.call_count)
        self.assertEqual(1, bot.send_msg.call_count)
        self.assertIn('&lt;captcha&gt;', bot.send_msg.call_args.args[1])

    def test_successful_signin_is_marked_done_and_not_repeated(self):
        state, bot, signin = self.run_scheduler({'success': True, 'already': False})
        self.assertEqual('2026-10-08', state['last_date'])
        self.assertEqual(1, signin.call_count)
        self.assertEqual(1, bot.send_msg.call_count)

    def test_query_confirmation_report_does_not_claim_automatic_submission(self):
        state, bot, _ = self.run_scheduler({'success': True, 'already': True})
        self.assertIn('查询确认', bot.send_msg.call_args.args[1])
        self.assertNotIn('自动完成', bot.send_msg.call_args.args[1])

    def test_captcha_failure_blocks_subsequent_posts_but_keeps_querying(self):
        state, bot, signin = self.run_scheduler({'success': False, 'needs_human': True, 'msg': 'captcha'})
        self.assertEqual('2026-10-08', state['manual_required_date'])
        self.assertTrue(signin.call_args_list[0].kwargs['allow_submit'])
        self.assertFalse(signin.call_args_list[1].kwargs['allow_submit'])
        self.assertEqual(1, bot.send_msg.call_count)


if __name__ == '__main__':
    unittest.main()
