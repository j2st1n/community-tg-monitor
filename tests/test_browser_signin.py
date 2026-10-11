import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from app import browser_signin as browser


class BrowserRulesTest(unittest.TestCase):
    def test_client_error_codes_are_whitelisted_without_event_messages(self):
        for code in ('instr_blocked', 'instr_timeout', 'network_error', 'challenge_parse_error'):
            self.assertEqual('cap_client_' + code, browser.cap_client_error(code))
        for value in ('private-token', 'https://example.invalid/?token=private', None, {'code': 'instr_blocked'}):
            self.assertEqual('cap_client_error', browser.cap_client_error(value))

    def test_post_redirects_cannot_preserve_body_or_leave_checkin(self):
        for status in (301, 307, 308):
            self.assertFalse(browser.post_response_allowed(browser.CHECKIN_URL, status, '/checkin/'))
        for location in ('https://evil.invalid/checkin/', '/login/', '/checkin/?token=private'):
            self.assertFalse(browser.post_response_allowed(browser.CHECKIN_URL, 303, location))
        self.assertTrue(browser.post_response_allowed(browser.CHECKIN_URL, 303, '/checkin/'))
        self.assertFalse(browser.post_response_allowed('https://sb.sb/cap/redeem', 302, '/checkin/'))

    def test_endpoint_is_same_origin_and_rejects_root_or_query(self):
        self.assertEqual({'https://sb.sb/cap/checkin/challenge', 'https://sb.sb/cap/checkin/redeem'},
                         browser.cap_post_urls('/cap/checkin/'))
        for endpoint in ('https://evil.invalid/cap/', '//evil.invalid/cap/', '/', '/cap/?token=private'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                browser.cap_post_urls(endpoint)

    def test_hosted_cap_endpoint_is_pinned_and_exact(self):
        base = browser.CAP_ORIGIN + '/synthetic-site/'
        posts = browser.cap_post_urls(base)
        self.assertEqual({base + 'challenge', base + 'redeem'}, posts)
        for method in ('POST', 'OPTIONS'):
            self.assertTrue(browser.request_allowed(base + 'redeem', method, posts, True))
            self.assertFalse(browser.request_allowed(base + 'redeem', method, posts, False))
        for url in (base + 'other', 'https://capjs.net.evil.invalid/synthetic-site/redeem',
                    'https://capjs.net/other-site/redeem'):
            self.assertFalse(browser.request_allowed(url, 'POST', posts, True))
        for endpoint in ('https://capjs.net/', 'https://capjs.net:443/site/',
                         'https://user:password@capjs.net/site/',
                         'https://capjs.net/site/?private=value'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                browser.cap_post_urls(endpoint)

    def test_request_guard_never_submits_in_query_mode_or_forwards_to_other_origins(self):
        posts = {browser.CHECKIN_URL}
        self.assertFalse(browser.request_allowed(browser.CHECKIN_URL, 'POST', posts, False))
        self.assertTrue(browser.request_allowed(browser.CHECKIN_URL, 'POST', posts, True))
        for target in ('https://evil.invalid/checkin/', 'https://sb.sb.evil.invalid/checkin/',
                       'https://sb.sb/login/', 'http://sb.sb/checkin/', 'https://user@sb.sb/checkin/'):
            self.assertFalse(browser.request_allowed(target, 'POST', posts, True))
        self.assertTrue(browser.request_allowed(
            'https://cdn.jsdelivr.net/npm/@cap.js/wasm@0.0.7/browser/cap_wasm_bg.wasm', 'GET', posts, False))
        self.assertFalse(browser.request_allowed('https://cdn.jsdelivr.net/collect', 'POST', posts, True))

    def test_cookie_is_bound_to_target_host_and_raw_session_is_normalized(self):
        records = browser.cookie_records('example-session')
        self.assertEqual('__Host-bbs_session', records[0]['name'])
        self.assertEqual('https://sb.sb/', records[0]['url'])
        self.assertTrue(records[0]['secure'])


class BrowserFlowTest(unittest.TestCase):
    def fixture(self, *, signed=False, cap=False, outcome='confirmed'):
        factory = MagicMock()
        runtime = factory.return_value.__enter__.return_value
        instance = runtime.chromium.launch.return_value
        context = instance.new_context.return_value
        page = context.new_page.return_value
        page.url = browser.CHECKIN_URL
        page.goto.return_value.status = 200
        hooks, guards, state, cleanup = {}, [], {'signed': signed}, []
        page.on.side_effect = lambda name, fn: hooks.update({name: fn})
        context.route.side_effect = lambda pattern, fn: guards.append(fn)
        page.evaluate.side_effect = lambda js: state['signed'] if js == browser._SIGNED_JS else False
        form = page.locator.return_value
        form.count.return_value = 1
        form.evaluate.return_value = {'action': browser.CHECKIN_URL, 'method': 'post',
                                     'csrf': True, 'cap': cap, 'endpoint': '/cap/checkin/'}
        button, widget = Mock(), Mock()
        form.locator.side_effect = lambda selector: widget if selector == 'cap-widget' else button

        def complete(*args):
            if outcome == 'cap_rejected':
                response = SimpleNamespace(url='https://sb.sb/cap/checkin/redeem', status=200,
                    request=SimpleNamespace(method='POST'),
                    json=lambda: {'success': False, 'error': 'automated_browser_detected', 'token': 'private'})
                hooks['response'](response)
                return
            route = Mock()
            route.request = SimpleNamespace(url=browser.CHECKIN_URL, method='POST')
            response = SimpleNamespace(status=200, headers={}, dispose=Mock())
            route.fetch.return_value = response
            guards[0](route)
            route.fetch.assert_called_once_with(max_redirects=0, max_retries=0, timeout=15000)
            route.fulfill.assert_called_once_with(response=response)
            route.continue_.assert_not_called()
            if outcome == 'cap_body_disposed':
                def disposed():
                    raise RuntimeError('synthetic disposed body')
                hooks['response'](SimpleNamespace(url='https://sb.sb/cap/checkin/redeem',status=200,
                    request=SimpleNamespace(method='POST'),json=disposed))
            hooks['response'](SimpleNamespace(url=browser.CHECKIN_URL,
                                             status=403 if outcome=='business_rejected' else 200,
                                             request=SimpleNamespace(method='POST')))
            hooks['framenavigated'](page.main_frame)
            state['signed'] = outcome in ('confirmed','cap_body_disposed','business_rejected')
        button.click.side_effect = None if cap else complete
        widget.evaluate.side_effect = complete
        instance.close.side_effect = lambda: cleanup.append('browser_closed')
        factory.return_value.__exit__.side_effect = lambda *args: cleanup.append('driver_stopped')
        return factory, page, form, widget, cleanup

    def test_already_signed_or_query_only_never_clicks_or_solves(self):
        for signed in (True, False):
            factory, page, form, widget, cleanup = self.fixture(signed=signed)
            result = browser.run_checkin('session', allow_submit=False, playwright_factory=factory)
            self.assertEqual(signed, result['success'])
            form.locator.assert_not_called()
            widget.evaluate.assert_not_called()
            self.assertEqual(['browser_closed', 'driver_stopped'], cleanup)

    def test_login_redirect_never_submits(self):
        factory, page, form, widget, _ = self.fixture()
        page.url = 'https://sb.sb/login/'
        result = browser.run_checkin('session', playwright_factory=factory)
        self.assertEqual('login_or_redirect', result['browser_stage'])
        form.locator.assert_not_called()

    def test_original_cap_event_flow_requires_confirmed_page(self):
        factory, _, _, widget, cleanup = self.fixture(cap=True)
        result = browser.run_checkin('session', cap_endpoint='/cap/checkin/', playwright_factory=factory)
        self.assertTrue(result['success'])
        self.assertFalse(result['already'])
        self.assertEqual('submitted_confirmed', result['browser_stage'])
        widget.evaluate.assert_called_once()
        self.assertEqual(['browser_closed', 'driver_stopped'], cleanup)

    def test_server_automation_rejection_is_returned_without_raw_token(self):
        factory, _, _, _, _ = self.fixture(cap=True, outcome='cap_rejected')
        result = browser.run_checkin('session', cap_endpoint='/cap/checkin/', playwright_factory=factory)
        self.assertFalse(result['success'])
        self.assertTrue(result['needs_human'])
        self.assertEqual('automated_browser_detected', result['browser_stage'])
        self.assertIn('站方验证明确拒绝自动化浏览器', result['msg'])
        self.assertNotIn('private', str(result))

    def test_original_client_error_event_keeps_only_known_code(self):
        for code, expected in (('instr_blocked', 'cap_client_instr_blocked'),
                               ('private-token', 'cap_client_error')):
            with self.subTest(code=code):
                factory, page, _, widget, _ = self.fixture(cap=True)
                widget.evaluate.side_effect = None
                page.evaluate.side_effect = lambda js: (
                    False if js == browser._SIGNED_JS else
                    code if js == '() => window.__monitorCapErrorCode' else True)
                result = browser.run_checkin('session', cap_endpoint='/cap/checkin/',
                                             playwright_factory=factory)
                self.assertFalse(result['success'])
                self.assertEqual(expected, result['browser_stage'])
                self.assertNotIn('private-token', str(result))

    def test_post_response_without_signed_page_is_not_success(self):
        factory, _, _, _, _ = self.fixture(outcome='unconfirmed')
        result = browser.run_checkin('session', playwright_factory=factory)
        self.assertFalse(result['success'])
        self.assertEqual('submitted_unconfirmed', result['browser_stage'])

    def test_late_cap_body_disposal_uses_confirmed_business_response(self):
        factory, _, _, _, _ = self.fixture(cap=True,outcome='cap_body_disposed')
        result = browser.run_checkin('session',cap_endpoint='/cap/checkin/',playwright_factory=factory)
        self.assertEqual('submitted_confirmed',result['browser_stage'])

    def test_rejected_business_post_is_not_success_even_if_page_is_signed(self):
        factory, _, _, _, _ = self.fixture(outcome='business_rejected')
        result = browser.run_checkin('session',playwright_factory=factory)
        self.assertFalse(result['success'])
        self.assertEqual('checkin_http_403',result['browser_stage'])

    def test_navigation_context_replaced_after_post_waits_for_signed_page(self):
        factory, page, _, _, _ = self.fixture()
        original = page.evaluate.side_effect
        signed_reads = 0

        def evaluate(js):
            nonlocal signed_reads
            if js == browser._SIGNED_JS:
                signed_reads += 1
                if signed_reads == 2:
                    raise RuntimeError('Execution context was destroyed; synthetic-private-token')
            return original(js)

        page.evaluate.side_effect = evaluate
        result = browser.run_checkin('session', playwright_factory=factory)
        self.assertEqual('submitted_confirmed', result['browser_stage'])
        self.assertNotIn('synthetic-private-token', str(result))

    def test_unrelated_error_after_post_is_not_retried_or_called_success(self):
        factory, page, _, _, _ = self.fixture()
        original = page.evaluate.side_effect
        signed_reads = 0

        def evaluate(js):
            nonlocal signed_reads
            if js == browser._SIGNED_JS:
                signed_reads += 1
                if signed_reads == 2:
                    raise RuntimeError('unrelated synthetic-private-token')
            return original(js)

        page.evaluate.side_effect = evaluate
        result = browser.run_checkin('session', playwright_factory=factory)
        self.assertFalse(result['success'])
        self.assertEqual('browser_confirmation_RuntimeError', result['browser_stage'])
        self.assertNotIn('synthetic-private-token', str(result))

    def test_errors_are_redacted_and_lock_is_released(self):
        factory, _, _, _, _ = self.fixture()
        factory.return_value.__enter__.return_value.chromium.launch.side_effect = RuntimeError('secret csrf token')
        for _ in range(2):
            result = browser.run_checkin('session', playwright_factory=factory)
            self.assertEqual('browser_launch_RuntimeError', result['browser_stage'])
            self.assertNotIn('secret', str(result))

    def test_invalid_endpoint_is_specific_and_never_launches_browser(self):
        factory = Mock()
        result = browser.run_checkin('session', cap_endpoint='https://evil.invalid/site/',
                                    playwright_factory=factory)
        self.assertEqual('invalid_cap_endpoint', result['browser_stage'])
        factory.assert_not_called()

    def test_desktop_context_uses_native_viewport_and_invalid_mode_never_launches(self):
        factory, _, _, _, _ = self.fixture(signed=True)
        result = browser.run_checkin('session', browser_mode='desktop', playwright_factory=factory)
        self.assertTrue(result['success'])
        chromium = factory.return_value.__enter__.return_value.chromium
        chromium.launch.assert_called_once_with(headless=False, timeout=20000)
        chromium.launch.return_value.new_context.assert_called_once_with(
            service_workers='block', no_viewport=True)
        factory = Mock()
        result = browser.run_checkin('session', browser_mode='unknown', playwright_factory=factory)
        self.assertEqual('invalid_browser_mode', result['browser_stage'])
        factory.assert_not_called()

    def test_hosted_cap_requests_strip_credentials_and_allow_exact_preflight(self):
        factory, _, _, _, _ = self.fixture(signed=True)
        base = browser.CAP_ORIGIN + '/synthetic-site/'
        result = browser.run_checkin('session', cap_endpoint=base, playwright_factory=factory)
        self.assertTrue(result['success'])
        context = factory.return_value.__enter__.return_value.chromium.launch.return_value.new_context.return_value
        guard = context.route.call_args.args[1]
        for method in ('POST', 'OPTIONS'):
            route = Mock()
            route.request = SimpleNamespace(url=base+'challenge', method=method,
                all_headers=lambda: {'Cookie':'private-session','Authorization':'Bearer private',
                                     'origin':'https://sb.sb','content-type':'application/json'})
            route.fetch.return_value = SimpleNamespace(status=200,headers={},dispose=Mock())
            guard(route)
            kwargs = route.fetch.call_args.kwargs if method=='POST' else route.continue_.call_args.kwargs
            self.assertEqual('', kwargs['headers']['cookie'])
            self.assertNotIn('authorization', {k.lower() for k in kwargs['headers']})
            self.assertNotIn('private', str(kwargs))
            self.assertEqual('https://sb.sb',kwargs['headers']['origin'])


if __name__ == '__main__':
    unittest.main()
