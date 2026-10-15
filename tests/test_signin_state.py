import io
import json
import os
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app import main
from app.signin_feedback import format_signin
from app.signin_state import (CST, UNKNOWN, PointsParser, SigninStore, StateError,
                              period_key, record_result, summary)
from test_signin import SIGNED, UNSIGNED, POINTS, Response
from test_telegram_ui import BotCase


def success(key="2026-10-15", submitted=False, **fields):
    return dict(success=True, already=not submitted, period=key,
                outcome="submitted_confirmed" if submitted else "query_confirmed",
                consecutive_days="51 天", total_days="51 天", total_points="572 饼",
                exp="555", level="Lv.8 热心新人", signin_time=f"{key} 08:05:31",
                submission_attempted=submitted, submission_accepted=submitted, **fields)


class SigninEvidenceTest(BotCase):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch.object(main, "datetime"))
        self.clock.now.side_effect = lambda *args: self.now

    def flow(self, responses, *, allow_submit=False, browser=None):
        opener = Mock()
        opener.open.side_effect = responses
        with patch.object(main.urllib.request, "build_opener", return_value=opener), \
             patch.dict(os.environ, {"SBSB_BROWSER_SIGNIN": "1" if browser else "0"}), \
             patch.object(main, "run_sbsb_browser_checkin", return_value=browser) as runner:
            result = main.do_sbsb_signin("synthetic-session", allow_submit=allow_submit)
        return result, opener, runner

    def test_readonly_unsigned_without_captcha_is_not_a_captcha_failure(self):
        result, opener, runner = self.flow([Response(UNSIGNED.replace("<cap-widget></cap-widget>", ""))])
        self.assertEqual(result["outcome"], "unsigned")
        self.assertFalse(result["needs_human"])
        self.assertNotIn("人机验证", result["msg"])
        self.assertFalse(result["submission_attempted"])
        self.assertEqual([c.args[0].get_method() for c in opener.open.call_args_list], ["GET"])
        runner.assert_not_called()

    def test_query_timeout_and_submission_confirmation_timeout_are_different(self):
        result, _, _ = self.flow([OSError("synthetic-sensitive-data")])
        self.assertEqual(result["outcome"], "query_unavailable")
        self.assertFalse(result["needs_human"])
        self.assertNotIn("synthetic-sensitive-data", str(result))
        result, _, _ = self.flow([Response(UNSIGNED), OSError("synthetic-sensitive-data")], allow_submit=True,
            browser=dict(success=False, submission_accepted=True, needs_human=True,
                         browser_stage="browser_confirmation_RuntimeError"))
        self.assertEqual(result["outcome"], "submission_unconfirmed")
        self.assertTrue(result["needs_human"])
        self.assertTrue(result["submission_accepted"])

    def test_browser_abort_without_evidence_does_not_claim_a_known_post(self):
        opener = Mock()
        opener.open.return_value = Response(UNSIGNED)
        with patch.object(main.urllib.request, "build_opener", return_value=opener), \
             patch.dict(os.environ, {"SBSB_BROWSER_SIGNIN": "1"}), \
             patch.object(main, "run_sbsb_browser_checkin", side_effect=RuntimeError("synthetic-private")):
            result = main.do_sbsb_signin("synthetic", True)
        self.assertEqual(result["outcome"], "submission_unconfirmed")
        self.assertTrue(result["submission_possible"])
        self.assertFalse(result["submission_attempted"])
        state = record_result({}, result, self.now)
        text, _ = format_signin(result, state, self.now)
        self.assertNotIn("程序已提交请求", text)
        self.assertNotIn("synthetic-private", text)

    def test_confirmed_signin_survives_optional_asset_failure(self):
        result, _, _ = self.flow([Response(SIGNED), OSError("synthetic")])
        self.assertTrue(result["success"])
        self.assertEqual(result["total_points"], UNKNOWN)
        self.assertEqual(result["signin_time"], UNKNOWN)
        self.assertEqual(result["consecutive_days"], "44 天")

    def test_after_midnight_ledger_time_from_same_forum_period_is_valid(self):
        self.now = datetime(2026, 10, 16, 0, 30, tzinfo=CST)
        result, _, _ = self.flow([Response(SIGNED), Response(POINTS.replace("2026-10-07", "2026-10-15"))])
        self.assertEqual(result["period"], "2026-10-15")
        self.assertEqual(result["signin_time"], "2026-10-15 08:00:04")
        text, _ = format_signin(result, record_result({}, result, self.now), self.now)
        self.assertIn("上一轮已签到", text)
        self.assertIn("10月16日 08:05", text)

    def test_period_boundary_cannot_attribute_previous_post_to_new_day(self):
        first = datetime(2026, 10, 15, 7, 59, tzinfo=CST)
        second = datetime(2026, 10, 15, 8, 1, tzinfo=CST)
        self.clock.now.side_effect = [first, first, second, second]
        opener = Mock()
        opener.open.side_effect = [Response(UNSIGNED), Response(status=303, location="/checkin/"),
                                   Response(SIGNED), Response(POINTS)]
        with patch.object(main.urllib.request, "build_opener", return_value=opener), patch.dict(os.environ, {"SBSB_BROWSER_SIGNIN": "0"}):
            result = main.do_sbsb_signin("synthetic", True)
        self.assertTrue(result["success"])
        self.assertTrue(result["already"])
        self.assertEqual(result["outcome"], "query_confirmed")

    def test_title_attributes_never_become_level_html_fragments(self):
        source = ('<div title="Lv.99 假等级 · 成长值 999">其他内容</div>'
                  '<span>等级</span><span class="value">Lv.8 热心新人 · 成长值 555</span>'
                  '<span>成长值</span><span class="value">555</span>'
                  '<span>可用烧饼</span><span class="value">0</span>')
        values = PointsParser(source).values("2026-10-15")
        self.assertEqual(values["level"], "Lv.8 热心新人")
        self.assertEqual(values["exp"], "555")
        self.assertEqual(values["total_points"], "0 饼")
        self.assertNotIn('\">', values["level"])

    def test_current_scoped_badge_wins_over_future_levels_and_ladder(self):
        source = ('<div class="points-progress-end-next"><span class="level-badge" title="Lv.9 社区常客 · 成长值 999">Lv.9</span></div>'
                  '<ol class="level-ladder"><li><span title="Lv.1 初来乍到">Lv.1</span></li></ol>'
                  '<span class="points-counter-value points-counter-level">'
                  '<span class="level-badge level-badge-lg" title="Lv.8 热心新人 · 成长值 555">'
                  '<span class="level-badge-n">Lv.8</span><span>热心新人</span></span></span>')
        values = PointsParser(source).values("2026-10-15")
        self.assertEqual(values["level"], "Lv.8 热心新人")
        self.assertEqual(values["exp"], "555")
        only_ladder = '<span class="level-n" title="Lv.1 初来乍到">Lv.1</span>'
        self.assertEqual(PointsParser(only_ladder).values("2026-10-15")["level"], UNKNOWN)

    def test_sidebar_daily_signin_link_cannot_reuse_unrelated_ledger_timestamp(self):
        source = ('<table><tbody><tr><td><time datetime="2026-10-15T01:46:19Z">09:46</time>'
                  '</td><td>其他积分变动</td></tr></tbody></table>'
                  '<aside class="sidebar"><a href="/checkin/">每日签到</a></aside>'
                  '<footer><a href="/checkin/">每日签到</a></footer>')
        self.assertEqual(PointsParser(source).values("2026-10-15")["signin_time"], UNKNOWN)
        signed_row = '<tr><td><time datetime="2026-10-15T00:05:31Z">08:05</time><td>每日签到</tr>'
        source = source.replace('</tbody>', signed_row + '</tbody>')
        self.assertEqual(PointsParser(source).values("2026-10-15")["signin_time"], "2026-10-15 08:05:31")

    def test_conflicting_same_period_signin_times_are_unknown_instead_of_picked(self):
        source = '<table>' + ''.join(f'<tr><td><time datetime="2026-10-15T{value}Z">时间</time><td>每日签到</tr>'
                                    for value in ('00:05:31', '01:46:19')) + '</table>'
        self.assertEqual(PointsParser(source).values("2026-10-15")["signin_time"], UNKNOWN)


