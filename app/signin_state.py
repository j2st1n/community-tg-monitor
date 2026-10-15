"""Forum-day state, evidence and optional asset parsing. No network operations."""
import copy
import fcntl
import html
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

CST = timezone(timedelta(hours=8))
UNKNOWN = "暂未获取"
_LOCK = threading.RLock()
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
FIELDS = ("consecutive_days", "total_days", "total_points", "exp", "level", "signin_time")


def period_key(now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def period_start(key):
    return datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc).astimezone(CST)


def due(now, completed_period):
    key = period_key(now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    return key != completed_period and now >= period_start(key) + timedelta(minutes=5)


class StateError(ValueError):
    pass


class SigninStore:
    def __init__(self, path):
        self.path = os.fspath(path)

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as file:
                state = json.load(file)
        except FileNotFoundError:
            return {}
        except Exception:
            raise StateError("signin_state_invalid") from None
        if not isinstance(state, dict) or state.get("version", 2) != 2:
            raise StateError("signin_state_invalid")
        for key in ("last_date", "manual_required_date", "blocked_period", "period"):
            if key in state and (not isinstance(state[key], str) or not _DATE.fullmatch(state[key])):
                raise StateError("signin_state_invalid")
        confirmation = state.get("confirmation")
        if confirmation is not None and (not isinstance(confirmation, dict)
                or not _DATE.fullmatch(str(confirmation.get("period", "")))
                or confirmation.get("source") not in {"submitted_confirmed", "query_confirmed"}):
            raise StateError("signin_state_invalid")
        for field in ("notice", "last_query", "submission_guard"):
            if field in state and not isinstance(state[field], dict):
                raise StateError("signin_state_invalid")
            value = state.get(field, {})
            if value and not _DATE.fullmatch(str(value.get("period", ""))):
                raise StateError("signin_state_invalid")
        notice = state.get("notice", {})
        if notice and (notice.get("kind") not in {"success", "warning"}
                       or notice.get("status") not in {"pending", "accepted", "unconfirmed", "legacy_unknown"}):
            raise StateError("signin_state_invalid")
        return state

    @contextmanager
    def locked(self):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        with _LOCK, open(self.path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self.load()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self, state):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".signin-", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(path, 0o600)
            os.replace(path, self.path)
        finally:
            if os.path.exists(path):
                os.unlink(path)


def confirmed(state, key):
    record = state.get("confirmation", {})
    return record if record.get("period") == key else None


def blocked(state, key):
    # Legacy stop flags remain conservative, but legacy completion alone is not
    # sufficient evidence: a fresh GET upgrades it without trusting midnight dates.
    return state.get("blocked_period", state.get("manual_required_date")) == key


def record_result(state, result, now):
    state = copy.deepcopy(state)
    if "version" not in state and state.get("last_date"):
        state["legacy_last_date"] = state.pop("last_date")
    key = result.get("period") or period_key(now)
    observed = now.astimezone(CST).isoformat()
    if result.get("success"):
        outcome = result.get("outcome") or ("query_confirmed" if result.get("already") else "submitted_confirmed")
    else:
        outcome = result.get("outcome", "query_unavailable")
    state.update(version=2, period=key)
    state["last_query"] = {"period": key, "outcome": outcome, "at": observed,
                           "error_code": result.get("error_code", "")}
    if result.get("needs_human"):
        state["blocked_period"] = key
        state["manual_required_date"] = key
    if result.get("success"):
        record = copy.deepcopy(confirmed(state, key) or {})
        record.update(period=key, verified_at=observed)
        record.setdefault("first_confirmed_at", observed)
        if outcome == "submitted_confirmed" or not record.get("source"):
            record["source"] = outcome
        times = record.setdefault("field_times", {})
        for field in FIELDS:
            value = result.get(field)
            if value not in (None, "", UNKNOWN):
                record[field] = str(value)
                times[field] = observed
        state["confirmation"] = record
        state["last_date"] = key
        for flag in ("blocked_period", "manual_required_date"):
            if state.get(flag) == key:
                state.pop(flag, None)
    return state


def summary(state, now, configured=True):
    if not configured:
        return "未配置登录凭据"
    key = period_key(now)
    record = confirmed(state, key)
    if record:
        streak = record.get("consecutive_days", UNKNOWN)
        result = f"已完成，连续 {streak}" if streak != UNKNOWN else "已完成，天数暂未获取"
        notice = state.get("notice", {})
        if notice.get("period") == key and notice.get("kind") == "success" and notice.get("status") == "unconfirmed":
            result += "（通知发送未确认）"
        return result
    query = state.get("last_query", {})
    if query.get("period") == key:
        if query.get("outcome") == "unsigned":
            return "尚未签到" + ("，自动提交已暂停" if blocked(state, key) else "")
        if query.get("outcome") == "submission_unconfirmed":
            return "提交结果待确认，自动提交已暂停"
        if query.get("outcome") == "query_unavailable":
            return "暂时无法查询"
    if blocked(state, key):
        return "自动提交已暂停，状态待查询"
    if now.hour < 8:
        return "上一轮状态待查询；下一轮计划 08:05"
    if not due(now, ""):
        return "计划 08:05，本轮尚未查询"
    return "尚未查询"


class PointsParser(HTMLParser):
    """Read visible values and the explicitly scoped current-level badge.

    The site also renders future levels and a full ladder. Their title attributes
    are not current account data and must never win a broad raw-HTML regex.
    """
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.texts, self.records = [], []
        self.hidden = 0
        self.time = None
        self.last_time = None
        self.parents = []
        self.level_titles, self.level_texts = [], []
        self.row = None
        self.fragment = not re.search(r"<(?:table|tr)\b", source, re.I)
        self.feed(source)
        self.finish_row()

    def finish_row(self):
        if self.row and self.row["daily"] and len(self.row["times"]) == 1:
            self.records.extend(self.row["times"])
        self.row = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag not in {"area", "base", "br", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}:
            self.parents.append((tag, classes))
        if tag in {"script", "style", "svg"}:
            self.hidden += 1
        if (not self.hidden and "level-badge" in classes and attrs.get("title")
                and any("points-counter-level" in parent[1] for parent in self.parents)):
            self.level_titles.append(attrs["title"])
        if tag == "time" and not self.hidden:
            self.time = {"iso": attrs.get("datetime", ""), "text": ""}
        if tag == "tr":
            self.finish_row()
            self.row = {"daily": False, "times": []}
            self.last_time = None

    def handle_endtag(self, tag):
        for index in range(len(self.parents) - 1, -1, -1):
            if self.parents[index][0] == tag:
                self.parents = self.parents[:index]
                break
        if tag in {"script", "style", "svg"} and self.hidden:
            self.hidden -= 1
        if tag == "time" and self.time:
            self.last_time = self.time
            if self.row is not None:
                self.row["times"].append(self.time)
            self.time = None
        if tag in {"tr", "tbody", "table"}:
            self.finish_row()

    def handle_data(self, data):
        if self.hidden:
            return
        text = data.strip()
        if self.time is not None:
            self.time["text"] += data
        if text:
            self.texts.append(text)
            if any("points-counter-level" in parent[1] for parent in self.parents):
                self.level_texts.append(text)
            if text == "每日签到" and any(tag == "td" for tag, _ in self.parents):
                if self.row is not None:
                    self.row["daily"] = True
                elif self.fragment and self.last_time:
                    self.records.append(self.last_time)
                    self.last_time = None

    def values(self, key):
        text = "\n".join(self.texts)
        result = dict(total_points=UNKNOWN, exp=UNKNOWN, level=UNKNOWN, signin_time=UNKNOWN)
        for label, field in (("可用烧饼", "total_points"), ("成长值", "exp")):
            match = re.search(re.escape(label) + r"\s*[:：]?\s*(\d+)(?!\d)", text)
            if match:
                result[field] = match.group(1) + (" 饼" if field == "total_points" else "")
        candidates = set()
        for value in self.level_titles or [" ".join(self.level_texts)]:
            value = re.split(r"[·|｜]|成长值", value, maxsplit=1)[0].strip()
            if re.fullmatch(r"Lv\.\s*\d+\s*[^\n<>\"']{0,60}", value):
                candidates.add(re.sub(r"\s+", " ", value))
        if not candidates:
            match = re.search(r"等级\s*[:：]?\s*(Lv\.\s*\d+[ \t]*[^\n<>\"']*)", text)
            if match:
                candidates.add(re.split(r"[·|｜]|成长值", match.group(1), maxsplit=1)[0].strip())
        if len(candidates) == 1:
            result["level"] = candidates.pop()
        if result["exp"] == UNKNOWN and self.level_titles:
            growth = {m.group(1) for value in self.level_titles
                      if (m := re.search(r"成长值\s*(\d+)\s*$", value))}
            if len(growth) == 1:
                result["exp"] = growth.pop()
        candidates = set()
        for record in self.records:
            for value in (record["iso"], record["text"].strip()):
                try:
                    stamp = datetime.fromisoformat(html.unescape(value).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=CST)
                    if period_key(stamp) == key:
                        candidates.add(stamp.astimezone(CST))
                    break
                except ValueError:
                    continue
        if len(candidates) == 1:
            result["signin_time"] = candidates.pop().strftime("%Y-%m-%d %H:%M:%S")
        return result
