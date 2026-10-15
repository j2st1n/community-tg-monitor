"""Exercise the actual service entry point without credentials or networking."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BOOTSTRAP = r'''
import datetime as clock, importlib, io, json, os, pathlib, sys, threading, time, urllib.request
class EntryClock(clock.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 12, 8, 5, tzinfo=clock.timezone(clock.timedelta(hours=8)))
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
clock.datetime = EntryClock
root = pathlib.Path(os.environ['ENTRY_ROOT'])
package = os.environ['ENTRY_MODE'] == 'package'
sys.path.insert(0, str(root if package else root / 'app'))
fixture_text = pathlib.Path(os.environ['ENTRY_FIXTURE']).read_text()
if not package:
    fixture_text = fixture_text.replace('from app import browser_signin as browser',
                                        'import browser_signin as browser')
fixtures = {'__name__': 'entrypoint_fixtures'}
exec(compile(fixture_text, 'entrypoint_fixtures', 'exec'), fixtures)
factory, page, form, widget, cleanup = fixtures['BrowserFlowTest']().fixture()
runtime = importlib.import_module('app.browser_runtime' if package else 'browser_runtime')
runtime.desktop_playwright = factory
today = EntryClock.now(clock.timezone(clock.timedelta(hours=8))).strftime('%Y-%m-%d')
calls, notifications = [], []
class Response(io.BytesIO):
    status = 200
    headers = {}
def open_response(req, *args, **kwargs):
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    method = req.get_method() if isinstance(req, urllib.request.Request) else 'GET'
    calls.append((url, method))
    if url.startswith('https://api.telegram.org/'):
        if url.endswith('/sendMessage'):
            notifications.append(json.loads(req.data)['text'])
        return Response(b'{"ok":true}')
    assert method == 'GET'
    if url == 'https://sb.sb/checkin/':
        signed = bool(cleanup)
        button = '<button class="btn-post" disabled>今日已签到</button>' if signed else (
            '<form action="/checkin/" method="post"><input name="_csrf" value="synthetic">'
            '<button class="btn-post" type="submit">立即签到</button></form>')
        body = '<div class="signin-streak-num">48</div><span class="signin-stat-value">48</span>'
        body += '<span class="signin-stat-label">累计签到</span>' + button
        return Response(body.encode())
    if url == 'https://sb.sb/points/':
        return Response(('<time>' + today + ' 08:05:00</time><td>每日签到').encode())
    raise AssertionError('unexpected_network_target')
urllib.request.urlopen = open_response
urllib.request.OpenerDirector.open = lambda self, req, *a, **kw: open_response(req, *a, **kw)
class SchedulerStop(BaseException):
    pass
def sleep(seconds):
    if seconds == 300:
        raise SchedulerStop()
    if seconds == 60:
        state = json.loads((pathlib.Path(os.environ['DATA_DIR']) / 'checkin_state.json').read_text())
        assert state['version'] == 2 and state['last_date'] == today, state
        assert state['confirmation']['source'] == 'submitted_confirmed', state
        assert state['confirmation']['consecutive_days'] == '48 天', state
        assert state['notice']['status'] == 'accepted', state
        assert 'submission_guard' not in state, state
        assert factory.call_count == 1
        assert cleanup == ['browser_closed', 'driver_stopped']
        assert len(notifications) == 1 and '自动签到成功' in notifications[0]
        assert '连续 <b>48 天' in notifications[0]
        print(json.dumps({'entry_mode': os.environ['ENTRY_MODE'], 'completed': True,
                          'browser_calls': factory.call_count, 'network_mocked': True}))
        raise SystemExit(0)
time.sleep = sleep
class Thread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        if self.target.__name__ == 'sbsb_checkin_scheduler_thread':
            try:
                self.target(*self.args)
            except SchedulerStop:
                pass
threading.Thread = Thread
'''


class ServiceEntrypointTest(unittest.TestCase):
    def run_entrypoint(self, mode):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(project / 'app', root / 'app', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'bootstrap').mkdir()
            (root / 'bootstrap' / 'sitecustomize.py').write_text(BOOTSTRAP)
            (root / 'run').mkdir()
            env = {k: v for k, v in os.environ.items()
                   if k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'SYSTEMROOT')}
            env.update(PYTHONPATH=str(root / 'bootstrap'), ENTRY_ROOT=str(root), ENTRY_MODE=mode,
                       ENTRY_FIXTURE=str(project / 'tests/test_browser_signin.py'),
                       DATA_DIR=str(root / 'data'), SBSB_COOKIE='synthetic-session',
                       SBSB_BROWSER_SIGNIN='1', SBSB_BROWSER_MODE='desktop',
                       TG_BOT_TOKEN='synthetic-token', TG_CHAT_ID='1')
            command = ([sys.executable, '-B', str(root / 'app/main.py')] if mode == 'script'
                       else [sys.executable, '-B', '-m', 'app.main'])
            result = subprocess.run(command, env=env, cwd=root / 'run',
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout.splitlines()[-1])
            self.assertTrue(report['completed'])
            self.assertEqual(report['browser_calls'], 1)

    def test_file_entrypoint_reaches_desktop_flow_and_scheduler_confirmation(self):
        self.run_entrypoint('script')

    def test_package_entrypoint_keeps_desktop_flow_and_scheduler_confirmation(self):
        self.run_entrypoint('package')
