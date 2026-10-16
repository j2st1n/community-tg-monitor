"""Telegram panels and conversations. Ledger detail views are strictly read-only."""
import math
import re
import secrets
import time
from datetime import datetime, timezone, timedelta

try:
    from app.telegram_text import escape as e, plain, clip, callback as cb, keyboard as kb, url_button, split_html
except ModuleNotFoundError:
    from telegram_text import escape as e, plain, clip, callback as cb, keyboard as kb, url_button, split_html

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
    "post_unconfirmed": "提交结果未确认",
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
AI_FIELDS = {
    "endpoint": ("API 地址", "env_ai_endpoint", "waiting_for_ai_endpoint"),
    "key": ("API Key", "env_ai_api_key", "waiting_for_ai_key"),
    "model": ("主模型", "env_ai_model", "waiting_for_ai_model"),
    "judge": ("复审模型", "env_ai_judge_model", "waiting_for_ai_judge"),
    "threshold": ("自动通过阈值", "env_ai_accept_threshold", "waiting_for_ai_threshold"),
}


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

    def _ai_label(self):
        configured = bool(self.ai_endpoint and self.ai_model and self.ai_api_key)
        return "已启用" if self.ai_is_ready() else ("已配置，未启用" if configured else "配置未完成")

    def format_status_card(self):
        lines = ["⏸️ <b>推送与参与已暂停</b>" if self.paused else "📊 <b>运行状态</b>", ""]
        push = lambda source: "暂停" if self.paused else ("开" if self.source_states.get(source, True) else "关")
        lines += [f"NodeSeek 活动推送：{push('nodeseek')}",
                  f"烧饼活动推送：{push('sbsb')} · 自动参与：{e(self._participation_label())}",
                  f"本轮签到：{e(self.signin_status())}", f"AI 判定：{self._ai_label()}", ""]
        try:
            report = self.participation_snapshot()
            waiting = report["waiting"]
            manual = sum(v["review"] for v in report["groups"].values())
            lines += [f"当前排队：AI {len(self.nodeseek_ai_pending)} 条 · 活动 {waiting} 项",
                      f"今日待处理：活动 {manual} 项 · AI 待复核总计 {len(self.nodeseek_ai_review)} 条"]
        except Exception:
            lines.append("参与账本读取异常，统计暂不可用")
        if self.paused:
            lines.append("\n签到、签到通知与定时日报按原设置执行。")
        return "\n".join(lines), kb(
            [cb("今日汇总", "menu:report"), cb("参与详情", "menu:participation")],
            [cb("推送与参与", "menu:sources"), cb("AI 设置", "menu:ai")],
            [cb("关注词", "menu:keywords"), cb("恢复推送与参与" if self.paused else "暂停推送与参与",
                                                "menu:resume" if self.paused else "menu:pause")])

    def report_markup(self):
        return kb([cb("参与详情", "menu:participation"), cb("统计详情", "menu:report_detail")],
                  [cb("待处理项目", "menu:review"), cb("返回状态", "menu:status")])

    def generate_daily_report_card(self):
        stats = self.stats_snapshot()
        lines = ["📈 <b>今日汇总</b>", f"{stats['date']} 截至 {self.local_now().strftime('%H:%M')} · 北京时间", "",
                 f"发现活动 {stats.get('lottery_hits', 0)} 条 · 关注词命中 {stats.get('custom_hits', 0)} 条"]
        success, errors = stats.get("delivery_success", 0), stats.get("delivery_errors", 0)
        lines.append(f"活动通知：发送成功 {success} 条 · 失败 {errors} 条" if success + errors else "活动通知：暂无发送记录")
        try:
            report = self.participation_snapshot(stats["date"])
            red, lottery = report["groups"]["redpacket"], report["groups"]["lottery"]
            lines += [f"烧饼抽奖：提交确认 {lottery['submitted']} 项 · 查询确认 {lottery['account_joined']} 项",
                      f"烧饼红包：提交确认 {red['submitted']} 项 · 查询确认 {red['account_joined']} 项",
                      f"今日待处理：{red['review'] + lottery['review']} 项 · 结果未确认 {red['unconfirmed'] + lottery['unconfirmed']} 项",
                      f"当前排队：{report['waiting']} 项 · AI 待复核总计 {len(self.nodeseek_ai_review)} 条",
                      f"抽奖预算预留：{report['reserved_cost']} / {self.participant.daily} 饼"]
        except Exception:
            lines.append("参与账本读取异常，统计暂不可用")
        if "筛查未配置" in self._participation_label():
            lines.append("⚠️ 安全筛查未配置，当前无法自动参与")
        lines += ["", "<i>查询确认不归因为程序提交；预算预留不等于实际扣费。</i>"]
        return "\n".join(lines)

    def format_report_detail(self):
        s = self.stats_snapshot()
        lines = ["📋 <b>统计详情</b>", f"{s['date']} 截至 {self.local_now().strftime('%H:%M')} · 北京时间", "",
                 f"扫描主题：{s.get('total_scanned', 0)} 篇",
                 f"发现活动：{s.get('lottery_hits', 0)} 条 · 关注词命中：{s.get('custom_hits', 0)} 条",
                 f"烧饼官方标记：红包 {s.get('sbsb_redpacket_events', 0)} 项 · 抽奖 {s.get('sbsb_lottery_events', 0)} 项",
                 f"活动通知发送：成功 {s.get('delivery_success', 0)} 条 · 失败 {s.get('delivery_errors', 0)} 条",
                 f"私信与互动提醒：{s.get('private_notified', 0)} 次", "",
                 f"采集请求：成功 {s.get('poll_success', 0)} 次 · 异常 {s.get('poll_errors', 0)} 次",
                 f"烧饼标记查询：成功 {s.get('marker_poll_success', 0)} 次 · 异常 {s.get('marker_poll_errors', 0)} 次",
                 f"AI 请求：{s.get('ai_requests', 0)} 次 · 异常 {s.get('ai_errors', 0)} 次",
                 f"AI 已完成判定：{s.get('ai_classified', 0)} 篇", "",
                 "<i>活动通知仅含公开活动卡片；成功指 Telegram 接口接受，不代表收件端已读。</i>"]
        return "\n".join(lines), kb([cb("返回汇总", "menu:report")])

    def format_participation_card(self):
        day = self.get_today_cst()
        lines = ["🎁 <b>烧饼参与详情</b>", f"{day} 截至 {self.local_now().strftime('%H:%M')} · 北京时间", "",
                 f"自动参与：{e(self._participation_label())}"]
        try:
            report = self.participation_snapshot(day)
            for kind, title in (("lottery", "普通抽奖"), ("redpacket", "红包")):
                c = report["groups"][kind]
                lines += ["", f"<b>{title}</b>", f"程序提交获确认：{c['submitted']} 项",
                          f"账号查询确认已参与：{c['account_joined']} 项", f"今日结果未确认：{c['unconfirmed']} 项",
                          f"今日待处理：{c['review']} 项 · 待重试：{c['retry']} 项 · 跳过：{c['skipped']} 项"]
            lines += ["", f"当前随机等待：{report['waiting']} 项",
                      f"抽奖预算预留：{report['reserved_cost']} / {self.participant.daily} 饼"]
        except Exception:
            lines.append("\n参与账本读取异常，统计暂不可用")
        lines += ["", "<i>查询确认不归因为程序提交；参与不代表中奖或到账。</i>"]
        lines.append("资格初筛待定时只做一次 AI 终审；仍不明确或调用失败就跳过，不转人工。")
        return "\n".join(lines), kb([cb("待处理项目", "menu:review"), cb("历史记录", "list:history:0")],
                                      [cb("预算说明", "menu:budget"), cb("返回汇总", "menu:report")])

    def format_budget_card(self):
        try:
            used = self.participation_snapshot()["reserved_cost"]
            usage = f"今日已预留：{used} 饼\n今日剩余可预留：{max(0, self.participant.daily - used)} 饼"
        except Exception:
            usage = "今日预算记录：暂不可用"
        return ("🍪 <b>抽奖预算</b>\n\n"
                f"单次最多：{self.participant.per_entry} 饼\n每天最多：{self.participant.daily} 饼\n{usage}\n"
                "参与次数：不设上限\n\n<i>按本地账本保守预留；实际扣费以论坛账单为准。</i>"), kb([cb("返回参与详情", "menu:participation")])

    def format_review_index(self):
        today = self.get_today_cst()
        try:
            rows = self.ledger_rows()
            red = sum(r.get("day") == today and r.get("kind") == "redpacket" and r["status"] == "review" for r in rows.values())
            lottery = sum(r.get("day") == today and r.get("kind") == "lottery" and r["status"] == "review" for r in rows.values())
            old = sum(r.get("day", today) < today and r["status"] in {"review", "reserved", "uncertain", "retry"} for r in rows.values())
            activity = f"今日红包：{red} 项\n今日抽奖：{lottery} 项\n历史活动待处理 / 未确认：{old} 项"
        except Exception:
            activity = "活动账本：暂不可用"
        ai_old, ai_new, ai_unknown = 0, 0, 0
        for row in self.nodeseek_ai_review.values():
            value = row.get("quarantined_at") or row.get("queued_at")
            if stamp(value) == "暂未获取":
                ai_unknown += 1
                continue
            day = datetime.fromtimestamp(float(value), CST).strftime("%Y-%m-%d")
            ai_old += day < today
            ai_new += day == today
            ai_unknown += day > today
        return ("📝 <b>待处理项目</b>\n\n" + activity +
                f"\nAI 今日新增复核：{ai_new} 条 · 历史存量：{ai_old} 条" +
                (f" · 日期未确认：{ai_unknown} 条" if ai_unknown else "") + "\n\n"
                "选择分类查看原帖、时间和已记录原因。"), kb(
                    [cb("今日红包", "list:redpacket:0"), cb("今日抽奖", "list:lottery:0")],
                    [cb("AI 复核", "list:ai:0"), cb("历史活动", "list:history:0")],
                    [cb("返回汇总", "menu:report")])

    def _review_items(self, kind):
        today = self.get_today_cst()
        if kind == "ai":
            return sorted(dict(self.nodeseek_ai_review).items(), key=lambda x: x[1].get("quarantined_at") or x[1].get("queued_at") or 0, reverse=True)
        rows = self.ledger_rows()
        return sorted(((key, row) for key, row in rows.items() if (
            row.get("day", today) < today and row["status"] in {"review", "reserved", "uncertain", "retry"}
            if kind == "history" else row.get("day") == today and row.get("kind") == kind
            and row["status"] in {"review", "reserved", "uncertain", "retry"})),
            key=lambda x: x[1]["at"], reverse=True)

    def format_review_list(self, kind, page=0):
        if kind not in {"ai", "redpacket", "lottery", "history"}:
            raise ValueError("invalid_list")
        title = {"ai": "AI 待复核", "redpacket": "今日红包待处理", "lottery": "今日抽奖待处理", "history": "历史活动待处理 / 未确认"}[kind]
        try:
            items = self._review_items(kind)
        except Exception:
            return f"⚠️ <b>{title}</b>\n\n记录读取失败，暂时无法查看。", kb([cb("返回分类", "menu:review")])
        selected, page, pages = page_slice(items, page)
        lines = [f"📝 <b>{title} · {page + 1} / {pages}</b>", ""]
        buttons = []
        for index, (key, row) in enumerate(selected, page * PAGE_SIZE + 1):
            lines += [f"{index}. {e(row.get('title', '标题暂未获取'), 90)}",
                      f"{stamp(row.get('queued_at') or row.get('at'))} · {e(self._row_status(row, kind))}"]
            buttons.append([cb(f"查看 {index}", self.ui_token("review_item", (kind, key, page)))])
        if not selected:
            lines.append("当前没有此类记录。")
        lines += ["", "<i>仅查询记录，不会重新参与或重试。</i>"]
        navigation = []
        if page:
            navigation.append(cb("上一页", f"list:{kind}:{page - 1}"))
        if page + 1 < pages:
            navigation.append(cb("下一页", f"list:{kind}:{page + 1}"))
        return "\n".join(lines), kb(*buttons, navigation, [cb("返回分类", "menu:review")])

    @staticmethod
    def _row_status(row, kind):
        if kind == "ai":
            return "等待人工复核"
        status = row.get("status")
        if status == "confirmed":
            return "程序提交已确认" if row.get("submission_accepted") is True else "账号查询确认已参与"
        return {"already": "账号已参与", "reserved": "提交结果未确认", "uncertain": "提交结果未确认",
                "review": "旧策略待定记录", "retry": "等待重试", "waiting": "随机等待中", "skipped": "已跳过"}.get(status, "状态暂不可用")

    def format_review_item(self, kind, key, page):
        rows = self.nodeseek_ai_review if kind == "ai" else self.ledger_rows()
        row = rows.get(key)
        if row is None:
            return "此记录已不存在，请返回列表刷新。", kb([cb("返回列表", f"list:{kind}:{page}")])
        reason = REASONS.get(row.get("reason"), "具体原因未记录")
        if kind == "ai":
            error = str(row.get("last_error", ""))
            code = re.search(r"\bHTTP\s*([45]\d\d)\b", error)
            reason = f"模型请求 HTTP {code.group(1)}" if code else "模型请求或结果格式未通过，已停止自动重试"
        lines = ["📝 <b>记录详情</b>", "", f"帖子：{e(row.get('title', '暂未获取'), 300)}",
                 f"状态：{e(self._row_status(row, kind))}", f"原因：{reason}",
                 f"发现时间：{stamp(row.get('queued_at') or row.get('at'))}",
                 f"记录更新时间：{stamp(row.get('quarantined_at') or row.get('at'))}"]
        diagnostic = row.get("diagnostics", {})
        eligibility = diagnostic.get("eligibility", {})
        if eligibility.get("first") in {"allow", "deny", "pending", "unavailable"}:
            first = {"allow": "通过", "deny": "不通过", "pending": "待定", "unavailable": "调用失败"}[eligibility["first"]]
            lines.append(f"资格初筛：{first}")
            if eligibility.get("rounds") == 2:
                lines.append("第二轮终审：" + ("通过" if eligibility.get("final") == "allow" else "未通过，已终止"))
        rules = diagnostic.get("reply_length_rules")
        if rules:
            lines.append(f"已记录字数要求：{e('；'.join(str(x) for x in rules), 240)}")
        if row.get("reply"):
            lines += ["", "<b>已保存回复</b>", e(row["reply"], 1000)]
        elif kind != "ai":
            lines.append("回复内容：没有已保存的提交回复")
        link = row.get("link") if kind == "ai" else f"https://sb.sb/t/{key}/"
        return "\n".join(lines), kb([url_button("查看原帖", link), cb("返回列表", f"list:{kind}:{page}")])

    def format_sources_card(self):
        lines, buttons = ["⚙️ <b>推送与参与</b>", ""], []
        for source in self.sources:
            sid = source["id"]
            on = self.source_states.get(sid, True)
            lines.append(f"{e(source['name'])}活动推送：{'开' if on else '关'}" + ("（全局暂停中）" if self.paused else ""))
            buttons.append([cb(f"{'关闭' if on else '开启'} {source['name']}推送", f"source:{sid}:{0 if on else 1}")])
        lines += [f"烧饼自动参与：{e(self._participation_label())}", "",
                  "关闭活动推送仍可自动参与。\n签到与定时日报按原设置执行。"]
        buttons += [[cb("关闭烧饼自动参与" if self.sbsb_participation_enabled else "开启烧饼自动参与", f"participation:{0 if self.sbsb_participation_enabled else 1}")],
                    [cb("返回状态", "menu:status")]]
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
            [cb("返回状态", "menu:status")])

    def ai_locked(self, field):
        value = getattr(self, AI_FIELDS[field][1], None)
        return value is not None if field == "threshold" else bool(value)

    def format_ai_card(self):
        lock = lambda field: " 🔒" if self.ai_locked(field) else ""
        lines = ["🧠 <b>AI 设置</b>", "", "用途：NodeSeek 活动判定", f"状态：{self._ai_label()}",
                 f"主模型：<code>{e(self.ai_model or '未设置', 200)}</code>{lock('model')}",
                 f"复审模型：{e(self.ai_judge_model or '跟随主模型', 200)}{lock('judge')}",
                 f"自动通过阈值：{self.ai_accept_threshold:.2f}{lock('threshold')}",
                 f"API：{'已配置' if self.ai_endpoint else '未设置'}{lock('endpoint')} · 密钥：{'已配置' if self.ai_api_key else '未设置'}{lock('key')}", "",
                 f"待判定：{len(self.nodeseek_ai_pending)} 篇 · 待复核总计：{len(self.nodeseek_ai_review)} 篇"]
        return "\n".join(lines), kb(
            [cb("主模型" + lock("model"), "ai:set_model"), cb("复审模型" + lock("judge"), "ai:set_judge")],
            [cb("通过阈值" + lock("threshold"), "ai:set_threshold"), cb("API 地址" + lock("endpoint"), "ai:set_endpoint")],
            [cb("API Key" + lock("key"), "ai:set_key"), cb("测试连接", "ai:test")],
            [cb(("停用 AI" if self.ai_enabled else "启用 AI") + (" 🔒" if self.env_ai_enabled is not None else ""),
                f"ai:enabled:{0 if self.ai_enabled else 1}"), cb("查看复核", "list:ai:0")],
            [cb("返回状态", "menu:status")])

    def format_help(self, details=False):
        if details:
            return ("ℹ️ <b>功能说明</b>\n\n活动推送和烧饼自动参与分别控制。\n"
                    "全局暂停同时停止活动、私信、互动提醒及自动参与；签到、签到通知和定时日报保留。\n"
                    "恢复后不补发暂停期间通知。\n\n私信目前只提醒新对话，已有对话的新消息不保证提醒。\n\n"
                    "论坛签到周期从北京时间08:00开始，计划08:05执行。\n"
                    "发送成功指Telegram接口接受；查询确认不等于程序提交；预算预留不等于实扣。"), kb([cb("返回帮助", "menu:help")])
        return ("🤖 <b>使用帮助</b>\n\n<code>/status</code> 运行状态\n<code>/report</code> 今日汇总\n"
                "<code>/sources</code> 推送与自动参与\n<code>/keywords</code> 关注词\n<code>/ai</code> AI 设置\n"
                "<code>/signin</code> 烧饼签到与查询\n<code>/pause</code> 暂停推送与参与\n"
                "<code>/resume</code> 恢复推送与参与\n<code>/cancel</code> 取消当前输入"), kb(
                    [cb("运行状态", "menu:status"), cb("功能说明", "menu:help_detail")])

    def panel(self, name):
        methods = {"status": self.format_status_card, "sources": self.format_sources_card,
                   "keywords": self.format_keywords_card, "ai": self.format_ai_card,
                   "participation": self.format_participation_card, "budget": self.format_budget_card,
                   "review": self.format_review_index, "report_detail": self.format_report_detail,
                   "help": self.format_help, "help_detail": lambda: self.format_help(True),
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
        self.send_msg(chat_id, "🔒 <b>此项由服务器配置管理</b>\n\n当前值不能通过 TG 修改，请在服务器更新对应配置并重新加载服务。",
                      reply_markup=kb([cb("返回 AI 设置", "menu:ai")]))

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
            if data.startswith(("menu:", "page:", "list:", "ai:set_", "source:", "participation:")):
                self.user_states.pop(chat_id, None)
            if data in {"menu:pause", "menu:resume"}:
                self.set_paused(data.endswith("pause"))
                card = (PAUSE_TEXT, kb([cb("恢复推送与参与", "menu:resume"), cb("查看状态", "menu:status")])) if self.paused else self.format_status_card()
                ack = "已暂停推送与参与" if self.paused else "已按原开关恢复推送与参与"
            elif data.startswith("menu:") and data[5:] in {"status", "sources", "keywords", "ai", "participation", "budget", "review", "report_detail", "help", "help_detail", "report"}:
                card = self.panel(data[5:])
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
                card = ("已取消本次输入，配置未更改。", kb([cb("返回状态", "menu:status")]))
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
                elif action == "review_item":
                    card = self.format_review_item(*payload)
                else:
                    raise ValueError("expired")
            elif data.startswith("ai:set_") and data[7:] in AI_FIELDS:
                field = data[7:]
                if self.ai_locked(field):
                    self.answer_callback_query(query_id, "此项由服务器配置锁定")
                    self.locked_reply(chat_id)
                    return
                label, _, state = AI_FIELDS[field]
                self.user_states[chat_id] = state
                instructions = {"endpoint": "请输入 HTTPS API 地址。", "key": "请发送 API Key。机器人会尝试删除原消息，删除结果另行告知。",
                                "model": "请发送模型名称，不要包含空格。", "judge": "请发送模型名称；发送 - 表示跟随主模型。",
                                "threshold": "请输入 0.70～0.99 之间的数值。"}[field]
                card = (f"✏️ <b>设置{label}</b>\n\n{instructions}\n发送 /cancel 取消。", kb([cb("取消", "input:cancel")]))
            elif data in {"ai:enabled:0", "ai:enabled:1"}:
                if self.env_ai_enabled is not None:
                    self.answer_callback_query(query_id, "启用开关由服务器配置锁定")
                    self.locked_reply(chat_id)
                    return
                if not (self.ai_endpoint and self.ai_model and self.ai_api_key):
                    self.answer_callback_query(query_id, "请先完成 AI 配置")
                    return
                previous = self.ai_enabled
                self.ai_enabled = data.endswith(":1")
                try:
                    self.save_settings()
                except Exception:
                    self.ai_enabled = previous
                    raise
                ack = "AI 已启用" if self.ai_enabled else "AI 已停用"
                card = self.format_ai_card()
            elif data == "ai:test":
                self.answer_callback_query(query_id, "正在测试 AI 连接")
                self.send_msg(chat_id, "⏳ 正在测试 AI 连接与返回格式…")
                try:
                    facts = self.test_ai_connection()
                    state = {"open": "活动进行中", "ended": "活动已结束", "upcoming": "尚未开始", "not_event": "非活动"}.get(facts.get("event_state"), "已识别")
                    text = f"✅ <b>AI 测试通过</b>\n\n连接正常，返回格式符合要求。\n示例识别：{state}\n判断置信度：{facts['confidence']:.2f}"
                except Exception:
                    text = "⚠️ <b>AI 测试未通过</b>\n\n未获取到有效的结构化结果，请检查模型支持情况或稍后再试。"
                self.send_msg(chat_id, text, reply_markup=kb([cb("返回 AI 设置", "menu:ai")]))
                return
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
        field = next((name for name, value in AI_FIELDS.items() if value[2] == state), None)
        if state == "waiting_for_add":
            self.show_panel(chat_id, None, self.add_keywords(text, legacy_words))
            if text.split():
                self.user_states.pop(chat_id, None)
            return
        deleted = self.delete_msg(chat_id, message_id) if field == "key" else None
        deletion = "包含密钥的原消息已删除。" if deleted else "原消息删除失败，请手动删除包含密钥的消息。"
        if field and self.ai_locked(field):
            self.user_states.pop(chat_id, None)
            if field == "key":
                self.send_msg(chat_id, "🔒 <b>API Key 未更改</b>\n\n此项由服务器配置管理。\n" + deletion,
                              reply_markup=kb([cb("返回 AI 设置", "menu:ai")]))
            else:
                self.locked_reply(chat_id)
            return
        try:
            value = text.strip()
            if field == "endpoint":
                normalizer(value)
                value = value.rstrip("/")
            elif field == "key":
                if not value or len(value) > 1000:
                    raise ValueError
            elif field in {"model", "judge"}:
                value = "" if field == "judge" and value == "-" else value
                if (not value and field == "model") or len(value) > 200 or any(c.isspace() for c in value):
                    raise ValueError
            elif field == "threshold":
                value = float(value)
                if not 0.70 <= value <= 0.99:
                    raise ValueError
            else:
                self.user_states.pop(chat_id, None)
                self.send_msg(chat_id, "输入已过期，请重新打开设置面板。")
                return
        except (ValueError, TypeError):
            label = AI_FIELDS[field][0] if field else "输入"
            message = f"⚠️ <b>{label}格式不正确</b>\n\n请重新发送，仍在等待此项输入；发送 /cancel 取消。"
            if field == "key":
                message += "\n" + deletion
            self.send_msg(chat_id, message, reply_markup=kb([cb("取消", "input:cancel")]))
            return
        if field == "key":
            try:
                self.save_ai_secret(value)
            except Exception:
                self.send_msg(chat_id, "⚠️ <b>密钥保存结果未确认</b>\n\n请核对配置后再继续。\n" + deletion,
                              reply_markup=kb([cb("返回 AI 设置", "menu:ai"), cb("取消", "input:cancel")]))
                return
        else:
            attr = {"endpoint": "ai_endpoint", "model": "ai_model", "judge": "ai_judge_model", "threshold": "ai_accept_threshold"}[field]
            previous = getattr(self, attr)
            setattr(self, attr, value)
            try:
                self.save_settings()
            except Exception:
                setattr(self, attr, previous)
                self.send_msg(chat_id, "⚠️ 配置保存失败，当前值未更改。仍在等待输入，可重新发送或 /cancel 取消。",
                              reply_markup=kb([cb("取消", "input:cancel")]))
                return
        self.user_states.pop(chat_id, None)
        label = AI_FIELDS[field][0]
        display = ("\n" + deletion) if field == "key" else "" if field == "endpoint" else ("\n" + e(
            "跟随主模型" if field == "judge" and not value else f"{value:.2f}" if field == "threshold" else value, 200))
        self.send_msg(chat_id, f"✅ <b>{label}已保存</b>{display}", reply_markup=kb([cb("返回 AI 设置", "menu:ai")]))

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
                          reply_markup=kb([cb("返回状态", "menu:status")]))
            return
        if chat_id in self.user_states and not text.startswith("/"):
            self.consume_input(chat_id, text, message_id, normalizer, legacy_words)
            return
        self.user_states.pop(chat_id, None)
        panels = {"/start": "help", "/help": "help", "/status": "status", "/stats": "status", "/report": "report",
                  "/sources": "sources", "/ai": "ai", "/keywords": "keywords", "/list": "keywords"}
        if command in panels:
            self.show_panel(chat_id, None, self.panel(panels[command]))
        elif command in {"/signin", "/checkin"}:
            self.send_msg(chat_id, "⏳ 正在检查本轮签到；已签到则只查询。")
            self.query_signin(chat_id, allow_submit=True)
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
            self.send_msg(chat_id, PAUSE_TEXT, reply_markup=kb([cb("恢复推送与参与", "menu:resume")]))
        elif command == "/resume":
            self.set_paused(False)
            self.send_msg(chat_id, "▶️ <b>已恢复推送与参与</b>\n\n按各站独立开关执行，不补发暂停期间的通知。",
                          reply_markup=kb([cb("查看状态", "menu:status")]))
        elif command == "/test":
            for kind, title in (("lottery", "抽一台测试 VPS"), ("welfare", "限时测试额度"),
                                ("redpacket", "红包样式示例"), ("custom", "DMIT 套餐分享")):
                text, markup = format_activity("NodeSeek", kind, title, "示例作者", "日常",
                                              "这是样式演示，不代表真实活动、参与或奖励结果。",
                                              "https://www.nodeseek.com/", demo=True)
                self.send_msg(chat_id, text, reply_markup=markup)
        else:
            self.send_msg(chat_id, "暂未识别这条指令。发送 /help 查看可用功能。", reply_markup=kb([cb("使用帮助", "menu:help")]))
