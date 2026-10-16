import html
import io
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

from app import main
from app.signin_state import CST, SigninStore
from app.telegram_text import split_html, fit_html, units, escape, valid_markup


class HTMLCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text, self.tags = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in {"b", "i", "code", "pre", "u", "s", "a"}:
            raise AssertionError(tag)
        self.stack.append(tag)
        self.tags.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise AssertionError("unbalanced_html")

    def handle_data(self, value):
        self.text.append(value)


def parsed(value):
    result = HTMLCheck()
    result.feed(value)
    result.close()
    assert not result.stack
    return result


class Response(io.BytesIO):
    status = 200
    headers = {}


class BotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {"TG_CHAT_ID": "100", "TG_BOT_TOKEN": "synthetic-bot",
                                                "SBSB_COOKIE": "synthetic-session"}, clear=True))
        for name, value in vars(main).copy().items():
            if name.endswith("_FILE") and isinstance(value, str):
                self.enterContext(patch.object(main, name, str(self.folder / os.path.basename(value))))
        self.enterContext(patch.object(main, "DATA_DIR", str(self.folder)))
        self.enterContext(patch.object(main.BotManager, "register_telegram_commands"))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("network_blocked")))
        self.enterContext(patch.object(main.urllib.request, "urlopen", side_effect=AssertionError("network_blocked")))
        self.bot = main.BotManager()
        self.bot.send_msg = Mock(return_value=True)
        self.bot.edit_msg_text = Mock(return_value=True)
        self.bot.answer_callback_query = Mock(return_value=True)
        self.bot.delete_msg = Mock(return_value=False)
        self.now = datetime(2026, 10, 15, 21, 21, tzinfo=CST)
        self.bot.local_now = lambda: self.now
        self.bot.get_today_cst = lambda: "2026-10-15"

    def command(self, value, message_id=7):
        main.handle_command_or_text(self.bot, "100", value, message_id)

    def callback(self, value):
        main.handle_callback_query(self.bot, {"id": "query", "from": {"id": 100},
            "message": {"chat": {"id": "100"}, "message_id": 8}, "data": value})

    def sent(self):
        return self.bot.send_msg.call_args.args[1]