class DurableSigninTest(BotCase):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch.object(main, "datetime"))
        self.clock.now.side_effect = lambda *args: self.now
        self.store = SigninStore(main.CHECKIN_STATE_FILE)

    def execute(self, result, automatic=False, allow_submit=True):
        with patch.object(main, "do_sbsb_signin", return_value=result) as worker:
            returned = main.execute_sbsb_signin(self.bot, allow_submit=allow_submit, automatic=automatic)
        return returned, worker

    def test_midnight_manual_query_cannot_complete_upcoming_0805_task(self):
        self.now = datetime(2026, 10, 16, 0, 30, tzinfo=CST)
        self.execute(success())
        state = self.store.load()
        self.assertEqual(state["last_date"], "2026-10-15")
        self.assertEqual(state["confirmation"]["period"], "2026-10-15")
        self.assertTrue(main.sbsb_checkin_due(datetime(2026, 10, 16, 8, 5, tzinfo=CST), state["last_date"]))
        self.assertIn("上一轮已签到", self.sent())

    def test_forum_period_and_schedule_boundaries(self):
        cases = [(datetime(2026,10,16,0,30,tzinfo=CST), "2026-10-15", False),
                 (datetime(2026,10,16,7,59,tzinfo=CST), "2026-10-15", False),
                 (datetime(2026,10,16,8,4,tzinfo=CST), "2026-10-15", False),
                 (datetime(2026,10,16,8,5,tzinfo=CST), "2026-10-15", True),
                 (datetime(2026,10,16,8,5,tzinfo=CST), "2026-10-16", False)]
        for now, completed, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(main.sbsb_checkin_due(now, completed), expected)

    def test_untrusted_legacy_future_completion_is_requeried_then_new_day_can_submit(self):
        self.now = datetime(2026,10,16,8,5,tzinfo=CST)
        self.store.save({"last_date":"2026-10-16"})
        unsigned = dict(success=False, outcome="unsigned", period="2026-10-16", needs_human=False)
        _, worker = self.execute(unsigned, automatic=True)
        self.assertFalse(worker.call_args.kwargs["allow_submit"])
        self.assertNotIn("last_date", self.store.load())
        _, worker = self.execute(success("2026-10-16", submitted=True), automatic=True)
        self.assertTrue(worker.call_args.kwargs["allow_submit"])
        self.assertEqual(self.store.load()["last_date"], "2026-10-16")

    def test_legacy_completed_record_upgrades_without_duplicate_notification_or_false_ack(self):
        self.store.save({"last_date":"2026-10-15"})
        _, worker = self.execute(success(), automatic=True)
        self.assertFalse(worker.call_args.kwargs["allow_submit"])
        self.bot.send_msg.assert_not_called()
        self.assertEqual(self.store.load()["notice"]["status"], "legacy_unknown")
        self.assertEqual(self.store.load()["confirmation"]["source"], "query_confirmed")

    def test_notice_failure_is_recorded_without_repeating_business(self):
        self.bot.send_msg.return_value = False
        self.execute(success(submitted=True), automatic=True)
        state = self.store.load()
        self.assertEqual(state["notice"]["status"], "unconfirmed")
        self.assertEqual(state["confirmation"]["source"], "submitted_confirmed")
        self.assertIn("通知发送未确认", self.bot.signin_status())
        _, worker = self.execute(success(submitted=True), automatic=True)
        worker.assert_not_called()
        self.assertEqual(self.bot.send_msg.call_count, 1)

    def test_previous_success_is_not_erased_by_later_query_failure(self):
        self.execute(success(submitted=True))
        first = self.store.load()["confirmation"]
        self.execute(dict(success=False, outcome="query_unavailable", period="2026-10-15"))
        self.assertEqual(self.store.load()["confirmation"], first)
        self.assertIn("签到已确认完成", self.sent())
        self.assertIn("最新查询未完成", self.sent())
        self.assertNotIn("签到失败", self.sent())

    def test_query_confirmation_does_not_overwrite_prior_submission_attribution(self):
        self.execute(success(submitted=True))
        result = success()
        result["signin_time"] = UNKNOWN
        result["level"] = UNKNOWN
        self.execute(result)
        state = self.store.load()
        self.assertEqual(state["confirmation"]["source"], "submitted_confirmed")
        self.assertIn("本次仅查询确认", self.sent())
        self.assertIn("08:05:31（此前核验）", self.sent())
        self.assertNotIn("成长值 555\">", self.sent())

    def test_new_period_does_not_inherit_old_asset_values(self):
        state = record_result({}, success(), self.now)
        tomorrow = datetime(2026,10,16,9,tzinfo=CST)
        result = success("2026-10-16")
        result.update(total_points=UNKNOWN, exp=UNKNOWN, level=UNKNOWN)
        updated = record_result(state, result, tomorrow)
        self.assertNotIn("total_points", updated["confirmation"])
        text, _ = format_signin(result, updated, tomorrow)
        self.assertNotIn("572", text)

    def test_accepted_submission_without_confirmation_blocks_post_but_keeps_querying(self):
        failed = dict(success=False, outcome="submission_unconfirmed", period="2026-10-15", needs_human=True,
                      submission_attempted=True, submission_accepted=True)
        self.execute(failed, automatic=True)
        self.assertIn("结果待确认", self.sent())
        self.assertIn("自动提交已暂停", self.sent())
        unsigned = dict(success=False, outcome="unsigned", period="2026-10-15", needs_human=False)
        _, worker = self.execute(unsigned, automatic=True)
        self.assertFalse(worker.call_args.kwargs["allow_submit"])
        self.assertEqual(self.store.load()["blocked_period"], "2026-10-15")
        _, worker = self.execute(success(), automatic=True)
        self.assertFalse(worker.call_args.kwargs["allow_submit"])
        self.assertNotIn("blocked_period", self.store.load())

    def test_stop_transition_generates_a_new_warning_instead_of_leaving_retry_promise(self):
        self.execute(dict(success=False, outcome="query_unavailable", period="2026-10-15"), automatic=True)
        first = self.bot.send_msg.call_count
        self.execute(dict(success=False, outcome="automation_stopped", period="2026-10-15", needs_human=True), automatic=True)
        self.assertEqual(self.bot.send_msg.call_count, first + 1)
        self.assertIn("自动提交已暂停", self.sent())

    def test_crash_guard_survives_and_recovery_is_query_only(self):
        class Crash(BaseException):
            pass
        with patch.object(main, "do_sbsb_signin", side_effect=Crash):
            with self.assertRaises(Crash):
                main.execute_sbsb_signin(self.bot, allow_submit=True)
        self.assertEqual(self.store.load()["submission_guard"]["period"], "2026-10-15")
        _, worker = self.execute(dict(success=False, outcome="unsigned", period="2026-10-15"), automatic=True)
        self.assertFalse(worker.call_args.kwargs["allow_submit"])
        self.assertIn("submission_guard", self.store.load())

    def test_corrupt_state_is_preserved_and_does_not_start_submission(self):
        Path(main.CHECKIN_STATE_FILE).write_text("{broken")
        _, worker = self.execute(success(submitted=True), automatic=True)
        worker.assert_not_called()
        self.assertEqual(Path(main.CHECKIN_STATE_FILE).read_text(), "{broken")

    def test_malformed_nested_state_does_not_break_status_or_allow_submission(self):
        for field in ("notice", "last_query", "submission_guard"):
            with self.subTest(field=field):
                self.store.save(dict(version=2, **{field: "broken"}))
                _, worker = self.execute(success(submitted=True), automatic=True)
                worker.assert_not_called()
                self.assertIn("记录读取异常", self.bot.signin_status())

    def test_scheduler_and_manual_processing_cannot_submit_concurrently(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def work(cookie, allow_submit):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("synthetic_timeout")
            return success(submitted=True)
        def run():
            try:
                main.execute_sbsb_signin(self.bot, allow_submit=True, automatic=True)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(main, "do_sbsb_signin", side_effect=work) as worker:
            one = threading.Thread(target=run)
            two = threading.Thread(target=run)
            one.start()
            self.assertTrue(entered.wait(1))
            two.start()
            release.set()
            one.join(2); two.join(2)
        self.assertFalse(one.is_alive() or two.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.store.load()["notice"]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
