"""Telegram panels and conversations. Ledger detail views are strictly read-only."""
import math
import re
import secrets
import time
from datetime import datetime, timezone, timedelta

try:
    from app.telegram_text import escape as e, clip, callback as cb, keyboard as kb, url_button, split_html
except ModuleNotFoundError:
    from telegram_text import escape as e, clip, callback as cb, keyboard as kb, url_button, split_html

CST = timezone(timedelta(hours=8))
PAGE_SIZE = 8
REASONS = {
    "manual_requirements": "原帖有附加条件或参与要求不明确",
    "final_review_denied": "第二轮终审未通过，已跳过",
    "final_review_unclear": "第二轮终审仍不明确，已跳过",
    "final_review_unavailable": "第二轮终审调用失败，已跳过",
    "unsafe_content": "原帖未通过内容安全或有效性检查",
    "reply_rejected": "生成回复未通过检查",
    "verification_required": "页面要求额外验证",
    "topic_changed": "提交前帖子内容或参与条件已变化",
    "entry_budget": "超过单次金额上限",
    "daily_budget": "今日可预留金额不足",
    "already": "论坛查询确认账号已参与",
    "ended": "活动已结束",
    "post_unconfirmed": "提交后未取得最终确认，不重复提交",
    "post_redirect_unconfirmed": "提交后返回非预期跳转，未确认是否成功",
    "post_rejected": "论坛未返回允许的提交状态",
    "page_unavailable": "主题页面暂不可用",
    "guard_unavailable": "内容筛查暂不可用",
    "guard_not_configured": "内容筛查未配置",
    "reply_unavailable": "回复生成服务暂不可用",
    "history_requires_review": "历史参与记录无法完整核验，已停止自动提交",
    "participation_error": "处理异常，具体原因未确认",
    "login_required": "论坛登录已失效",
}
PAUSE_TEXT = ("⏸️ <b>已暂停推送与参与</b>\n\n"
              "暂停：活动、私信和互动提醒，红包 / 抽奖自动参与。\n"
              "签到、签到结果通知与定时日报照常执行。\n"
              "后台继续去重；恢复后不补发暂停期间的通知。")


def stamp(value):
    try:
        if not value or not math.isfinite(float(value)) or float(value) <= 0:
            return "暂未获取"
        return datetime.fromtimestamp(float(value), CST).strftime("%m月%d日 %H:%M")
    except (ValueError, TypeError, OSError, OverflowError):
        return "暂未获取"


def page_slice(items, page):
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(max(0, int(page)), pages - 1)
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, pages


def format_activity(source, kind, title, author, category, description, link, reason="", demo=False):
    label = {"lottery": "抽奖", "welfare": "福利", "redpacket": "红包", "custom": "关注词命中"}.get(kind, "活动")
    icon = {"lottery": "🎁", "welfare": "🎉", "redpacket": "🧧", "custom": "🎯"}.get(kind, "🎁")
    description = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', description or '', flags=re.I | re.S)
    description = re.sub(r'</?(?:p|div|br|a|img|strong|b|em|i|ul|ol|li|span)\b[^>]*>', ' ', description, flags=re.I)
    description = re.sub(r'\s+', ' ', description).strip()
    text = (f"{icon} <b>{e(source, 40)} · {label}{'（演示）' if demo else ''}</b>\n\n"
            f"<b>{e(title, 300)}</b>\n{e(author, 80)} · {e(category, 60)}\n\n{e(description, 300)}")
    if kind == "custom" and reason:
        text += "\n" + e(reason, 200)
    return text, kb([url_button("查看网站" if demo else "查看原帖", link)])