class FormattingTest(BotCase):
    def test_source_fields_are_literal_and_unsupported_entities_are_decoded(self):
        main.deliver_public_match(self.bot, "NodeSeek", "🌐", "A&B", "<VPS> & 福利", "A&B",
                                  "<p>内容&nbsp;A&amp;B&hellip; &lt;说明&gt;</p>",
                                  "https://example.invalid/t/1/", "lottery", "示例")
        result = parsed(self.sent())
        self.assertEqual(result.tags, ["b", "b"])
        self.assertIn("<VPS> & 福利", "".join(result.text))
        self.assertIn("内容\xa0A&B… <说明>", "".join(result.text))
        self.assertNotIn("&nbsp;", self.sent())
        self.assertEqual(self.bot.send_msg.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://example.invalid/t/1/")

    def test_long_html_is_split_balanced_without_losing_text_or_emoji(self):
        source = "<b>" + escape("🎁<&>" * 2600) + "</b>"
        chunks = split_html(source)
        self.assertGreater(len(chunks), 1)
        texts = []
        for chunk in chunks:
            text = "".join(parsed(chunk).text)
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
            texts.append(text)
        self.assertEqual("".join(texts), "🎁<&>" * 2600)
        self.assertLessEqual(units("".join(parsed(fit_html(source)).text)), 4096)

    def test_zero_values_and_unsafe_buttons(self):
        self.assertEqual(escape(0), "0")
        markup = valid_markup({"inline_keyboard": [[{"text": "x", "url": "javascript:alert(1)"},
                 {"text": "x", "callback_data": "关" * 30}, {"text": "OK", "callback_data": "menu:status"}]]})
        self.assertEqual(markup, {"inline_keyboard": [[{"text": "OK", "callback_data": "menu:status"}]]})

    def test_all_common_panels_have_valid_html_bounded_messages_and_callbacks(self):
        self.bot.keywords = ["<关注>&" + "长" * 1000] * 20
        self.bot.nodeseek_ai_review = {str(i): {"title": "<标题> &" * 100, "queued_at": self.now.timestamp(),
                                              "link": "https://example.invalid/"} for i in range(25)}
        cards = [self.bot.panel(name) for name in ("status", "report", "report_detail", "participation", "sources",
                 "ai", "keywords", "review", "budget", "help", "help_detail")]
        cards += [self.bot.format_keywords_card(1, True), self.bot.format_review_list("ai", 1)]
        for text, markup in cards:
            with self.subTest(title=text.splitlines()[0]):
                self.assertLess(units("".join(parsed(text).text)), 4096)
                for row in markup["inline_keyboard"]:
                    for button in row:
                        if "callback_data" in button:
                            self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_api_response_ok_is_required_and_full_text_only_has_last_chunk_buttons(self):
        requests = []
        def send(request, timeout):
            requests.append(json.loads(request.data))
            return Response(b'{"ok":true}')
        with patch.object(main.urllib.request, "urlopen", side_effect=send):
            result = main.BotManager.send_msg(self.bot, "100", "<b>" + "饼" * 9000 + "</b>",
                                              {"inline_keyboard": [[{"text": "返回", "callback_data": "menu:status"}]]})
        self.assertTrue(result)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all("reply_markup" not in row for row in requests[:-1]))
        self.assertIn("reply_markup", requests[-1])
        self.assertEqual("".join("".join(parsed(row["text"]).text) for row in requests), "饼" * 9000)
        with patch.object(main.urllib.request, "urlopen", return_value=Response(b'{"ok":false}')):
            self.assertFalse(main.BotManager.delete_msg(self.bot, "100", 7))
        with patch.object(main.urllib.request, "urlopen", return_value=Response(b'{"ok":true,"result":false}')):
            self.assertFalse(main.BotManager.delete_msg(self.bot, "100", 7))
        with patch.object(main.urllib.request, "urlopen", return_value=Response(b'{"ok":true,"result":true}')):
            self.assertTrue(main.BotManager.delete_msg(self.bot, "100", 7))

    def test_callback_toast_is_bounded(self):
        payload = []
        with patch.object(self.bot, "_telegram_call", side_effect=lambda method, value: payload.append(value) or True):
            main.BotManager.answer_callback_query(self.bot, "query", "🎁" * 300)
        self.assertLessEqual(units(payload[0]["text"]), 200)


