"""Human-readable feedback derived from recorded evidence, not cookie presence."""
from datetime import timedelta

try:
    from app.signin_state import UNKNOWN, FIELDS, period_key, period_start, confirmed, blocked
    from app.telegram_text import escape as e, keyboard, callback, url_button
except ModuleNotFoundError:
    from signin_state import UNKNOWN, FIELDS, period_key, period_start, confirmed, blocked
    from telegram_text import escape as e, keyboard, callback, url_button


def signin_markup():
    return keyboard([callback("刷新查询", "signin:query"), url_button("查看签到页", "https://sb.sb/checkin/")],
                    [callback("返回首页", "menu:status")])


def format_signin(result, state, now, automatic=False):
    key = result.get("period") or period_key(now)
    record = confirmed(state, key) or {}
    current_key = period_key(now)
    previous = key != current_key or (now.hour < 8 and key != now.strftime("%Y-%m-%d"))
    if result.get("success"):
        submitted = result.get("outcome") == "submitted_confirmed" or (
            "outcome" not in result and not result.get("already"))
        title = "上一轮已签到" if previous else (
            "烧饼自动签到成功" if submitted and automatic else "烧饼签到成功" if submitted else "本轮已签到（查询确认）")
        lines = [f"🍪 <b>{title}</b>", ""]

        def field(name):
            value = result.get(name)
            if value not in (None, "", UNKNOWN):
                return e(value)
            cached = record.get(name)
            return e(cached) + "（此前核验）" if cached else UNKNOWN

        lines += [f"连续 <b>{field('consecutive_days')}</b> · 累计 {field('total_days')}"]
        if not automatic:
            lines += [f"可用烧饼：{field('total_points')}", f"等级：{field('level')}", f"成长值：{field('exp')}"]
        lines.append(f"签到时间：{field('signin_time')}")
        if previous:
            start = period_start(key)
            end = start + timedelta(days=1)
            lines += [f"签到周期：{start.strftime('%m月%d日 08:00')} 至 {end.strftime('%m月%d日 08:00')}前",
                      f"下一轮计划：{end.strftime('%m月%d日')} 08:05"]
        lines += ["", "本程序提交，官方已确认。" if submitted else "本次仅查询确认，没有再次提交。"]
        return "\n".join(lines), signin_markup()

    record = confirmed(state, current_key)
    if record:
        when = record.get("verified_at", "")[:16].replace("T", " ") or "暂未获取"
        return ("🍪 <b>本轮签到已确认完成</b>\n\n"
                f"上次官方核验：{e(when)}（北京时间）\n"
                f"连续 {e(record.get('consecutive_days', UNKNOWN))} · 累计 {e(record.get('total_days', UNKNOWN))}\n\n"
                "最新查询未完成，以上为已核验记录。"), signin_markup()
    outcome = result.get("outcome", "query_unavailable")
    title, text = {
        "unsigned": ("本轮尚未签到", "官方查询确认当前尚未签到。\n本次仅查询，没有提交。"),
        "submission_unconfirmed": ("签到结果待确认", "程序已提交请求，但最终签到结果尚未确认。" if
                                   result.get("submission_attempted") or result.get("submission_accepted") else
                                   "自动流程结果尚未确认，暂不重复提交。"),
        "login_required": ("无法查询签到状态", "论坛将请求转至登录页，请更新登录凭据。"),
        "not_configured": ("签到未配置", "请先在服务器配置烧饼登录凭据。"),
        "unsupported_form": ("自动流程未完成", "无法确认官方签到表单，未提交；请打开论坛查看。"),
        "automation_stopped": ("自动流程未完成", "本次未获得签到确认，请打开论坛查看。"),
    }.get(outcome, ("暂时无法确认签到状态", "本次官方查询未完成，不能据此判断未签或断签。"))
    if result.get("error_code") in {"automated_browser_detected", "instr_automated_browser"}:
        text = "站方明确拒绝当前浏览器运行方式，请打开论坛查看。"
    if blocked(state, key):
        text += "\n本轮自动提交已暂停，继续只读查询。"
    elif automatic and outcome == "query_unavailable":
        text += "\n自动处理将继续按计划检查。"
    return f"⚠️ <b>{title}</b>\n\n{text}", signin_markup()
