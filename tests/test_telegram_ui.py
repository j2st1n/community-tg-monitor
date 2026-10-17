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
                 "settings", "records", "keywords", "review", "budget", "help", "help_detail")]
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
    def test_retired_ai_buttons_never_open_input_or_modify_server_configuration(self):
        before = (self.bot.ai_model, self.bot.ai_judge_model, self.bot.ai_accept_threshold, self.bot.ai_enabled)
        self.bot.test_ai_connection = Mock(side_effect=AssertionError("model_calls_forbidden"))
        for data in ("ai:set_model", "ai:set_judge", "ai:set_threshold", "ai:set_endpoint", "ai:set_key", "ai:enabled:0", "ai:test", "menu:ai"):
            self.callback(data)
            self.assertNotIn("100", self.bot.user_states)
            self.assertEqual(before, (self.bot.ai_model, self.bot.ai_judge_model, self.bot.ai_accept_threshold, self.bot.ai_enabled))
            self.assertIn("服务器配置", self.bot.answer_callback_query.call_args.args[1])
        self.bot.test_ai_connection.assert_not_called()

    def test_cancel_and_navigation_end_keyword_input_without_changing_configuration(self):
        self.callback("menu:add_kw")
        previous = self.bot.keywords[:]
        self.command("/cancel")
        self.assertNotIn("100", self.bot.user_states)
        self.assertEqual(previous, self.bot.keywords)
        self.callback("menu:add_kw")
        self.callback("menu:records")
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

    def test_stale_secret_input_never_saves_and_reports_deletion_result(self):
        self.bot.user_states["100"] = "waiting_for_ai_key"
        self.command("synthetic-private-key")
        self.assertIn("未更改", self.sent())
        self.assertIn("删除失败", self.sent())
        self.assertNotIn("synthetic-private-key", self.sent())
        self.assertFalse(Path(main.AI_SECRET_FILE).exists())
        self.bot.delete_msg.return_value = True
        self.bot.user_states["100"] = "waiting_for_ai_key"
        self.command("synthetic-replacement-key")
        self.assertIn("原消息已删除", self.sent())
        self.assertFalse(Path(main.AI_SECRET_FILE).exists())

    def test_locked_secret_is_not_saved_and_delete_failure_is_not_hidden(self):
        self.bot.user_states["100"] = "waiting_for_ai_key"
        self.bot.env_ai_api_key = "synthetic-configured-key"
        self.command("synthetic-not-to-save")
        self.assertIn("未更改", self.sent())
        self.assertIn("删除失败", self.sent())
        self.assertFalse(Path(main.AI_SECRET_FILE).exists())

    def test_stale_secret_input_never_invokes_secret_writer(self):
        self.bot.user_states["100"] = "waiting_for_ai_key"
        with patch.object(self.bot, "save_ai_secret", side_effect=AssertionError("writer_forbidden")) as writer:
            self.command("synthetic-secret")
        writer.assert_not_called()
        self.assertIn("未更改", self.sent())
        self.assertNotIn("synthetic-secret", self.sent())
        self.assertNotIn("100", self.bot.user_states)

    def test_keyword_save_failure_keeps_previous_value_and_input_mode(self):
        previous = self.bot.keywords[:]
        self.callback("menu:add_kw")
        with patch.object(self.bot, "save_settings", side_effect=OSError("synthetic-private")):
            self.command("replacement")
        self.assertEqual(self.bot.keywords, previous)
        self.assertEqual(self.bot.user_states["100"], "waiting_for_add")
        self.assertNotIn("synthetic-private", self.sent())

    def test_all_old_ai_input_modes_are_retired_even_without_environment_locks(self):
        for state, attribute in (("waiting_for_ai_endpoint", "ai_endpoint"), ("waiting_for_ai_model", "ai_model"),
                                 ("waiting_for_ai_judge", "ai_judge_model"), ("waiting_for_ai_threshold", "ai_accept_threshold")):
            previous = getattr(self.bot, attribute)
            self.bot.user_states["100"] = state
            self.command("replacement")
            self.assertEqual(getattr(self.bot, attribute), previous)
            self.assertNotIn("100", self.bot.user_states)
            self.assertIn("服务器配置", self.sent())

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

    def test_settings_and_legacy_commands_never_expose_server_configuration_or_run_tests(self):
        self.bot.ai_endpoint = "https://private.invalid/v1"
        self.bot.ai_api_key = "synthetic-sensitive-key"
        self.bot.test_ai_connection = Mock()
        for command in ("/settings", "/sources", "/ai", "/test"):
            self.command(command)
            self.assertNotIn("private.invalid", self.sent())
            self.assertNotIn("synthetic-sensitive-key", self.sent())
        self.bot.test_ai_connection.assert_not_called()


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

    def test_all_outcomes_are_queryable_without_mutation_or_false_submission_attribution(self):
        rows = {
            "1": dict(kind="redpacket", status="confirmed", submission_accepted=False, cost=0,
                      day="2026-10-15", at=self.now.timestamp(), title="查询确认", reply="未单独核实的旧回复"),
            "2": dict(kind="lottery", status="confirmed", submission_accepted=True, cost=10,
                      day="2026-10-15", at=self.now.timestamp(), title="程序提交"),
            "3": dict(kind="redpacket", status="skipped", cost=0, day="2026-10-15", at=self.now.timestamp(),
                      title="<已跳过>&", reason="final_review_unclear", topic_body="<帖子正文>&", diagnostics={"eligibility":{"first":"pending","rounds":2,"final":"skip"}}),
            "4": dict(kind="redpacket", status="uncertain", cost=0, day="2026-10-14", at=self.now.timestamp(), title="旧提交异常", attempted_reply="拟发文案")}
        self.bot.participant.ledger.save(dict(version=1, entries=rows))
        path = Path(self.bot.participant.ledger.path); before = path.read_bytes()
        self.bot.participant.run = Mock(side_effect=AssertionError("no_participation"))
        self.bot.participant.reconcile_pending = Mock(side_effect=AssertionError("no_state_recovery"))
        report = self.bot.generate_daily_report_card()
        for text in ("自动参与成功：1 项", "账号查询确认已参与：1 项", "已跳过：1 项", "提交异常：0 项"):
            self.assertIn(text, report)
        listing, markup = self.bot.format_records_card()
        self.assertEqual(len([b for row in markup['inline_keyboard'] for b in row if b.get('callback_data','').startswith('ui:')]), 4)
        for row in markup['inline_keyboard']:
            for b in row:
                if b.get('callback_data','').startswith('ui:'):
                    self.callback(b['callback_data'])
                    detail=self.bot.edit_msg_text.call_args.args[2]
                    parsed(detail)
                    self.assertIn("发布时间：暂未获取", detail)
                    self.assertIn("回复时间：暂未获取", detail)
                    self.assertNotIn("等待人工", detail)
        error,_=self.bot.format_record_item("4")
        self.assertIn("提交异常", error); self.assertIn("拟提交回复", error); self.assertIn("不重复提交", error)
        self.assertNotIn("结果待确认", error)
        skipped,_=self.bot.format_record_item("3")
        self.assertIn("第二轮终审仍不明确，已跳过", skipped); self.assertIn("&lt;帖子正文&gt;&amp;", skipped)
        query,_=self.bot.format_record_item("1")
        self.assertIn("未单独核实", query); self.assertIn("不能据此认定由本程序提交", query)
        self.assertEqual(path.read_bytes(), before)
        self.bot.participant.run.assert_not_called(); self.bot.participant.reconcile_pending.assert_not_called()

    def test_record_paging_keeps_all_dates_and_does_not_expose_legacy_ai_queue(self):
        self.bot.nodeseek_ai_review = {"old": {"title": "legacy-ai-private", "last_error": "HTTP 503"}}
        rows={str(i):dict(kind="redpacket",status="skipped",cost=0,day="2026-10-14",at=self.now.timestamp()+i,title=f"标题{i}") for i in range(1,18)}
        self.bot.participant.ledger.save(dict(version=1,entries=rows))
        before=dict(self.bot.nodeseek_ai_review)
        text,markup=self.bot.format_records_card(1)
        self.assertIn("2 / 3",text); self.assertIn("共 17 项",text)
        self.assertNotIn("legacy-ai-private",text)
        self.assertTrue(any(b['text']=='下一页' for row in markup['inline_keyboard'] for b in row))
        self.assertEqual(self.bot.nodeseek_ai_review,before)
        self.assertLess(units(text),4096)

    def test_home_has_four_daily_entries_and_settings_only_has_business_switches(self):
        text, markup = self.bot.format_status_card()
        self.assertEqual([b['text'] for row in markup['inline_keyboard'] for b in row],['签到与资产','今日汇总','参与记录','设置'])
        settings, markup = self.bot.format_sources_card()
        actions=[b.get('callback_data','') for row in markup['inline_keyboard'] for b in row]
        self.assertEqual(sum(a.startswith(('source:','participation:')) for a in actions),3)
        self.assertFalse(any(a.startswith('ai:') or a in {'menu:pause','menu:resume','menu:review'} for a in actions))
        help_text, _=self.bot.format_help()
        for word in ('/settings','/records','/signin','/report'):
            self.assertIn(word,help_text)
        for word in ('/ai','/test','/pause','API','复审模型'):
            self.assertNotIn(word,help_text+settings+text)


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
            self.assertNotIn("资格初筛：待定", text)
            self.assertIn("已跳过", text)
            self.assertNotIn("人工", text); parsed(text)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(any("重试" in b["text"] for r in markup["inline_keyboard"] for b in r))
            card, _ = self.bot.format_participation_card()
            self.assertIn("已跳过", card)
            self.assertNotIn("待人工", card)