class TelegramUI:
    def ui_token(self, action, payload):
        with self.lock:
            if not hasattr(self, "ui_actions"):
                self.ui_actions = {}
            now = time.time()
            self.ui_actions = {k: v for k, v in self.ui_actions.items() if now - v[0] < 21600}
            while len(self.ui_actions) >= 1024:
                self.ui_actions.pop(next(iter(self.ui_actions)))
            token = secrets.token_hex(8)
            self.ui_actions[token] = (now, action, payload)
            return "ui:" + token

    def stats_snapshot(self):
        with self.lock:
            stats = dict(self.daily_stats)
            today = self.get_today_cst()
            if stats.get("date") != today:
                stats = {key: 0 for key in stats if key != "date"}
            stats["date"] = today
            return stats

    def participation_snapshot(self, day=None):
        return self.participant.ledger.daily_report(day or self.get_today_cst())

    def ledger_rows(self):
        return self.participant.ledger.load()["entries"]

    def _participation_label(self):
        if not getattr(self.participant, "enabled", False):
            return "服务未启用"
        if self.paused:
            return "暂停"
        if not self.sbsb_participation_enabled:
            return "关"
        planner = getattr(self.participant, "planner", None)
        if hasattr(planner, "key") and not planner.key:
            return "安全筛查未配置，当前无法自动参与"
        return "开"

    def format_status_card(self):
        push = lambda sid: "暂停" if self.paused else ("开" if self.source_states.get(sid, True) else "关")
        lines = ["📊 <b>运行状态</b>", "", f"NodeSeek 活动推送：{push('nodeseek')}",
                 f"烧饼活动推送：{push('sbsb')} · 自动参与：{e(self._participation_label())}",
                 f"本轮签到：{e(self.signin_status())}"]
        return "\n".join(lines), kb([cb("签到与资产", "signin:query"), cb("今日汇总", "menu:report")],
                                    [cb("参与记录", "menu:records"), cb("设置", "menu:settings")])

    def report_markup(self):
        return kb([cb("参与记录", "menu:records"), cb("刷新汇总", "menu:report")],
                  [cb("返回首页", "menu:status")])

    def generate_daily_report_card(self):
        stats = self.stats_snapshot()
        lines = ["📈 <b>今日汇总</b>", f"{stats['date']} 截至 {self.local_now().strftime('%H:%M')} · 北京时间", "",
                 f"发现活动 {stats.get('lottery_hits', 0)} 条 · 关注词命中 {stats.get('custom_hits', 0)} 条"]
        success, errors = stats.get("delivery_success", 0), stats.get("delivery_errors", 0)
        lines.append(f"活动通知：发送成功 {success} 条 · 失败 {errors} 条" if success + errors else "活动通知：暂无发送记录")
        try:
            report = self.participation_snapshot(stats["date"])
            totals = {k: sum(c[k] for c in report["groups"].values()) for k in
                      ("submitted", "account_joined", "unconfirmed", "skipped", "retry", "review")}
            lines += ["", f"自动参与成功：{totals['submitted']} 项", f"账号查询确认已参与：{totals['account_joined']} 项",
                      f"已跳过：{totals['skipped']} 项", f"提交异常：{totals['unconfirmed'] - report['submitting']} 项",
                      f"当前排队：{report['waiting']} 项"]
            if report['submitting']:
                lines.append(f"提交中：{report['submitting']} 项")
            if totals['retry']:
                lines.append(f"待重试：{totals['retry']} 项")
            if totals['review']:
                lines.append(f"旧策略记录：{totals['review']} 项（仅查询）")
            lines += [f"抽奖预算预留：{report['reserved_cost']} / {self.participant.daily} 饼",
                      f"单次上限：{self.participant.per_entry} 饼"]
        except Exception:
            lines.append("参与账本读取异常，统计暂不可用")
        if "筛查未配置" in self._participation_label():
            lines.append("安全筛查未配置，当前无法自动参与")
        lines += ["", "<i>查询确认不归因为程序提交；预算预留不等于实扣。通知成功仅指发送接口接受。</i>"]
        return "\n".join(lines)

    def format_report_detail(self):
        return self.generate_daily_report_card(), self.report_markup()

    def format_participation_card(self):
        return self.format_records_card()

    def format_budget_card(self):
        return self.generate_daily_report_card(), self.report_markup()

    def format_review_index(self):
        return self.format_records_card()

    def format_records_card(self, page=0):
        """Read every ledger outcome, including historical confirmed and skipped rows."""
        try:
            items = sorted(self.ledger_rows().items(), key=lambda item: item[1]["at"], reverse=True)
        except Exception:
            return "⚠️ <b>参与记录</b>\n\n账本读取失败，暂时无法查看。", kb([cb("返回首页", "menu:status")])
        selected, page, pages = page_slice(items, page)
        lines = [f"📝 <b>参与记录 · {page + 1} / {pages}</b>", "", f"共 {len(items)} 项，包含已参与、已跳过和提交异常的活动。"]
        buttons = []
        for index, (key, row) in enumerate(selected, page * PAGE_SIZE + 1):
            kind = {"redpacket": "红包", "lottery": "抽奖"}.get(row.get("kind"), "活动")
            status = self._row_status(row, row.get("kind"))
            lines += ["", f"{index}. {e(row.get('title', '标题未记录'), 90)}",
                      f"记录：{stamp(row.get('queued_at') or row.get('at'))} · {kind} · {e(status)}"]
            buttons.append([cb(f"{kind} · {clip(row.get('title', '标题未记录'), 18)} · {status}", self.ui_token("record_item", (key, page)))])
        if not selected:
            lines.append("\n暂无参与记录。")
        navigation = []
        if page:
            navigation.append(cb("上一页", f"records:{page - 1}"))
        if page + 1 < pages:
            navigation.append(cb("下一页", f"records:{page + 1}"))
        return "\n".join(lines), kb(*buttons, navigation, [cb("返回首页", "menu:status")])

    def format_record_item(self, key, page=0):
        """Show recorded evidence only; timestamps are never inferred from submission attempts."""
        if not re.fullmatch(r"[1-9]\d*", key):
            raise ValueError("expired")
        row = self.ledger_rows().get(key)
        if row is None:
            return "此记录已不存在，请返回列表刷新。", kb([cb("返回列表", f"records:{page}")])
        status = row.get("status")
        lines = ["📝 <b>活动详情</b>", "", f"帖子：{e(row.get('title', '未记录'), 300)}",
                 f"状态：{e(self._row_status(row, row.get('kind')))}",
                 f"发布时间：{stamp(row.get('published_at'))}"]
        if status in {"reserved", "uncertain", "confirmed"}:
            lines.append(f"尝试提交时间：{stamp(row.get('attempted_at') or row.get('at'))}")
        lines += [f"回复时间：{stamp(row.get('reply_at'))}", "", "<b>帖子内容</b>",
                  e(row.get("topic_body") or "未记录，可通过原帖查看。", 2000)]
        if row.get("reply"):
            label = "已核实回复" if row.get("submission_accepted") is True or row.get("reply_source") == "official_get" else "已保存回复（未单独核实）"
            lines += ["", f"<b>{label}</b>", e(row["reply"], 1000)]
        elif row.get("attempted_reply"):
            lines += ["", "<b>拟提交回复</b>", e(row["attempted_reply"], 1000)]
        else:
            lines += ["", "回复内容：未记录"]
        if status == "reserved":
            reason = "已保存提交保护记录，尚未记录最终响应；保留防重，只查询确认，不重复提交。"
        elif status == "uncertain":
            reason = "提交阶段未取得最终确认，未确认是否成功；保留防重记录，不重复提交。"
        elif status == "confirmed":
            reason = "本程序提交，论坛已确认。" if row.get("submission_accepted") is True else "账号查询确认已参与，不能据此认定由本程序提交。"
        elif status == "already":
            reason = "论坛查询确认账号已参与，没有再次提交。"
        else:
            reason = REASONS.get(row.get("reason"), "具体原因未记录")
        lines += ["", "<b>处理结果</b>", reason]
        return "\n".join(lines), kb([url_button("查看原帖", f"https://sb.sb/t/{key}/"), cb("返回列表", f"records:{page}")])

    def format_review_list(self, kind, page=0):
        if kind not in {"ai", "redpacket", "lottery", "history"}:
            raise ValueError("invalid_list")
        return self.format_records_card(page)

    @staticmethod
    def _row_status(row, kind):
        status = row.get("status")
        if status == "confirmed":
            return "自动参与成功" if row.get("submission_accepted") is True else "已参与（查询确认）"
        return {"already": "已参与（查询确认）", "reserved": "提交中", "uncertain": "提交异常",
                "review": "旧策略待定记录", "retry": "等待重试", "waiting": "随机等待中", "skipped": "已跳过"}.get(status, "状态暂不可用")

    def format_review_item(self, kind, key, page):
        return self.format_records_card(page) if kind == "ai" else self.format_record_item(key, page)

    def format_sources_card(self):
        lines = ["⚙️ <b>设置</b>", "", "点击当前状态即可开关。", "烧饼通知关闭不影响自动参与。"]
        buttons = []
        for source in self.sources:
            sid, name = source["id"], source["name"]
            on = self.source_states.get(sid, True)
            buttons.append([cb(f"{name}活动推送：{'开' if on else '关'}", f"source:{sid}:{0 if on else 1}")])
        buttons += [[cb(f"烧饼自动参与：{e(self._participation_label())}", f"participation:{0 if self.sbsb_participation_enabled else 1}")],
                    [cb(f"关注词 · {len(self.keywords)} 个", "menu:keywords")], [cb("返回首页", "menu:status")]]
        if self.paused:
            lines.append("\n旧版总暂停仍生效，可发送 /resume 恢复独立开关。签到与日报照常执行。")
        return "\n".join(lines), kb(*buttons)

    def format_keywords_card(self, page=0, delete=False):
        with self.lock:
            words = list(self.keywords)
        selected, page, pages = page_slice(words, page)
        lines = [f"🎯 <b>关注词 · {page + 1} / {pages}</b>" if words else "🎯 <b>关注词</b>", ""]
        buttons = []
        for index, word in enumerate(selected, page * PAGE_SIZE + 1):
            lines.append(f"{index}. {e(word, 120)}")
            action = "kw_delete" if delete else "kw_view"
            buttons.append([cb(("删除 " if delete else "查看 ") + clip(word, 18), self.ui_token(action, (word, page)))])
        if words:
            lines += ["", f"共 {len(words)} 个，每页 {PAGE_SIZE} 个。"]
        else:
            lines += ["当前未设置关注词。", "添加后，标题或正文包含该词的帖子可触发活动推送。"]
        navigation = []
        prefix = "kwdel" if delete else "kw"
        if page:
            navigation.append(cb("上一页", f"page:{prefix}:{page - 1}"))
        if page + 1 < pages:
            navigation.append(cb("下一页", f"page:{prefix}:{page + 1}"))
        return "\n".join(lines), kb(*buttons, navigation,
            [cb("添加", "menu:add_kw"), cb("查看列表" if delete else "点选删除", "menu:keywords" if delete else "menu:del_kw")],
            [cb("返回设置", "menu:settings")])

    def format_help(self, details=False):
        return ("🤖 <b>使用帮助</b>\n\n/status 运行状态\n/signin 查询签到与资产\n/report 今日汇总\n"
                "/records 参与记录\n/settings 设置\n\n在设置里直接点击状态开关；关注词可添加或删除。"), kb([cb("返回首页", "menu:status")])

    def panel(self, name):
        methods = {"status": self.format_status_card, "sources": self.format_sources_card,
                   "settings": self.format_sources_card, "keywords": self.format_keywords_card,
                   "records": self.format_records_card, "participation": self.format_records_card,
                   "review": self.format_records_card, "budget": lambda: (self.generate_daily_report_card(), self.report_markup()),
                   "report_detail": lambda: (self.generate_daily_report_card(), self.report_markup()),
                   "help": self.format_help, "help_detail": self.format_help,
                   "report": lambda: (self.generate_daily_report_card(), self.report_markup())}
        return methods[name]()

    def show_panel(self, chat_id, message_id, card):
        text, markup = card
        if message_id is None or len(split_html(text)) > 1 or not self.edit_msg_text(chat_id, message_id, text, reply_markup=markup):
            self.send_msg(chat_id, text, reply_markup=markup)

    def set_paused(self, paused):
        with self.lock:
            previous = self.paused
            self.paused = paused
            try:
                self.save_settings()
            except Exception:
                self.paused = previous
                raise

    def locked_reply(self, chat_id):
        self.send_msg(chat_id, "模型和连接凭据由服务器配置，TG不再修改。", reply_markup=kb([cb("返回设置", "menu:settings")]))

    def handle_callback(self, query):
        query_id = query.get("id")
        if str(query.get("from", {}).get("id", "")) != self.admin_chat_id:
            self.answer_callback_query(query_id, "无权操作此机器人")
            return
        message = query.get("message", {})
        chat_id, message_id = message.get("chat", {}).get("id"), message.get("message_id")
        data = query.get("data", "")
        ack = "已更新"
        try:
            card = None
            if data.startswith(("menu:", "page:", "list:", "ai:", "source:", "participation:")):
                self.user_states.pop(chat_id, None)
            if data.startswith("ai:") or data == "menu:ai":
                ack, card = "模型和凭据仅在服务器配置", self.format_sources_card()
            elif data in {"menu:pause", "menu:resume"}:
                ack, card = "请直接点击各业务状态开关", self.format_sources_card()
            elif data.startswith("menu:") and data[5:] in {"status", "sources", "settings", "records", "keywords", "participation", "budget", "review", "report_detail", "help", "help_detail", "report"}:
                card = self.panel(data[5:])
            elif data.startswith("records:"):
                self.user_states.pop(chat_id, None)
                _, page = data.split(":")
                card = self.format_records_card(int(page))
            elif data.startswith("source:"):
                _, sid, desired = data.split(":")
                if sid not in {s["id"] for s in self.sources} or desired not in {"0", "1"}:
                    raise ValueError("expired")
                on = desired == "1"
                with self.lock:
                    if self.source_states.get(sid, True) != on:
                        self.toggle_source(sid)
                ack = f"活动推送已{'开启' if on else '关闭'}" + ("，恢复后生效" if self.paused else "")
                card = self.format_sources_card()
            elif data in {"participation:0", "participation:1"}:
                if not self.participant.enabled:
                    self.answer_callback_query(query_id, "自动参与服务尚未启用")
                    return
                on = data.endswith(":1")
                with self.lock:
                    if self.sbsb_participation_enabled != on:
                        self.toggle_sbsb_participation()
                ack = f"自动参与已{'开启' if on else '关闭'}" + ("，恢复后生效" if self.paused else "")
                card = self.format_sources_card()
            elif data in {"menu:add_kw", "menu:del_kw"}:
                if data.endswith("del_kw"):
                    card = self.format_keywords_card(delete=True)
                else:
                    self.user_states[chat_id] = "waiting_for_add"
                    card = ("➕ <b>添加关注词</b>\n\n一次可输入多个词，用空格分隔。\n例如：<code>DMIT 搬瓦工</code>\n发送 /cancel 取消。", kb([cb("取消", "input:cancel")]))
            elif data == "input:cancel":
                self.user_states.pop(chat_id, None)
                card = ("已取消本次输入，配置未更改。", kb([cb("返回设置", "menu:settings")]))
                ack = "已取消"
            elif data.startswith("page:"):
                _, mode, page = data.split(":")
                if mode not in {"kw", "kwdel"}:
                    raise ValueError("expired")
                card = self.format_keywords_card(int(page), mode == "kwdel")
            elif data.startswith("list:"):
                _, kind, page = data.split(":")
                card = self.format_review_list(kind, int(page))
            elif data.startswith("ui:"):
                item = getattr(self, "ui_actions", {}).get(data[3:])
                if not item or time.time() - item[0] >= 21600:
                    raise ValueError("expired")
                _, action, payload = item
                if action in {"kw_view", "kw_delete"}:
                    word, page = payload
                    with self.lock:
                        if word not in self.keywords:
                            raise ValueError("expired")
                        if action == "kw_delete":
                            previous = self.keywords[:]
                            self.keywords.remove(word)
                            try:
                                self.save_settings()
                            except Exception:
                                self.keywords[:] = previous
                                raise
                    if action == "kw_delete":
                        ack, card = "已删除关注词", self.format_keywords_card(page, True)
                    else:
                        card = ("🎯 <b>关注词详情</b>\n\n" + e(word), kb([cb("返回列表", f"page:kw:{page}")]))
                elif action == "record_item":
                    card = self.format_record_item(*payload)
                elif action == "review_item":
                    card = self.format_records_card(payload[2])
                else:
                    raise ValueError("expired")
            elif data == "signin:query":
                self.answer_callback_query(query_id, "正在只读查询签到与资产")
                self.query_signin(chat_id, allow_submit=False)
                return
            else:
                raise ValueError("expired")
            self.answer_callback_query(query_id, ack)
            if card:
                self.show_panel(chat_id, message_id, card)
        except (ValueError, KeyError, TypeError):
            self.answer_callback_query(query_id, "按钮已过期，请重新打开对应面板")
        except Exception:
            self.answer_callback_query(query_id, "操作未完成，请重新查询当前状态")

    def add_keywords(self, text, legacy_words):
        added, duplicate, rejected = [], [], []
        with self.lock:
            previous = self.keywords[:]
            for word in dict.fromkeys(text.split()):
                if word in legacy_words:
                    rejected.append(word)
                elif word in self.keywords:
                    duplicate.append(word)
                else:
                    self.keywords.append(word)
                    added.append(word)
            if added:
                try:
                    self.save_settings()
                except Exception:
                    self.keywords[:] = previous
                    raise
        lines = ["🎯 <b>关注词处理结果</b>" if added else "未添加新关注词。", ""]
        for label, words in (("已添加", added), ("已存在", duplicate), ("未添加（当前规则不接受的内置词）", rejected)):
            if words:
                lines.append(label + "：" + "、".join(e(word) for word in words))
        if not (added or duplicate or rejected):
            lines.append("请输入至少一个关注词。")
        return "\n".join(lines), kb([cb("查看关注词", "menu:keywords")])

    def consume_input(self, chat_id, text, message_id, normalizer, legacy_words):
        state = self.user_states[chat_id]
        if state == "waiting_for_add":
            self.show_panel(chat_id, None, self.add_keywords(text, legacy_words))
            if text.split():
                self.user_states.pop(chat_id, None)
            return
        self.user_states.pop(chat_id, None)
        if state == "waiting_for_ai_key":
            deleted = self.delete_msg(chat_id, message_id)
            note = "原消息已删除。" if deleted else "原消息删除失败，请手动删除包含密钥的消息。"
            self.send_msg(chat_id, "AI配置未更改；模型和凭据只在服务器配置。\n" + note,
                          reply_markup=kb([cb("返回设置", "menu:settings")]))
        else:
            self.locked_reply(chat_id)

    def handle_command(self, chat_id, text, message_id, normalizer, legacy_words):
        text = text.strip()
        if not text:
            return
        parts = text.split(maxsplit=1)
        command = parts[0].lower().split("@", 1)[0]
        argument = parts[1] if len(parts) > 1 else ""
        if command == "/cancel":
            existed = self.user_states.pop(chat_id, None)
            self.send_msg(chat_id, "已取消本次输入，配置未更改。" if existed else "当前没有待取消的输入。",
                          reply_markup=kb([cb("返回设置", "menu:settings")]))
            return
        if chat_id in self.user_states and not text.startswith("/"):
            self.consume_input(chat_id, text, message_id, normalizer, legacy_words)
            return
        self.user_states.pop(chat_id, None)
        panels = {"/start": "status", "/help": "help", "/status": "status", "/stats": "status", "/report": "report",
                  "/settings": "settings", "/sources": "settings", "/ai": "settings", "/records": "records",
                  "/keywords": "keywords", "/list": "keywords"}
        if command in panels:
            self.show_panel(chat_id, None, self.panel(panels[command]))
        elif command in {"/signin", "/checkin"}:
            self.send_msg(chat_id, "⏳ 正在查询签到与资产。")
            self.query_signin(chat_id, allow_submit=False)
        elif command == "/add":
            if argument:
                self.show_panel(chat_id, None, self.add_keywords(argument, legacy_words))
            else:
                self.user_states[chat_id] = "waiting_for_add"
                self.send_msg(chat_id, "➕ 请发送关注词，多个词用空格分隔；发送 /cancel 取消。", reply_markup=kb([cb("取消", "input:cancel")]))
        elif command == "/del":
            self.show_panel(chat_id, None, self.format_keywords_card(delete=True))
        elif command == "/pause":
            self.set_paused(True)
            self.send_msg(chat_id, PAUSE_TEXT, reply_markup=self.format_sources_card()[1])
        elif command == "/resume":
            self.set_paused(False)
            self.send_msg(chat_id, "▶️ <b>已恢复推送与参与</b>\n\n按各站独立开关执行，不补发暂停期间的通知。",
                          reply_markup=kb([cb("查看状态", "menu:status")]))
        elif command == "/test":
            self.show_panel(chat_id, None, self.format_status_card())
        else:
            self.send_msg(chat_id, "暂未识别这条指令。发送 /help 查看可用功能。", reply_markup=kb([cb("使用帮助", "menu:help")]))