class InputAndButtonsTest(BotCase):
    def test_invalid_model_keeps_state_then_correct_model_saves(self):
        self.callback("ai:set_model")
        self.command("bad model")
        self.assertEqual(self.bot.user_states["100"], "waiting_for_ai_model")
        self.command("gemini-3.8-flash-high")
        self.assertEqual(self.bot.ai_model, "gemini-3.8-flash-high")
        self.assertNotIn("100", self.bot.user_states)
        self.assertNotIn("未识别", self.sent())
        self.assertLess(len(self.sent()), 150)

    def test_cancel_and_navigation_end_input_without_changing_configuration(self):
        self.callback("ai:set_model")
        previous = self.bot.ai_model
        self.command("/cancel")
        self.assertNotIn("100", self.bot.user_states)
        self.assertEqual(previous, self.bot.ai_model)
        self.callback("ai:set_model")
        self.callback("menu:status")
        self.assertNotIn("100", self.bot.user_states)

    def test_unknown_callback_acknowledges_without_mutation(self):
        before = self.bot.keywords[:]
        self.callback("del_kw:outdated")
        self.assertIn("按钮已过期", self.bot.answer_callback_query.call_args.args[1])
        self.assertEqual(before, self.bot.keywords)

    def test_repeated_desired_state_buttons_never_reverse_the_requested_setting(self):
        self.callback("source:sbsb:0")
        self.callback("source:sbsb:0")
        self.assertFalse(self.bot.source_states["sbsb"])
        self.bot.participant.enabled = True
        self.callback("participation:0")
        self.callback("participation:0")
        self.assertFalse(self.bot.sbsb_participation_enabled)
        self.callback("toggle_src:sbsb")
        self.assertFalse(self.bot.source_states["sbsb"])
        self.assertIn("过期", self.bot.answer_callback_query.call_args.args[1])

    def test_unicode_keyword_short_ids_and_stale_delete_do_not_delete_another_word(self):
        first, second = "关" * 25, "另一个关注词"
        self.bot.keywords = [first, second]
        _, markup = self.bot.format_keywords_card(delete=True)
        token = markup["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(token.encode()), 64)
        self.bot.keywords.remove(first)
        self.callback(token)
        self.assertEqual(self.bot.keywords, [second])
        self.assertIn("过期", self.bot.answer_callback_query.call_args.args[1])

    def test_restart_expires_opaque_buttons(self):
        self.bot.keywords = ["DMIT"]
        _, markup = self.bot.format_keywords_card(delete=True)
        token = markup["inline_keyboard"][0][0]["callback_data"]
        self.bot.ui_actions.clear()
        self.callback(token)
        self.assertEqual(self.bot.keywords, ["DMIT"])

    def test_keyword_delete_save_failure_restores_in_memory_list(self):
        self.bot.keywords = ["DMIT", "VPS"]
        _, markup = self.bot.format_keywords_card(delete=True)
        with patch.object(self.bot, "save_settings", side_effect=OSError("synthetic")):
            self.callback(markup["inline_keyboard"][0][0]["callback_data"])
        self.assertEqual(self.bot.keywords, ["DMIT", "VPS"])
        self.assertIn("未完成", self.bot.answer_callback_query.call_args.args[1])

    def test_full_long_keyword_is_accessible_without_edit_truncation(self):
        word = "关注" * 2500
        self.bot.keywords = [word]
        _, markup = self.bot.format_keywords_card()
        self.callback(markup["inline_keyboard"][0][0]["callback_data"])
        self.assertIn(word, self.sent())
        self.bot.edit_msg_text.assert_not_called()

    def test_repeated_and_legacy_keywords_never_claim_empty_success(self):
        self.command("/add DMIT")
        self.command("/add DMIT 抽奖 搬瓦工")
        self.assertIn("已添加：搬瓦工", self.sent())
        self.assertIn("已存在：DMIT", self.sent())
        self.assertIn("未添加", self.sent())
        self.command("/add DMIT 抽奖")
        self.assertIn("未添加新关注词", self.sent())
        self.assertNotIn("✅", self.sent())

    def test_secret_save_and_deletion_have_independent_feedback(self):
        self.callback("ai:set_key")
        self.command("synthetic-private-key")
        self.assertIn("已保存", self.sent())
        self.assertIn("删除失败", self.sent())
        self.assertNotIn("synthetic-private-key", self.sent())
        self.assertNotIn("已删除", self.sent())
        self.assertEqual(os.stat(main.AI_SECRET_FILE).st_mode & 0o777, 0o600)
        self.bot.delete_msg.return_value = True
        self.callback("ai:set_key")
        self.command("synthetic-replacement-key")
        self.assertIn("原消息已删除", self.sent())

    def test_locked_secret_is_not_saved_and_delete_failure_is_not_hidden(self):
        self.bot.user_states["100"] = "waiting_for_ai_key"
        self.bot.env_ai_api_key = "synthetic-configured-key"
        self.command("synthetic-not-to-save")
        self.assertIn("未更改", self.sent())
        self.assertIn("删除失败", self.sent())
        self.assertFalse(Path(main.AI_SECRET_FILE).exists())

    def test_secret_save_error_still_reports_original_message_deletion_result(self):
        self.callback("ai:set_key")
        with patch.object(self.bot, "save_ai_secret", side_effect=OSError("synthetic-private")):
            self.command("synthetic-secret")
        self.assertIn("保存结果未确认", self.sent())
        self.assertIn("删除失败", self.sent())
        self.assertNotIn("synthetic-private", self.sent())
        self.assertIn("100", self.bot.user_states)

    def test_setting_save_failure_keeps_previous_value_and_input_mode(self):
        self.bot.ai_model = "previous"
        self.callback("ai:set_model")
        with patch.object(self.bot, "save_settings", side_effect=OSError("synthetic-private")):
            self.command("replacement")
        self.assertEqual(self.bot.ai_model, "previous")
        self.assertIn("保存失败", self.sent())
        self.assertEqual(self.bot.user_states["100"], "waiting_for_ai_model")

    def test_all_locked_fields_refuse_callback_and_pending_input(self):
        for field, env, state, value in (("endpoint", "env_ai_endpoint", "waiting_for_ai_endpoint", "https://example.invalid/v1"),
                ("model", "env_ai_model", "waiting_for_ai_model", "locked-model"),
                ("judge", "env_ai_judge_model", "waiting_for_ai_judge", "locked-judge"),
                ("threshold", "env_ai_accept_threshold", "waiting_for_ai_threshold", .9)):
            with self.subTest(field=field):
                setattr(self.bot, env, value)
                self.callback("ai:set_" + field)
                self.assertNotIn("100", self.bot.user_states)
                self.assertIn("服务器配置", self.sent())
                self.bot.user_states["100"] = state
                self.command("0.85" if field == "threshold" else "replacement")
                self.assertIn("服务器配置", self.sent())
                self.assertNotIn("已保存", self.sent())

    def test_ai_disable_survives_restart_with_environment_credentials(self):
        with patch.dict(os.environ, {"AI_ENDPOINT": "https://example.invalid/v1", "AI_MODEL": "synthetic-model", "AI_API_KEY": "synthetic-key"}):
            # Legacy settings lacking enabled use configured defaults once.
            saved = json.loads(Path(main.SETTINGS_FILE).read_text())
            saved.pop("ai_enabled")
            Path(main.SETTINGS_FILE).write_text(json.dumps(saved))
            first = main.BotManager()
            self.assertTrue(first.ai_enabled)
            first.ai_enabled = False
            first.save_settings()
            restarted = main.BotManager()
        self.assertFalse(restarted.ai_enabled)

    def test_default_ai_panel_never_exposes_endpoint_or_key_and_test_enum_is_translated(self):
        self.bot.ai_endpoint = "https://private.invalid/v1"
        self.bot.ai_api_key = "synthetic-sensitive-key"
        self.bot.ai_model = "gemini-3.8-flash-high"
        text, _ = self.bot.format_ai_card()
        self.assertNotIn("private.invalid", text)
        self.assertNotIn("synthetic-sensitive-key", text)
        self.bot.test_ai_connection = lambda: {"event_state": "open", "confidence": .95}
        self.callback("ai:test")
        self.assertIn("活动进行中", self.sent())
        self.assertNotIn(">open<", self.sent())


class StatisticsAndDetailTest(BotCase):
    def test_discovery_and_notification_are_different_counts_and_empty_rate_is_absent(self):
        self.bot.daily_stats.update(date="2026-10-15", lottery_hits=35, delivery_success=13, delivery_errors=0)
        report = self.bot.generate_daily_report_card()
        self.assertIn("发现活动 35", report)
        self.assertIn("发送成功 13", report)
        self.assertNotIn("送达", report)
        self.assertNotIn("24h", report)
        self.bot.daily_stats["delivery_success"] = 0
        report = self.bot.generate_daily_report_card()
        self.assertIn("暂无发送记录", report)
        self.assertNotIn("100%", report)

    def test_stale_statistics_reset_on_read_without_mutating_old_persisted_stats(self):
        self.bot.daily_stats.update(date="2026-10-14", lottery_hits=99, delivery_success=90)
        text = self.bot.generate_daily_report_card()
        self.assertIn("发现活动 0", text)
        self.assertNotIn("发送成功 90", text)
        self.assertEqual(self.bot.daily_stats["lottery_hits"], 99)

    def test_push_disabled_participation_and_cookie_are_not_mislabelled(self):
        self.bot.source_states["sbsb"] = False
        self.bot.participant.enabled = True
        self.bot.participant.planner = Mock(key="synthetic")
        text, _ = self.bot.format_status_card()
        self.assertIn("烧饼活动推送：关 · 自动参与：开", text)
        self.assertNotIn("签到：已完成", text)
        self.assertNotIn("运行中", text)

    def test_ledger_detail_is_read_only_and_preserves_submission_attribution(self):
        rows = {
            "1": dict(kind="redpacket", status="confirmed", submission_accepted=False, cost=0,
                      day="2026-10-15", at=self.now.timestamp(), title="查询确认"),
            "2": dict(kind="lottery", status="confirmed", submission_accepted=True, cost=10,
                      day="2026-10-15", at=self.now.timestamp(), title="程序提交"),
            "3": dict(kind="redpacket", status="review", cost=0, day="2026-10-15", at=self.now.timestamp(),
                      title="<需处理>&", reason="manual_requirements", diagnostics={"reply_length_rules": ["不少于20字"]}),
            "4": dict(kind="redpacket", status="uncertain", cost=0, day="2026-10-14", at=self.now.timestamp(), title="旧未确认")}
        self.bot.participant.ledger.save(dict(version=1, entries=rows))
        path = Path(self.bot.participant.ledger.path)
        before = path.read_bytes()
        report = self.bot.generate_daily_report_card()
        self.assertIn("烧饼抽奖：提交确认 1 项 · 查询确认 0 项", report)
        self.assertIn("烧饼红包：提交确认 0 项 · 查询确认 1 项", report)
        self.assertIn("结果未确认 0 项", report)
        index, _ = self.bot.format_review_index()
        self.assertIn("历史活动待处理 / 未确认：1", index)
        _, markup = self.bot.format_review_list("redpacket")
        self.callback(markup["inline_keyboard"][0][0]["callback_data"])
        detail = self.bot.edit_msg_text.call_args.args[2]
        self.assertIn("不少于20字", detail)
        self.assertEqual(path.read_bytes(), before)
        for row in self.bot.edit_msg_text.call_args.kwargs["reply_markup"]["inline_keyboard"]:
            for button in row:
                self.assertNotIn("重试", button["text"])
                self.assertNotIn("参与", button["text"])

    def test_old_ai_entries_are_paged_and_unknown_dates_not_counted_as_today(self):
        self.bot.nodeseek_ai_review = {str(i): {"title": f"标题{i}", "queued_at": (self.now - timedelta(days=10)).timestamp(),
                                     "last_error": "HTTP 503", "link": "https://example.invalid/"} for i in range(177)}
        self.bot.nodeseek_ai_review["unknown"] = {"title": "未记录日期"}
        index, _ = self.bot.format_review_index()
        self.assertIn("今日新增复核：0", index)
        self.assertIn("历史存量：177", index)
        self.assertIn("日期未确认：1", index)
        text, markup = self.bot.format_review_list("ai", 1)
        self.assertIn("2 / 23", text)
        self.assertLess(units(text), 4096)
        self.assertTrue(any(button["text"] == "下一页" for row in markup["inline_keyboard"] for button in row))

    def test_help_exposes_ai_report_cancel_and_private_capability(self):
        help_text, _ = self.bot.format_help()
        for value in ("/ai", "/report", "/cancel"):
            self.assertIn(value, help_text)
        details, _ = self.bot.format_help(True)
        self.assertIn("已有对话的新消息不保证提醒", details)


class PauseAndPrivateTest(BotCase):
    HOME = ('<div class="top"><div class="nav-user-links"><a href="/u/7/">主页</a>'
            '<a href="/u/7/notifications/">通知</a></div></div>')

    def private_run(self, paused=False, resume=False):
        self.bot.paused = paused
        self.bot.seen_msgs = {"existing"}
        self.bot.seen_msg_order = ["existing"]
        item = (self.HOME + '<li class="notification-item unread">'
                '<a href="/u/8/">A&amp;B</a><span class="notification-kind">回复</span>'
                '<div class="notification-content">内容&nbsp;&lt;VPS&gt;&hellip;</div>'
                '<time>2分钟前</time><a class="notification-reply-action" href="/t/1/?reply=77">查看</a></li>')
        def response(request, **kwargs):
            if request.full_url == "https://sb.sb/":
                body = self.HOME
            elif request.full_url.endswith("/notifications/"):
                body = item
            elif request.full_url == "https://sb.sb/messages/":
                body = '<a href="/messages/' + 'a' * 32 + '/">对话</a>'
            else:
                raise AssertionError("unexpected_target")
            return Response(body.encode())
        class Stop(BaseException):
            pass
        loops = []
        def sleep(seconds):
            loops.append(seconds)
            if resume and len(loops) == 1:
                self.bot.paused = False
                return
            raise Stop
        opener = Mock()
        opener.open.side_effect = response
        with patch.object(main.urllib.request, "build_opener", return_value=opener), \
             patch.object(main.time, "sleep", side_effect=sleep), patch("builtins.print"):
            with self.assertRaises(Stop):
                main.sbsb_private_messages_thread(self.bot)

    def test_pause_records_private_and_interaction_dedup_without_sending_or_counting(self):
        self.private_run(paused=True)
        self.bot.send_msg.assert_not_called()
        self.assertIn("notif:reply_77", self.bot.seen_msgs)
        self.assertIn("thread:" + "a" * 32, self.bot.seen_msgs)
        self.assertEqual(self.bot.total_private_notified, 0)

    def test_resume_does_not_replay_private_and_interaction_seen_while_paused(self):
        self.private_run(paused=True, resume=True)
        self.bot.send_msg.assert_not_called()
        self.assertEqual(self.bot.total_private_notified, 0)

    def test_private_and_interaction_are_safe_short_cards_when_enabled(self):
        self.private_run()
        self.assertEqual(self.bot.send_msg.call_count, 2)
        interaction, private = [call.args[1] for call in self.bot.send_msg.call_args_list]
        text = "".join(parsed(interaction).text)
        self.assertIn("A&B", text)
        self.assertIn("<VPS>…", text)
        self.assertNotIn("&nbsp;", interaction)
        self.assertIn("新私信对话", private)
        self.assertNotIn("对话编号", private)

    def test_pause_suppresses_confirmed_login_warning(self):
        self.bot.paused = True
        from app.main import SbsbIdentityError
        class Stop(BaseException):
            pass
        with patch.object(main, "fetch_sbsb_current_uid", side_effect=SbsbIdentityError("login_required")), \
             patch.object(main.time, "sleep", side_effect=Stop), patch("builtins.print"):
            with self.assertRaises(Stop):
                main.sbsb_private_messages_thread(self.bot)
        self.bot.send_msg.assert_not_called()

    def test_pause_scope_keeps_signin_and_report_separate_and_resume_preserves_source_switches(self):
        self.bot.source_states["sbsb"] = False
        self.command("/pause")
        self.assertTrue(self.bot.paused)
        self.assertIn("签到、签到结果通知与定时日报照常执行", self.sent())
        self.command("/resume")
        self.assertFalse(self.bot.paused)
        self.assertFalse(self.bot.source_states["sbsb"])

    def test_public_push_rechecks_pause_and_source_after_inflight_work(self):
        self.bot.paused = True
        self.assertFalse(main.deliver_public_match(self.bot, "NodeSeek", "🌐", "日常", "标题", "作者", "摘要",
                                                  "https://example.invalid/", "lottery", "示例"))
        self.bot.paused = False
        self.bot.source_states["sbsb"] = False
        self.assertFalse(main.deliver_public_match(self.bot, "烧饼论坛", "🍪", "日常", "标题", "作者", "摘要",
                                                  "https://example.invalid/", "lottery", "示例"))
        self.bot.send_msg.assert_not_called()


class FinalEligibilityFeedbackTest(BotCase):
    def test_terminal_review_feedback_is_literal_read_only_and_never_requests_manual_work(self):
        for reason, expected in (("final_review_denied", "第二轮终审未通过"),
                                 ("final_review_unclear", "第二轮终审仍不明确"),
                                 ("final_review_unavailable", "第二轮终审调用失败")):
            row = dict(kind="redpacket", status="skipped", cost=0, day="2026-10-15",
                       at=self.now.timestamp(), title="<合成标题>&", reason=reason,
                       diagnostics={"eligibility": {"rounds": 2, "first": "pending", "final": "skip"}})
            self.bot.participant.ledger.save(dict(version=1, entries={"1": row}))
            path = Path(self.bot.participant.ledger.path); before = path.read_bytes()
            text, markup = self.bot.format_review_item("redpacket", "1", 0)
            self.assertIn(expected, text)
            self.assertIn("资格初筛：待定", text)
            self.assertIn("第二轮终审：未通过，已终止", text)
            self.assertNotIn("人工", text); parsed(text)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(any("重试" in b["text"] for r in markup["inline_keyboard"] for b in r))
            card, _ = self.bot.format_participation_card()
            self.assertIn("跳过：1 项", card)
            self.assertIn("不转人工", card)


if __name__ == "__main__":
    unittest.main()