class DailyMenuRoutingTest(BotCase):
    def test_signin_commands_and_button_are_query_only(self):
        self.bot.query_signin = Mock()
        for cmd in ("/signin", "/checkin"):
            self.command(cmd)
            self.bot.query_signin.assert_called_with("100", allow_submit=False)
        self.callback("signin:query")
        self.bot.query_signin.assert_called_with("100", allow_submit=False)
        self.assertEqual(self.bot.query_signin.call_count, 3)
        from app.signin_feedback import signin_markup
        self.assertIn("menu:status", [b.get("callback_data") for row in signin_markup()["inline_keyboard"] for b in row])

    def test_combined_pause_buttons_expire_without_changing_runtime(self):
        self.bot.paused = False
        for callback in ("menu:pause", "menu:resume"):
            self.callback(callback)
            self.assertFalse(self.bot.paused)
            self.assertIn("状态开关", self.bot.answer_callback_query.call_args.args[1])
        self.bot.paused = True
        text, markup = self.bot.format_sources_card()
        self.assertIn("/resume", text)
        self.assertFalse(any(b.get("callback_data") in {"menu:pause", "menu:resume"} for row in markup["inline_keyboard"] for b in row))

    def test_registered_commands_only_offer_settings_and_queries(self):
        self.assertEqual([x["command"] for x in main.BOT_COMMANDS], ["status", "signin", "report", "records", "settings"])
        for cmd, title in (("/settings", "设置"), ("/records", "参与记录"), ("/start", "运行状态")):
            self.command(cmd)
            self.assertIn(title, self.sent())

    def test_corrupt_ledger_is_reported_not_presented_as_empty(self):
        path = Path(self.bot.participant.ledger.path)
        path.write_text("{broken")
        text, _ = self.bot.format_records_card()
        self.assertIn("读取失败", text)
        self.assertNotIn("共 0 项", text)
        self.assertEqual(path.read_text(), "{broken")

    def test_long_record_data_is_literal_bounded_and_callbacks_are_short(self):
        row = dict(kind="redpacket", status="uncertain", cost=0, day="2026-10-15", at=self.now.timestamp(),
                   title="<标题>&" * 1500, topic_body="<正文>&🍪" * 4000, attempted_reply="<拟发>&🍪" * 3000)
        self.bot.participant.ledger.save(dict(version=1, entries={"1": row}))
        text, markup = self.bot.format_record_item("1")
        self.assertLess(units("".join(parsed(text).text)), 4096)
        self.assertIn("拟提交回复", text)
        self.assertNotIn("已核实回复", text)
        for group in markup['inline_keyboard']:
            for b in group:
                if 'callback_data' in b:
                    self.assertLessEqual(len(b['callback_data'].encode()), 64)

    def test_durable_submission_in_progress_is_not_reported_as_an_exception(self):
        self.bot.participant.ledger.save(dict(version=1, entries={"1":dict(kind="redpacket",status="reserved",cost=0,day="2026-10-15",at=self.now.timestamp(),title="提交中")}))
        before = Path(self.bot.participant.ledger.path).read_bytes()
        text = self.bot.generate_daily_report_card()
        self.assertIn("提交异常：0 项", text)
        self.assertIn("提交中：1 项", text)
        detail, _ = self.bot.format_record_item("1")
        self.assertIn("状态：提交中", detail)
        self.assertIn("保留防重", detail)
        self.assertEqual(Path(self.bot.participant.ledger.path).read_bytes(), before)

    def test_record_tokens_expire_and_do_not_query_or_mutate_any_activity(self):
        self.bot.participant.ledger.save(dict(version=1, entries={"1":dict(kind="redpacket",status="skipped",cost=0,day="2026-10-15",at=self.now.timestamp(),title="示例")}))
        _, markup = self.bot.format_records_card()
        token = markup['inline_keyboard'][0][0]['callback_data']
        self.bot.ui_actions.clear()
        self.callback(token)
        self.assertIn("过期", self.bot.answer_callback_query.call_args.args[1])
        with self.assertRaises(ValueError):
            self.bot.format_record_item("../settings")


if __name__ == "__main__":
    unittest.main()
