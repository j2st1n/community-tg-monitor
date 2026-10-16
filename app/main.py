#!/usr/bin/env python3
import os
import sys
import time
import json
import html
import re
import hashlib
import traceback
import threading
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

try:
    from app.ai_classifier import (
        AIClassificationError, OpenAICompatibleClassifier, clean_for_ai, normalize_chat_endpoint,
    )
except ModuleNotFoundError:
    from ai_classifier import (
        AIClassificationError, OpenAICompatibleClassifier, clean_for_ai, normalize_chat_endpoint,
    )

try:
    from app.participation import ContentPlanner, TopicPage, build_participant
except ModuleNotFoundError:
    from participation import ContentPlanner, TopicPage, build_participant

try:
    from app.telegram_ui import TelegramUI, format_activity
    from app.telegram_text import (escape as tg_escape, plain as tg_plain, clip as tg_clip,
                                   split_html, fit_html, valid_markup, keyboard, callback, url_button)
    from app.signin_state import (CST, UNKNOWN, SigninStore, StateError, PointsParser,
                                  period_key, period_start, due as signin_due,
                                  confirmed as signin_confirmed, blocked as signin_blocked,
                                  record_result as record_signin_result, summary as signin_summary)
    from app.signin_feedback import format_signin, signin_markup
except ModuleNotFoundError:
    from telegram_ui import TelegramUI, format_activity
    from telegram_text import (escape as tg_escape, plain as tg_plain, clip as tg_clip,
                               split_html, fit_html, valid_markup, keyboard, callback, url_button)
    from signin_state import (CST, UNKNOWN, SigninStore, StateError, PointsParser,
                              period_key, period_start, due as signin_due,
                              confirmed as signin_confirmed, blocked as signin_blocked,
                              record_result as record_signin_result, summary as signin_summary)
    from signin_feedback import format_signin, signin_markup

APP_VERSION = "5.1"
APP_USER_AGENT = f"Community-Monitor-Bot/{APP_VERSION}"
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SEEN_IDS_FILE = os.path.join(DATA_DIR, "seen_ids.json")
SEEN_MSGS_FILE = os.path.join(DATA_DIR, "seen_msgs.json")
CHECKIN_STATE_FILE = os.path.join(DATA_DIR, "checkin_state.json")
DAILY_STATS_FILE = os.path.join(DATA_DIR, "daily_stats.json")
SBSB_EVENTS_FILE = os.path.join(DATA_DIR, "sbsb_events.json")
AI_SECRET_FILE = os.path.join(DATA_DIR, "ai_secret.json")
NODESEEK_AI_STATE_FILE = os.path.join(DATA_DIR, "nodeseek_ai_state.json")
MAX_SEEN_IDS = 10000
MAX_SEEN_MSGS = 2000
MAX_SBSB_EVENTS = 5000
SBSB_RECONCILE_INTERVAL = 300
SBSB_MARKER_PATHS = (("lottery", "/prize/"), ("redpacket", "/redpacket/"))
MAX_NODESEEK_AI_PENDING = 500
MAX_NODESEEK_AI_HISTORY = 2000
MAX_AI_POSTS_PER_CYCLE = 5
MAX_AI_RETRY_ATTEMPTS = 24

# 内置多源配置
DEFAULT_SOURCES = [
    {
        "id": "nodeseek",
        "name": "NodeSeek",
        "icon": "🌐",
        "url": "https://rss.nodeseek.com/",
        "author_tag": "{http://purl.org/dc/elements/1.1/}creator",
    },
    {
        "id": "sbsb",
        "name": "烧饼论坛",
        "icon": "🍪",
        "url": "https://sb.sb/rss.xml",
        "author_tag": None,
    }
]

DEFAULT_POLL_INTERVAL = 30
DEFAULT_PRIVATE_INTERVAL = 45

# 默认用户自定义关键词（初始为空，不启用；添加后享受最高优先级直通）
DEFAULT_KEYWORDS = []

# 精简后的 4 大核心指令
BOT_COMMANDS = [
    {"command": "status", "description": "运行状态"},
    {"command": "keywords", "description": "关注词"},
    {"command": "sources", "description": "推送与自动参与"},
    {"command": "signin", "description": "烧饼签到与查询"},
]

LEGACY_BUILTIN_WORDS = {
    "抽奖", "抽", "福利", "roll", "Roll", "ROLL",
    "送只", "送个", "送台", "送一", "白送", "直接送", "先到先得", "免费送", "送小鸡", "送机器", "送码",
    "口令", "红包", "开奖", "盖楼", "中奖", "白嫖", "免费"
}


class BotManager(TelegramUI):
    def __init__(self):
        self.bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
        self.admin_chat_id = str(os.environ.get("TG_CHAT_ID", "")).strip()
        
        sbsb_cookie_val = os.environ.get("SBSB_COOKIE", "").strip()
        if not sbsb_cookie_val and os.environ.get("__Host-bbs_session"):
            sbsb_cookie_val = os.environ.get("__Host-bbs_session").strip()
            
        if sbsb_cookie_val and "=" not in sbsb_cookie_val:
            sbsb_cookie_val = f"__Host-bbs_session={sbsb_cookie_val}"
        self.sbsb_cookie = sbsb_cookie_val
        self.sbsb_uid = None
        
        self.lock = threading.RLock()
        self.start_time = datetime.now()
        self.total_checked = 0
        self.total_hit = 0
        self.total_private_notified = 0
        self.last_cookie_warn_time = 0
        self.last_daily_report_date = ""
        
        self.user_states = {}
        self.sources = list(DEFAULT_SOURCES)
        self.source_states = {s["id"]: True for s in DEFAULT_SOURCES}
        
        # 每日统计字典
        self.daily_stats = self.load_daily_stats()
        
        # 支持通过环境变量过滤启用的源
        enabled_source_ids = os.environ.get("MONITOR_SOURCES", "").strip().lower()
        if enabled_source_ids:
            allowed = [s.strip() for s in enabled_source_ids.split(",") if s.strip()]
            self.sources = [s for s in self.sources if s["id"] in allowed]
            if not self.sources:
                print(f"[{datetime.now()}] ⚠️ 环境变量 MONITOR_SOURCES 过滤后无有效源，恢复默认全源。", flush=True)
                self.sources = list(DEFAULT_SOURCES)

        # AI 环境变量注入支持
        self.env_ai_endpoint = (os.environ.get("AI_ENDPOINT") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "").strip()
        self.env_ai_api_key = (os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
        self.env_ai_model = os.environ.get("AI_MODEL", "").strip()
        self.env_ai_judge_model = os.environ.get("AI_JUDGE_MODEL", "").strip()
        env_threshold = os.environ.get("AI_ACCEPT_THRESHOLD", "").strip()
        try:
            self.env_ai_accept_threshold = float(env_threshold) if env_threshold else None
        except ValueError:
            self.env_ai_accept_threshold = None
        env_enabled = os.environ.get("AI_ENABLED", "").strip().lower()
        self.env_ai_enabled = (env_enabled in ("1", "true", "yes", "on")) if env_enabled else None
        
        self.load_settings()
        self.ai_api_key = self.load_ai_secret()
        ai_state = self.load_nodeseek_ai_state()
        self.nodeseek_ai_pending = ai_state.get("pending", {})
        self.nodeseek_ai_history = ai_state.get("history", [])
        self.nodeseek_ai_review = ai_state.get("review", {})
        loaded_seen_ids = self.load_seen_ids()
        self.seen_id_order = list(dict.fromkeys(loaded_seen_ids))
        self.seen_ids = set(self.seen_id_order)
        loaded_seen_msgs = self.load_seen_msgs()
        self.seen_msg_order = list(dict.fromkeys(loaded_seen_msgs))
        self.seen_msgs = set(self.seen_msg_order)
        sbsb_events = self.load_sbsb_events()
        self.sbsb_events_initialized = bool(sbsb_events.get("initialized"))
        self.sbsb_event_order = {
            kind: list(dict.fromkeys(sbsb_events.get(kind, [])))
            for kind in ("lottery", "redpacket")
        }
        self.sbsb_events = {
            kind: set(self.sbsb_event_order[kind])
            for kind in ("lottery", "redpacket")
        }
        self.participant = build_participant(DATA_DIR, self.sbsb_cookie, self.build_ai_classifier)
        self.participant.allowed = self.is_sbsb_participation_allowed
        self.register_telegram_commands()

    def get_today_cst(self):
        tz_cst = timezone(timedelta(hours=8))
        return datetime.now(tz_cst).strftime("%Y-%m-%d")

    def local_now(self):
        return datetime.now(CST)

    def signin_status(self):
        try:
            return signin_summary(SigninStore(CHECKIN_STATE_FILE).load(), self.local_now(), bool(self.sbsb_cookie))
        except StateError:
            return "记录读取异常，状态待查询"

    def query_signin(self, chat_id, allow_submit=False):
        return execute_sbsb_signin(self, chat_id, allow_submit=allow_submit)

    def load_daily_stats(self):
        today = self.get_today_cst()
        default_stats = {
            "date": today,
            "total_scanned": 0,
            "lottery_hits": 0,
            "custom_hits": 0,
            "private_notified": 0,
            "poll_success": 0,
            "poll_errors": 0,
            "delivery_success": 0,
            "delivery_errors": 0,
            "marker_poll_success": 0,
            "marker_poll_errors": 0,
            "sbsb_lottery_events": 0,
            "sbsb_redpacket_events": 0,
            "ai_requests": 0,
            "ai_errors": 0,
            "ai_classified": 0,
            "ai_giveaway_hits": 0,
            "ai_second_reviews": 0,
            "ai_uncertain": 0
        }
        if os.path.exists(DAILY_STATS_FILE):
            try:
                with open(DAILY_STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("date") == today:
                        for k, v in default_stats.items():
                            if k not in data:
                                data[k] = v
                        return data
            except Exception as e:
                print(f"[{datetime.now()}] 读取 daily_stats.json 异常: {e}", flush=True)
        return default_stats

    def save_daily_stats(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DAILY_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.daily_stats, f, ensure_ascii=False, indent=2)

    def record_stat(self, key, count=1):
        with self.lock:
            today = self.get_today_cst()
            if self.daily_stats.get("date") != today:
                self.daily_stats = {
                    "date": today,
                    "total_scanned": 0,
                    "lottery_hits": 0,
                    "custom_hits": 0,
                    "private_notified": 0,
                    "poll_success": 0,
                    "poll_errors": 0,
                    "delivery_success": 0,
                    "delivery_errors": 0,
                    "marker_poll_success": 0,
                    "marker_poll_errors": 0,
                    "sbsb_lottery_events": 0,
                    "sbsb_redpacket_events": 0,
                    "ai_requests": 0,
                    "ai_errors": 0,
                    "ai_classified": 0,
                    "ai_giveaway_hits": 0,
                    "ai_second_reviews": 0,
                    "ai_uncertain": 0
                }
            self.daily_stats[key] = self.daily_stats.get(key, 0) + count
            self.save_daily_stats()

    def register_telegram_commands(self):
        if not self.bot_token:
            return
        accepted = self._telegram_call("setMyCommands", {"commands": BOT_COMMANDS})
        print(f"[{datetime.now()}] Telegram 指令菜单注册: {'已确认' if accepted else '未确认'}", flush=True)

    def load_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        saved_ai_enabled = False
        # Older settings have only notification switches. Preserve the separately
        # configured participation service, even when notifications are muted.
        self.sbsb_participation_enabled = True
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    saved_ai_enabled = "ai_enabled" in data
                    raw_keywords = data.get("keywords", [])
                    self.keywords = [k for k in raw_keywords if k not in LEGACY_BUILTIN_WORDS]
                    self.poll_interval = data.get("poll_interval", DEFAULT_POLL_INTERVAL)
                    self.paused = data.get("paused", False)
                    self.sbsb_participation_enabled = bool(data.get("sbsb_participation_enabled", True))
                    self.ai_enabled = bool(data.get("ai_enabled", False))
                    self.ai_endpoint = str(data.get("ai_endpoint", "")).strip()
                    self.ai_model = str(data.get("ai_model", "")).strip()
                    self.ai_judge_model = str(data.get("ai_judge_model", "")).strip()
                    self.ai_accept_threshold = min(0.99, max(0.70, float(data.get("ai_accept_threshold", 0.90))))
                    saved_source_states = data.get("source_states", {})
                    for s in self.sources:
                        sid = s["id"]
                        if sid in saved_source_states:
                            self.source_states[sid] = bool(saved_source_states[sid])
            except Exception as e:
                print(f"[{datetime.now()}] 读取 settings.json 异常: {e}", flush=True)
        else:
            self.keywords = list(DEFAULT_KEYWORDS)
            self.poll_interval = DEFAULT_POLL_INTERVAL
            self.paused = False
            self.ai_enabled = False
            self.ai_endpoint = ""
            self.ai_model = ""
            self.ai_judge_model = ""
            self.ai_accept_threshold = 0.90
            self.source_states = {s["id"]: True for s in self.sources}

        # 环境变量优先覆盖与默认激活
        if self.env_ai_endpoint:
            self.ai_endpoint = self.env_ai_endpoint
        if self.env_ai_model:
            self.ai_model = self.env_ai_model
        if self.env_ai_judge_model:
            self.ai_judge_model = self.env_ai_judge_model
        if self.env_ai_accept_threshold is not None:
            self.ai_accept_threshold = min(0.99, max(0.70, self.env_ai_accept_threshold))
        if self.env_ai_enabled is not None:
            self.ai_enabled = self.env_ai_enabled
        elif not saved_ai_enabled and self.env_ai_api_key and self.ai_endpoint and self.ai_model:
            self.ai_enabled = True
        if not saved_ai_enabled:
            self.save_settings()

    def save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "keywords": self.keywords,
            "poll_interval": self.poll_interval,
            "paused": self.paused,
            "source_states": self.source_states,
            "sbsb_participation_enabled": self.sbsb_participation_enabled,
            "ai_enabled": self.ai_enabled,
            "ai_endpoint": self.ai_endpoint,
            "ai_model": self.ai_model,
            "ai_judge_model": self.ai_judge_model,
            "ai_accept_threshold": self.ai_accept_threshold
        }
        with self.lock:
            fd, path = tempfile.mkstemp(prefix=".settings-", dir=DATA_DIR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(data, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(path, SETTINGS_FILE)
            finally:
                if os.path.exists(path):
                    os.unlink(path)

    def load_ai_secret(self):
        if self.env_ai_api_key:
            return self.env_ai_api_key
        if os.path.exists(AI_SECRET_FILE):
            try:
                with open(AI_SECRET_FILE, "r", encoding="utf-8") as f:
                    value = json.load(f).get("api_key", "")
                    return str(value).strip()
            except Exception as e:
                print(f"[{datetime.now()}] 读取 AI 密钥文件异常: {e}", flush=True)
        return ""

    def save_ai_secret(self, api_key):
        if self.env_ai_api_key:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        temp_path = f"{AI_SECRET_FILE}.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"api_key": api_key}, f, ensure_ascii=False)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, AI_SECRET_FILE)
        os.chmod(AI_SECRET_FILE, 0o600)
        self.ai_api_key = api_key

    def load_nodeseek_ai_state(self):
        if os.path.exists(NODESEEK_AI_STATE_FILE):
            try:
                with open(NODESEEK_AI_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                pending = data.get("pending", {})
                history = data.get("history", [])
                review = data.get("review", {})
                return {
                    "pending": pending if isinstance(pending, dict) else {},
                    "history": history if isinstance(history, list) else [],
                    "review": review if isinstance(review, dict) else {},
                }
            except Exception as e:
                print(f"[{datetime.now()}] 读取 NodeSeek AI 状态异常: {e}", flush=True)
        return {"pending": {}, "history": [], "review": {}}

    def save_nodeseek_ai_state(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if len(self.nodeseek_ai_pending) > MAX_NODESEEK_AI_PENDING:
            overflow = len(self.nodeseek_ai_pending) - MAX_NODESEEK_AI_PENDING
            for key in list(self.nodeseek_ai_pending)[:overflow]:
                self.nodeseek_ai_pending.pop(key, None)
        if len(self.nodeseek_ai_history) > MAX_NODESEEK_AI_HISTORY:
            self.nodeseek_ai_history = self.nodeseek_ai_history[-MAX_NODESEEK_AI_HISTORY:]
        with open(NODESEEK_AI_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"pending": self.nodeseek_ai_pending, "history": self.nodeseek_ai_history,
                 "review": self.nodeseek_ai_review},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def queue_nodeseek_post(self, unique_id, category, title, author, desc, link):
        if unique_id in self.nodeseek_ai_pending or unique_id in self.nodeseek_ai_review:
            return False
        self.nodeseek_ai_pending[unique_id] = {
            "id": unique_id,
            "category": clean_for_ai(category, 80),
            "title": clean_for_ai(title, 300),
            "author": clean_for_ai(author, 100),
            "description": clean_for_ai(desc, 4000),
            "link": link,
            "queued_at": int(time.time()),
            "attempts": 0,
            "next_retry_at": 0,
            "last_error": "",
        }
        self.save_nodeseek_ai_state()
        return True

    def ai_is_ready(self):
        return bool(self.ai_enabled and self.ai_endpoint and self.ai_model and self.ai_api_key)

    def build_ai_classifier(self):
        return OpenAICompatibleClassifier(
            self.ai_endpoint,
            self.ai_api_key,
            self.ai_model,
            self.ai_judge_model,
        )

    def masked_ai_key(self):
        if not self.ai_api_key:
            return "未设置"
        if self.env_ai_api_key:
            if len(self.ai_api_key) <= 8:
                return "🔒 .env 环境变量注入 (••••••••)"
            return f"🔒 .env 注入 ({self.ai_api_key[:3]}••••{self.ai_api_key[-4:]})"
        if len(self.ai_api_key) <= 8:
            return "••••••••"
        return f"{self.ai_api_key[:3]}••••{self.ai_api_key[-4:]}"

    def test_ai_connection(self):
        classifier = self.build_ai_classifier()
        facts = classifier.extract_facts({
            "source": "NodeSeek",
            "category": "daily",
            "title": "【抽奖】回复本帖随机送一台测试 VPS",
            "author": "system-test",
            "description": "活动今天有效，坛友回复即可参加，免费随机抽取一人。",
            "link": "https://www.nodeseek.com/",
        })
        return facts

    def is_source_enabled(self, source_id):
        with self.lock:
            return self.source_states.get(source_id, True)

    def toggle_source(self, source_id):
        with self.lock:
            current = self.source_states.get(source_id, True)
            self.source_states[source_id] = not current
            try:
                self.save_settings()
            except Exception:
                self.source_states[source_id] = current
                raise
            return self.source_states[source_id]

    def is_sbsb_participation_allowed(self):
        with self.lock:
            participant = getattr(self, "participant", None)
            return bool(participant and participant.enabled and
                        self.sbsb_participation_enabled and not self.paused)

    def toggle_sbsb_participation(self):
        with self.lock:
            self.sbsb_participation_enabled = not self.sbsb_participation_enabled
            try:
                self.save_settings()
            except Exception:
                self.sbsb_participation_enabled = not self.sbsb_participation_enabled
                raise
            return self.sbsb_participation_enabled

    def run_sbsb_participation(self, topics):
        return self.participant.run(topics, paused=not self.is_sbsb_participation_allowed())

    def sbsb_participation_status(self):
        with self.lock:
            participant = getattr(self, "participant", None)
            if not participant or not participant.enabled:
                return "⚪ 服务未启用"
            if not self.sbsb_participation_enabled:
                return "🔴 已关闭"
            if self.paused:
                return "⏸️ 全局暂停中"
            return "🟢 已开启"

    def match_custom_keyword(self, title, desc):
        """匹配用户专属关注词（最高优先级直通）"""
        with self.lock:
            if not self.keywords:
                return None
            t = (title or "").lower()
            d = (desc or "").lower()
            for kw in self.keywords:
                if kw and (kw.lower() in t or kw.lower() in d):
                    return kw
        return None

    def load_seen_ids(self):
        if os.path.exists(SEEN_IDS_FILE):
            try:
                with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[{datetime.now()}] 读取 seen_ids.json 异常: {e}", flush=True)
        return []

    def remember_seen_id(self, unique_id):
        with self.lock:
            if unique_id in self.seen_ids:
                return False
            self.seen_ids.add(unique_id)
            self.seen_id_order.append(unique_id)
            return True

    def save_seen_ids(self):
        with self.lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            if len(self.seen_id_order) > MAX_SEEN_IDS:
                self.seen_id_order = self.seen_id_order[-MAX_SEEN_IDS:]
                self.seen_ids = set(self.seen_id_order)
            with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.seen_id_order, f, ensure_ascii=False)

    def load_seen_msgs(self):
        if os.path.exists(SEEN_MSGS_FILE):
            try:
                with open(SEEN_MSGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[{datetime.now()}] 读取 seen_msgs.json 异常: {e}", flush=True)
        return []

    def remember_seen_msg(self, msg_id):
        with self.lock:
            if msg_id in self.seen_msgs:
                return False
            self.seen_msgs.add(msg_id)
            self.seen_msg_order.append(msg_id)
            return True

    def save_seen_msgs(self):
        with self.lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            if len(self.seen_msg_order) > MAX_SEEN_MSGS:
                self.seen_msg_order = self.seen_msg_order[-MAX_SEEN_MSGS:]
                self.seen_msgs = set(self.seen_msg_order)
            with open(SEEN_MSGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.seen_msg_order, f, ensure_ascii=False)

    def load_sbsb_events(self):
        if os.path.exists(SBSB_EVENTS_FILE):
            try:
                with open(SBSB_EVENTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def remember_sbsb_event(self, kind, event_id):
        if kind not in self.sbsb_events or event_id in self.sbsb_events[kind]:
            return False
        self.sbsb_events[kind].add(event_id)
        self.sbsb_event_order[kind].append(event_id)
        return True

    def save_sbsb_events(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        for kind in ("lottery", "redpacket"):
            if len(self.sbsb_event_order[kind]) > MAX_SBSB_EVENTS:
                self.sbsb_event_order[kind] = self.sbsb_event_order[kind][-MAX_SBSB_EVENTS:]
                self.sbsb_events[kind] = set(self.sbsb_event_order[kind])
        data = {
            "initialized": self.sbsb_events_initialized,
            "lottery": self.sbsb_event_order["lottery"],
            "redpacket": self.sbsb_event_order["redpacket"],
        }
        with open(SBSB_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _telegram_call(self, method, payload, retries=0):
        api_url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(retries + 1):
            req = urllib.request.Request(api_url, data=data, headers={
                "Content-Type": "application/json", "User-Agent": APP_USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=12) as response:
                    result = json.loads(response.read(65536).decode("utf-8"))
                    accepted = response.status == 200 and result.get("ok") is True
                    return accepted and (method != "deleteMessage" or result.get("result") is True)
            except urllib.error.HTTPError as exc:
                if method == "editMessageText" and exc.code == 400:
                    try:
                        result = json.loads(exc.read(65536).decode("utf-8"))
                        if str(result.get("description", "")).startswith("Bad Request: message is not modified"):
                            return True
                    except Exception:
                        pass
                print(f"[{datetime.now()}] TG {method} HTTP {exc.code}", flush=True)
                exc.close()
                if exc.code != 429:
                    break
            except Exception as exc:
                print(f"[{datetime.now()}] TG {method} 未确认: {type(exc).__name__}", flush=True)
            if attempt < retries:
                time.sleep(2)
        return False

    def send_msg(self, chat_id, text, reply_markup=None, disable_preview=True, retries=2):
        chunks = split_html(text)
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": disable_preview}
            markup = valid_markup(reply_markup) if index == len(chunks) - 1 else None
            if markup:
                payload["reply_markup"] = markup
            if not self._telegram_call("sendMessage", payload, retries=retries):
                return False
        return True

    def edit_msg_text(self, chat_id, message_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": fit_html(text),
                   "parse_mode": "HTML", "reply_markup": valid_markup(reply_markup) or {"inline_keyboard": []}}
        return self._telegram_call("editMessageText", payload)

    def delete_msg(self, chat_id, message_id):
        if not self.bot_token or not chat_id or not message_id:
            return False
        return self._telegram_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id, text):
        if not callback_query_id:
            return False
        return self._telegram_call("answerCallbackQuery", {"callback_query_id": callback_query_id,
                                   "text": tg_clip(tg_plain(text), 180), "show_alert": False})


SBSB_CHECKIN_URL = "https://sb.sb/checkin/"


class SbsbSigninPageParser(HTMLParser):
    """Read the official form and counters without borrowing another form's CSRF."""

    def __init__(self, page_html):
        super().__init__(convert_charrefs=True)
        self.form = None
        self.forms = []
        self.button = None
        self.capture = None
        self.streak = None
        self.total = None
        self.stat_value = None
        self.signed = False
        self.has_captcha = False
        self.cap_endpoint = None
        self.feed(page_html)
        checkin = self.checkin_form()
        self.cap_endpoint = checkin.get("cap_endpoint") if checkin else None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "form":
            action = urllib.parse.urljoin(SBSB_CHECKIN_URL, attrs.get("action", ""))
            self.form = {"action": action, "csrf": None, "cap_endpoint": None}
        elif tag == "input" and self.form is not None and attrs.get("name") == "_csrf":
            self.form["csrf"] = attrs.get("value")
        elif tag == "button":
            self.button = {"disabled": "disabled" in attrs, "text": "", "classes": classes}
        elif tag == "cap-widget":
            self.has_captcha = True
            if self.form is not None:
                self.form["cap_endpoint"] = attrs.get("data-cap-api-endpoint")
        for field in ("signin-streak-num", "signin-stat-value", "signin-stat-label"):
            if field in classes:
                self.capture = {"tag": tag, "field": field, "text": ""}
                break

    def handle_data(self, data):
        if self.button is not None:
            self.button["text"] += data
        if self.capture is not None:
            self.capture["text"] += data

    def handle_endtag(self, tag):
        if tag == "form" and self.form is not None:
            self.forms.append(self.form)
            self.form = None
        if tag == "button" and self.button is not None:
            if (self.button["disabled"] and "btn-post" in self.button["classes"]
                    and self.button["text"].strip() == "今日已签到"):
                self.signed = True
            self.button = None
        if self.capture is not None and tag == self.capture["tag"]:
            value = self.capture["text"].strip()
            field = self.capture["field"]
            if field == "signin-streak-num":
                self.streak = int(value) if re.fullmatch(r"\d+", value) else None
            elif field == "signin-stat-value":
                self.stat_value = int(value) if re.fullmatch(r"\d+", value) else None
            elif value == "累计签到":
                self.total = self.stat_value
            self.capture = None

    def checkin_form(self):
        return next((form for form in self.forms if form["action"] == SBSB_CHECKIN_URL), None)


def run_sbsb_browser_checkin(cookie, cap_endpoint):
    try:
        from app.browser_signin import run_checkin
    except ModuleNotFoundError:
        from browser_signin import run_checkin
    return run_checkin(cookie, cap_endpoint=cap_endpoint)


def do_sbsb_signin(cookie, allow_submit=True):
    """Return independent query, submission and optional-field evidence."""
    observed_period = period_key(datetime.now(CST))
    attempted, accepted = False, False
    browser_in_progress = False
    confirmation_recovered = False
    browser_stage = None

    def failure(outcome, code, *, stop=False):
        messages = {
            "not_configured": "未配置烧饼登录凭据",
            "login_required": "论坛已将请求重定向到登录页",
            "unsupported_form": "未找到可确认的官方签到表单，未提交",
            "query_unavailable": "官方查询暂未完成",
            "unsigned": "官方查询确认本轮尚未签到；本次没有提交",
            "submission_unconfirmed": "程序已提交请求，但最终签到结果尚未确认",
            "automation_stopped": "自动流程未完成，本轮停止自动提交，请查看论坛状态",
        }
        return {"success": False, "already": False, "outcome": outcome,
                "period": observed_period, "error_code": code,
                "needs_human": stop, "submission_attempted": attempted,
                "submission_possible": browser_in_progress,
                "browser_stage": browser_stage,
                "submission_accepted": accepted, "msg": messages[outcome]}

    if not cookie:
        return failure("not_configured", "cookie_not_configured")
    if "=" not in cookie:
        cookie = f"__Host-bbs_session={cookie}"
    headers = {"User-Agent": APP_USER_AGENT, "Cookie": cookie,
               "Accept": "text/html,application/xhtml+xml", "Referer": SBSB_CHECKIN_URL,
               "Origin": "https://sb.sb", "Cache-Control": "no-cache"}

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    class QueryError(Exception):
        def __init__(self, code):
            self.code = code

    opener = urllib.request.build_opener(NoRedirectHandler)

    def read_response(req):
        try:
            response = opener.open(req, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = getattr(response, "status", getattr(response, "code", 200))
            location = response.headers.get("Location", "")
            body = response.read(1500001)
        if len(body) > 1500000:
            raise QueryError("page_too_large")
        if location:
            target = urllib.parse.urljoin(req.full_url, location)
            parts = urllib.parse.urlsplit(target)
            if parts.scheme == "https" and parts.netloc == "sb.sb" and parts.path in ("/login", "/login/"):
                raise QueryError("login_required")
            if target != req.full_url:
                raise QueryError("unexpected_redirect")
        return status, body.decode("utf-8", errors="replace")

    try:
        status, source = read_response(urllib.request.Request(SBSB_CHECKIN_URL, headers=headers))
        observed_period = period_key(datetime.now(CST))
        if status != 200:
            return failure("query_unavailable", f"query_http_{status}")
        page = SbsbSigninPageParser(source)
        already = page.signed
        if not already:
            if not allow_submit:
                return failure("unsigned", "query_only_unsigned")
            form = page.checkin_form()
            if not form or not form["csrf"]:
                return failure("unsupported_form", "checkin_form_unavailable", stop=True)
            submission_period = observed_period
            if os.environ.get("SBSB_BROWSER_SIGNIN", "").lower() in ("1", "true", "yes"):
                # If the browser call itself aborts without returning evidence,
                # conservatively retain the durable pre-call submission guard.
                browser_in_progress = True
                browser = run_sbsb_browser_checkin(cookie, page.cap_endpoint)
                browser_in_progress = False
                stage = browser.get("browser_stage", "unknown")
                if not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z_]{1,100}", stage):
                    stage = "unknown"
                browser_stage = stage
                print(f"[{datetime.now()}] 🍪 浏览器签到阶段: {stage}", flush=True)
                attempted = browser.get("submission_attempted") is True or stage.startswith("submitted_")
                accepted = browser.get("submission_accepted") is True or (
                    browser.get("success") is True and stage == "submitted_confirmed" and not browser.get("already"))
                attempted = attempted or accepted
                if not browser.get("success") and not accepted:
                    return failure("submission_unconfirmed" if attempted else "automation_stopped", stage, stop=True)
                confirmation_recovered = not browser.get("success") and accepted
                already = browser.get("already") is True
            else:
                post_headers = dict(headers, **{"Content-Type": "application/x-www-form-urlencoded"})
                data = urllib.parse.urlencode({"_csrf": form["csrf"], "message": ""}).encode("utf-8")
                attempted = True
                post_status, _ = read_response(urllib.request.Request(SBSB_CHECKIN_URL, data=data, headers=post_headers))
                accepted = post_status in (302, 303)
                if post_status not in (200, 302, 303):
                    return failure("submission_unconfirmed", f"submission_http_{post_status}", stop=True)
            status, source = read_response(urllib.request.Request(SBSB_CHECKIN_URL, headers=headers))
            observed_period = period_key(datetime.now(CST))
            page = SbsbSigninPageParser(source)
            if status != 200 or not page.signed:
                return failure("submission_unconfirmed", "official_confirmation_unavailable", stop=True)
            # A day boundary or a response without acceptance cannot attribute this
            # query to our submission, even if the account is now signed.
            already = already or not accepted or observed_period != submission_period
            if confirmation_recovered and not already:
                print(f"[{datetime.now()}] 🍪 浏览器签到阶段: submitted_confirmed（官方只读查询补确认）", flush=True)
    except QueryError as exc:
        if exc.code == "login_required" and not attempted:
            return failure("login_required", exc.code, stop=True)
        return failure("submission_unconfirmed" if attempted or accepted or browser_in_progress else "query_unavailable", exc.code,
                       stop=attempted or accepted or browser_in_progress)
    except Exception:
        return failure("submission_unconfirmed" if attempted or accepted or browser_in_progress else "query_unavailable",
                       "confirmation_query_failed" if attempted or accepted or browser_in_progress else "official_query_failed",
                       stop=attempted or accepted or browser_in_progress)

    result = {"success": True, "already": already, "period": observed_period,
              "outcome": "query_confirmed" if already else "submitted_confirmed",
              "confirmed_at": datetime.now(CST).isoformat(), "needs_human": False,
              "submission_attempted": attempted, "submission_accepted": accepted,
              "browser_stage": browser_stage,
              "confirmation_recovered": confirmation_recovered and not already,
              "msg": "本轮已签到（查询确认）" if already else "签到成功",
              "consecutive_days": f"{page.streak} 天" if page.streak and page.streak > 0 else UNKNOWN,
              "total_days": f"{page.total} 天" if page.total and page.total > 0 else UNKNOWN,
              **dict.fromkeys(("total_points", "exp", "level", "signin_time"), UNKNOWN)}
    try:
        status, points = read_response(urllib.request.Request("https://sb.sb/points/", headers=headers))
        if status == 200:
            result.update(PointsParser(points).values(observed_period))
    except Exception:
        pass  # An optional asset query cannot negate confirmed business success.
    return result


def handle_callback_query(bot, query):
    bot.handle_callback(query)


def handle_command_or_text(bot, chat_id, text, message_id=None):
    try:
        bot.handle_command(chat_id, text, message_id, normalize_chat_endpoint, LEGACY_BUILTIN_WORDS)
    except Exception as exc:
        # User input, API endpoints and exceptions may contain credentials.
        print(f"[{datetime.now()}] 指令处理未完成: {type(exc).__name__}", flush=True)
        bot.send_msg(chat_id, "⚠️ 操作未完成，请重新查询当前状态。")


def telegram_polling_thread(bot):
    offset = 0
    print(f"[{datetime.now()}] 🤖 Telegram 交互指令与按钮监听器已启动...", flush=True)
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot.bot_token}/getUpdates?offset={offset}&timeout=20"
            req = urllib.request.Request(url, headers={"User-Agent": APP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    for result in data.get("result", []):
                        offset = result["update_id"] + 1
                        
                        if "callback_query" in result:
                            try:
                                handle_callback_query(bot, result["callback_query"])
                            except Exception as ce:
                                print(f"[{datetime.now()}] 按钮处理异常: {ce}", flush=True)

                        elif "message" in result:
                            msg = result["message"]
                            from_user = str(msg.get("chat", {}).get("id", ""))
                            text = msg.get("text", "")
                            message_id = msg.get("message_id")
                            if from_user == bot.admin_chat_id and text:
                                try:
                                    handle_command_or_text(bot, from_user, text, message_id=message_id)
                                except Exception as ce:
                                    print(f"[{datetime.now()}] 指令处理异常: {ce}", flush=True)
        except urllib.error.HTTPError as he:
            if he.code == 429:
                time.sleep(3)
            else:
                time.sleep(1)
        except Exception as e:
            time.sleep(1)


def fetch_rss(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (CommunityFeed/{APP_VERSION})"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return ET.fromstring(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[{datetime.now()}] RSS 拉取异常 ({url}): {e}", flush=True)
        return None


def parse_sbsb_badged_topics(page_html, badge_kind):
    """从烧饼论坛主题列表解析带官方抽奖或红包徽标的帖子。"""
    badge_classes = {
        "lottery": "lottery-badge",
        "redpacket": "redpacket-badge",
    }
    if badge_kind not in badge_classes:
        raise ValueError(f"不支持的烧饼标记类型: {badge_kind}")

    blocks = re.findall(
        r'<li[^>]*class=["\'][^"\']*post-item[^"\']*["\'][^>]*>'
        r'(.*?)(?=<li[^>]*class=["\'][^"\']*post-item|</ul>)',
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    topics = []
    seen_links = set()
    for block in blocks:
        badge_class = re.escape(badge_classes[badge_kind])
        if not re.search(rf'class=["\'][^"\']*{badge_class}', block, re.IGNORECASE):
            continue

        title_match = re.search(
            r'<a[^>]*class=["\'][^"\']*post-title[^"\']*["\'][^>]*'
            r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            continue

        link = urllib.parse.urljoin("https://sb.sb/", html.unescape(title_match.group(1)))
        if link in seen_links:
            continue
        seen_links.add(link)

        title = html.unescape(re.sub(r'<[^>]+>', ' ', title_match.group(2)))
        title = re.sub(r'\s+', ' ', title).strip()

        author_match = re.search(
            r'<a[^>]*href=["\']/u/\d+/?["\'][^>]*>(.*?)</a>',
            block,
            re.IGNORECASE | re.DOTALL,
        )
        author = "烧饼用户"
        if author_match:
            author = html.unescape(re.sub(r'<[^>]+>', ' ', author_match.group(1)))
            author = re.sub(r'\s+', ' ', author).strip() or author

        category_match = re.search(
            r'<a[^>]*href=["\']/go/[^"\']+/?["\'][^>]*>(.*?)</a>',
            block,
            re.IGNORECASE | re.DOTALL,
        )
        category = "综合"
        if category_match:
            category = html.unescape(re.sub(r'<[^>]+>', ' ', category_match.group(1)))
            category = re.sub(r'\s+', ' ', category).strip() or category

        topics.append({
            "title": title,
            "link": link,
            "author": author,
            "category": category,
            "badge_kind": badge_kind,
        })
    return topics


def parse_sbsb_redpacket_topics(page_html):
    """兼容旧调用：解析官方红包徽标。"""
    return parse_sbsb_badged_topics(page_html, "redpacket")


def fetch_sbsb_topic_page(path="/", cookie=""):
    url = urllib.parse.urljoin("https://sb.sb/", path)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"Mozilla/5.0 (CommunityFeed/{APP_VERSION})",
            "Cookie": cookie,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[{datetime.now()}] 烧饼官方标记页面拉取异常 ({url}): {e}", flush=True)
        return None


def deliver_public_match(bot, source_name, source_icon, category, title, author, desc, link, hit_type, hit_reason):
    sid = {"NodeSeek": "nodeseek", "烧饼论坛": "sbsb"}.get(source_name)
    if getattr(bot, "paused", False) or (sid and not getattr(bot, "source_states", {}).get(sid, True)):
        return False
    bot.total_hit += 1
    hit_badges = {
        "lottery": "🎁 [抽奖]",
        "welfare": "🎉 [福利]",
        "redpacket": "🧧 [红包]",
        "custom": "🎯 [自定义关注]",
    }
    hit_badge = hit_badges.get(hit_type, "🎁 [抽奖/福利]")
    print(f"[{datetime.now()}] {hit_badge} 命中: [{source_name}] [{category}] {title} ({hit_reason})", flush=True)

    msg, markup = format_activity(source_name, hit_type, title, author, category, desc, link, hit_reason)
    delivered = bot.send_msg(bot.admin_chat_id, msg, reply_markup=markup)
    bot.record_stat("delivery_success" if delivered else "delivery_errors")
    if not delivered:
        print(f"[{datetime.now()}] ❌ Telegram 推送失败: [{source_name}] {title}", flush=True)
    return delivered


def quarantine_nodeseek_ai_failure(bot, unique_id, record, now_ts):
    """Keep exhausted posts for review without retrying or silently accepting them."""
    bot.nodeseek_ai_review[unique_id] = {
        "id": unique_id,
        "title": record.get("title", ""),
        "link": record.get("link", ""),
        "queued_at": record.get("queued_at"),
        "attempts": record.get("attempts", 0),
        "last_error": record.get("last_error", ""),
        "quarantined_at": now_ts,
    }
    bot.nodeseek_ai_pending.pop(unique_id, None)
    bot.remember_seen_id(unique_id)
    bot.save_nodeseek_ai_state()
    bot.save_seen_ids()
    print(
        f"[{datetime.now()}] ⚠️ NodeSeek AI 连续判定失败，转待人工复核: "
        f"{record.get('title', '')} ({record.get('attempts', 0)} 次)",
        flush=True,
    )


def process_nodeseek_ai_queue(bot):
    """Classify queued NodeSeek posts and quarantine exhausted retries."""
    if not bot.ai_is_ready() or not bot.is_source_enabled("nodeseek"):
        return
    try:
        classifier = bot.build_ai_classifier()
    except ValueError as exc:
        print(f"[{datetime.now()}] ⚠️ NodeSeek AI 配置无效: {exc}", flush=True)
        return

    processed = 0
    now_ts = int(time.time())
    for unique_id, record in list(bot.nodeseek_ai_pending.items()):
        if processed >= MAX_AI_POSTS_PER_CYCLE:
            break
        if int(record.get("attempts", 0)) >= MAX_AI_RETRY_ATTEMPTS:
            quarantine_nodeseek_ai_failure(bot, unique_id, record, now_ts)
            continue
        if int(record.get("next_retry_at", 0)) > now_ts:
            continue
        processed += 1
        post = {
            "source": "NodeSeek",
            "category": record.get("category", "综合"),
            "title": record.get("title", ""),
            "author": record.get("author", "未知"),
            "description": record.get("description", ""),
            "link": record.get("link", ""),
        }
        bot.record_stat("ai_requests")
        try:
            result = classifier.classify(post, accept_threshold=bot.ai_accept_threshold)
        except (AIClassificationError, ValueError) as exc:
            attempts = int(record.get("attempts", 0)) + 1
            record["attempts"] = attempts
            record["last_error"] = clean_for_ai(str(exc), 180)
            bot.record_stat("ai_errors")
            if attempts >= MAX_AI_RETRY_ATTEMPTS:
                quarantine_nodeseek_ai_failure(bot, unique_id, record, now_ts)
                continue
            retry_seconds = min(1800, 60 * (2 ** min(attempts - 1, 5)))
            record["next_retry_at"] = now_ts + retry_seconds
            bot.save_nodeseek_ai_state()
            print(
                f"[{datetime.now()}] ⚠️ NodeSeek AI 判定失败，{retry_seconds}s 后重试: "
                f"{record.get('title', '')} ({record['last_error']})",
                flush=True,
            )
            continue

        bot.record_stat("ai_classified")
        if result.get("reviewed"):
            bot.record_stat("ai_second_reviews")
        decision = result.get("decision")
        if decision == "giveaway":
            sub_type = result.get("sub_type") or "lottery"
            bot.record_stat("ai_giveaway_hits")
            bot.record_stat("lottery_hits")
            if sub_type == "welfare":
                bot.record_stat("welfare_hits")
            elif sub_type == "redpacket":
                bot.record_stat("redpacket_hits")
            evidence = "；".join(result.get("evidence", [])[:2])
            default_reason = "AI 语义判定为有效抽奖" if sub_type == "lottery" else "AI 语义判定为有效福利活动"
            reason = result.get("reason", default_reason)
            if evidence:
                reason = f"{reason}；证据：{evidence}"
            if not bot.paused:
                deliver_public_match(
                    bot,
                    "NodeSeek",
                    "🌐",
                    record.get("category", "综合"),
                    record.get("title", ""),
                    record.get("author", "未知"),
                    record.get("description", ""),
                    record.get("link", ""),
                    sub_type,
                    f"AI 双阶段语义判定（置信度 {result.get('confidence', 0):.2f}）：{reason}",
                )
        elif decision == "uncertain":
            bot.record_stat("ai_uncertain")

        bot.nodeseek_ai_history.append({
            "id": unique_id,
            "title": record.get("title", ""),
            "link": record.get("link", ""),
            "decision": decision,
            "sub_type": result.get("sub_type"),
            "confidence": result.get("confidence", 0),
            "reason": result.get("reason", ""),
            "reviewed": bool(result.get("reviewed")),
            "classified_at": int(time.time()),
        })
        bot.nodeseek_ai_pending.pop(unique_id, None)
        bot.remember_seen_id(unique_id)
        bot.save_nodeseek_ai_state()
        bot.save_seen_ids()
        sub_type_label = f" ({result.get('sub_type')})" if result.get('sub_type') else ""
        print(
            f"[{datetime.now()}] 🧠 [NodeSeek AI] {decision}{sub_type_label} "
            f"({result.get('confidence', 0):.2f}): {record.get('title', '')}",
            flush=True,
        )


def execute_sbsb_signin(bot, chat_id=None, *, allow_submit=False, automatic=False):
    """Serialize scheduler/manual work and commit business evidence before notice.

    A failed notice never makes a completed sign-in eligible for another POST.
    Old completion-only files are upgraded by a GET, without trusting their
    ambiguous midnight date or generating a duplicate success notification.
    """
    target = chat_id if chat_id is not None else bot.admin_chat_id
    store = SigninStore(CHECKIN_STATE_FILE)
    try:
        with store.locked() as state:
            now = datetime.now(CST)
            key = period_key(now)
            completed = signin_confirmed(state, key)
            if automatic and (completed or not sbsb_checkin_due(now, "")):
                return None
            legacy = "version" not in state and bool(state.get("last_date"))
            legacy_last_date = state.get("last_date") if legacy else None
            can_submit = allow_submit and not completed and not signin_blocked(state, key)
            guard = state.get("submission_guard", {})
            if guard.get("period") == key:
                # A process may have exited after a POST but before confirmation.
                # Recover by GET only, never by repeating that uncertain POST.
                can_submit = False
                state["blocked_period"] = state["manual_required_date"] = key
            if legacy:
                can_submit = False
            if can_submit:
                state["submission_guard"] = {"period": key, "started_at": now.isoformat()}
                store.save(state)
            result = do_sbsb_signin(bot.sbsb_cookie, allow_submit=can_submit)
            now = datetime.now(CST)
            state = record_signin_result(state, result, now)
            if result.get("success") or not any(result.get(k) for k in (
                    "submission_attempted", "submission_possible", "submission_accepted")):
                # Only this call's guard can be cleared by evidence of no POST.
                # A read-only recovery cannot clear a previous uncertain attempt.
                if can_submit or result.get("success"):
                    state.pop("submission_guard", None)
            # The caller may have crossed 08:00. Only the period confirmed by the
            # official read is completed; a previous period cannot skip the new one.
            result_period = result.get("period") or key
            store.save(state)
            text, markup = format_signin(result, state, now, automatic=automatic)
            kind = "success" if result.get("success") else "warning"
            previous_notice = state.get("notice", {})
            signature = kind + ":" + state["last_query"]["outcome"] + (":blocked" if signin_blocked(state, result_period) else "")
            notify = not automatic or (
                previous_notice.get("period") != result_period or previous_notice.get("signature") != signature)
            if automatic and legacy and result.get("success") and legacy_last_date == result_period:
                # Existing deployments already emitted their daily card. There
                # is no acknowledgement evidence, so do not label it accepted.
                state["notice"] = {"period": result_period, "kind": kind, "status": "legacy_unknown", "signature": signature}
                store.save(state)
                notify = False
            if notify:
                state["notice"] = {"period": result_period, "kind": kind, "status": "pending",
                                   "attempted_at": now.isoformat(), "signature": signature}
                if kind == "warning":
                    state["last_error_date"] = result_period
                store.save(state)
                sent = bot.send_msg(target, text, reply_markup=markup)
                state["notice"]["status"] = "accepted" if sent is True else "unconfirmed"
                store.save(state)
            if automatic:
                outcome = "提交成功" if result.get("success") and not result.get("already") else (
                    "查询确认" if result.get("success") else result.get("outcome", "query_unavailable"))
                notice = state.get("notice", {}).get("status", "not_requested")
                code = result.get("error_code", "")
                print(f"[{datetime.now()}] 🍪 烧饼签到 {outcome}；安全码: {code or 'none'}；通知接口状态: {notice}", flush=True)
            return result
    except StateError:
        print(f"[{datetime.now()}] 签到状态读取异常，未提交", flush=True)
        if not automatic:
            bot.send_msg(target, "⚠️ 签到记录读取异常，未执行自动提交；请先核对记录。")
        return {"success": False, "outcome": "state_unavailable"}


def sbsb_checkin_due(now_cst, last_date):
    return signin_due(now_cst, last_date)


def sbsb_checkin_scheduler_thread(bot):
    if not bot.sbsb_cookie:
        return
    print(f"[{datetime.now()}] ⏰ 烧饼签到调度器已就绪（北京时间08:05）", flush=True)
    while True:
        try:
            execute_sbsb_signin(bot, allow_submit=True, automatic=True)
        except Exception as exc:
            print(f"[{datetime.now()}] 签到调度器异常: {type(exc).__name__}", flush=True)
        time.sleep(300)


def daily_quality_reporter_thread(bot):
    """每日算法过滤与运行成功率总结推送线程 (北京时间每天 22:00 执行)"""
    print(f"[{datetime.now()}] 📊 每日算法成功率与运行日报调度器已就绪 (目标时间: 北京时间 22:00)...", flush=True)

    while True:
        try:
            tz_cst = timezone(timedelta(hours=8))
            now_cst = datetime.now(tz_cst)
            today_str = now_cst.strftime("%Y-%m-%d")

            if now_cst.hour >= 22 and bot.last_daily_report_date != today_str:
                bot.last_daily_report_date = today_str
                print(f"[{datetime.now()}] 📊 生成并推送每日算法与监控健康度报告...", flush=True)
                report_text = bot.generate_daily_report_card()
                bot.send_msg(bot.admin_chat_id, report_text, reply_markup=bot.report_markup())

        except Exception as e:
            print(f"[{datetime.now()}] 每日报告调度异常: {e}", flush=True)

        time.sleep(300)


def parse_sbsb_notifications(html_text, default_uid=""):
    """
    解析烧饼论坛 (sb.sb) 互动通知页面 HTML。

    优化项：
    1. 精确匹配闭合的 </li>，识别 .notification-read-divider 分割线并标定已读状态。
    2. 兼容各种 <time> 属性（如 data-abs="date", class 等），未匹配时严禁兜底“刚刚”。
    3. 提取 /u/{uid}/ 中的用户名，剥离 SVG 等内部图标，杜绝误用 post-title。
    4. 业务去重主键解耦时间字段：优先提取 link/block 中的 reply_id 生成 notif:reply_{id}，
       无 reply_id 时使用路径与通知关键内容的 SHA-256 哈希生成 notif:hash_{digest}。
    """
    if not html_text:
        return []

    # 1. 检测已读分割线位置
    divider_m = re.search(
        r'<[^>]*class=[\"\'][^\"\']*notification-read-divider[^\"\']*[\"\'][^>]*>',
        html_text,
        re.IGNORECASE
    )
    divider_pos = divider_m.start() if divider_m else None

    # 2. 精确提取闭合的 <li class="...notification-item...">...</li>
    item_pattern = re.compile(
        r'<li\b([^>]*class=[\"\'][^\"\']*notification-item[^\"\']*[\"\'][^>]*)>(.*?)</li>',
        re.DOTALL | re.IGNORECASE
    )

    notifications = []

    for match in item_pattern.finditer(html_text):
        li_attrs = match.group(1)
        block = match.group(2)
        item_start = match.start()

        # 判断是否已读：位于已读分割线之后，或 li 属性包含 read class
        is_read = False
        if divider_pos is not None and item_start > divider_pos:
            is_read = True
        else:
            class_m = re.search(r'class=[\"\']([^\"\']+)[\"\']', li_attrs, re.IGNORECASE)
            if class_m:
                classes = set(class_m.group(1).split())
                if "read" in classes and "unread" not in classes:
                    is_read = True

        # 3. 提取用户名：匹配 /u/{uid}/ 链接，剥离内部 SVG 图标，杜绝误用 post-title
        user = ""
        user_link_match = re.search(
            r'<a\b[^>]*href=[\"\'](?:https?://[^/]+)?/u/([^/\"\'\?#]+)/?[\"\'][^>]*>(.*?)</a>',
            block,
            re.DOTALL | re.IGNORECASE
        )
        if user_link_match:
            uid_or_name = user_link_match.group(1)
            raw_user_html = user_link_match.group(2)
            no_svg = re.sub(r'<svg\b[^>]*>.*?</svg>', '', raw_user_html, flags=re.DOTALL | re.IGNORECASE)
            clean_user = re.sub(r'<[^>]+>', '', no_svg).strip()
            if clean_user:
                user = clean_user
            else:
                attr_m = re.search(r'(?:title|aria-label|data-username)=[\"\']([^\"\']+)[\"\']', user_link_match.group(0), re.IGNORECASE)
                user = attr_m.group(1).strip() if attr_m else (uid_or_name or "烧饼用户")

        if not user:
            # 备用匹配非 post-title 的 user/author 类链接
            alt_m = re.search(
                r'<a\b(?![^>]*class=[\"\'][^\"\']*post-title[^\"\']*[\"\'])[^>]*class=[\"\'][^\"\']*(?:user|author)[^\"\']*[\"\'][^>]*>(.*?)</a>',
                block,
                re.DOTALL | re.IGNORECASE
            )
            if alt_m:
                no_svg = re.sub(r'<svg\b[^>]*>.*?</svg>', '', alt_m.group(1), flags=re.DOTALL | re.IGNORECASE)
                clean_alt = re.sub(r'<[^>]+>', '', no_svg).strip()
                if clean_alt:
                    user = clean_alt

        if not user:
            user = "烧饼用户"

        # 4. 提取通知类型
        kind_match = re.search(r'<span[^>]*class=[\"\'][^\"\']*notification-kind[^\"\']*[\"\'][^>]*>([^<]+)</span>', block, re.IGNORECASE)
        kind = kind_match.group(1).strip() if kind_match else "提醒"

        # 5. 提取时间：兼容 data-abs="date"、class 等属性；失配时严禁兜底“刚刚”
        time_match = re.search(r'<time\b([^>]*)>(.*?)</time>', block, re.DOTALL | re.IGNORECASE)
        if time_match:
            time_attrs = time_match.group(1)
            time_content = time_match.group(2)
            dt_m = re.search(r'datetime=[\"\']([^\"\']+)[\"\']', time_attrs, re.IGNORECASE)
            iso_time = dt_m.group(1).strip() if dt_m else ""
            rel_time = re.sub(r'<[^>]+>', '', time_content).strip()
        else:
            iso_time = ""
            rel_time = ""  # 匹配失败时严禁兜底赋值“刚刚”

        # 6. 提取通知内容
        content_match = re.search(r'<div[^>]*class=[\"\'][^\"\']*notification-content[^\"\']*[\"\'][^>]*>(.*?)</div>', block, re.DOTALL | re.IGNORECASE)
        if content_match:
            content_raw = content_match.group(1)
            content = re.sub(r'<[^>]+>', ' ', content_raw)
            content = re.sub(r'\s+', ' ', content).strip()
        else:
            content = "收到一条新的互动提醒"

        # 7. 提取直达链接与 reply_id
        link_match = (
            re.search(r'<a[^>]*class=[\"\'][^\"\']*notification-reply-action[^\"\']*[\"\'][^>]*href=[\"\']([^\"\']+)[\"\']', block, re.IGNORECASE)
            or re.search(r'<a[^>]*href=[\"\'](/t/\d+/[^\"\']*)[\"\']', block, re.IGNORECASE)
            or re.search(r'href=[\"\'](/t/\d+/[^\"\']*)[\"\']', block, re.IGNORECASE)
            or re.search(r'href=[\"\'](/t/\d+/?[\"\']?)', block, re.IGNORECASE)
        )
        if link_match:
            raw_href = link_match.group(1)
            if raw_href.startswith("http://") or raw_href.startswith("https://"):
                link = raw_href
            else:
                if not raw_href.startswith("/"):
                    raw_href = "/" + raw_href
                link = f"https://sb.sb{raw_href}"
        else:
            link = f"https://sb.sb/u/{default_uid}/?tab=notifications" if default_uid else "https://sb.sb/"

        # 提取 reply_id
        reply_id = None
        reply_m = (
            re.search(r'reply[-_]?id=(\d+)', link, re.IGNORECASE)
            or re.search(r'reply[-_](\d+)', link, re.IGNORECASE)
            or re.search(r'[?&]reply=(\d+)', link, re.IGNORECASE)
            or re.search(r'/reply/(\d+)', link, re.IGNORECASE)
            or re.search(r'#(?:reply[-_]?|r)?(\d+)', link, re.IGNORECASE)
        )
        if reply_m:
            reply_id = reply_m.group(1)

        if not reply_id:
            data_reply_m = re.search(r'data-reply(?:-|_)?id=[\"\'](\d+)[\"\']', li_attrs + " " + block, re.IGNORECASE)
            if data_reply_m:
                reply_id = data_reply_m.group(1)

        # 8. 构造去重主键（彻底与时间解耦）
        if reply_id:
            unique_key = f"notif:reply_{reply_id}"
        else:
            path = urllib.parse.urlparse(link).path or link
            raw_key_material = f"{path}|{user}|{kind}|{content}"
            digest = hashlib.sha256(raw_key_material.encode("utf-8")).hexdigest()
            unique_key = f"notif:hash_{digest}"

        notifications.append({
            "user": user,
            "kind": kind,
            "content": content,
            "iso_time": iso_time,
            "rel_time": rel_time,
            "link": link,
            "reply_id": reply_id,
            "unique_key": unique_key,
            "is_read": is_read,
            "block": block,
        })

    return notifications


class SbsbIdentityError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fetch_sbsb_current_uid(cookie, opener):
    """Read fresh official navigation; never infer identity from post authors."""
    if not cookie:
        raise SbsbIdentityError("identity_unconfirmed")
    cookie = cookie if "=" in cookie else f"__Host-bbs_session={cookie}"
    req = urllib.request.Request("https://sb.sb/", headers={
        "User-Agent": APP_USER_AGENT, "Cookie": cookie,
        "Referer": "https://sb.sb/", "Cache-Control": "no-cache",
    })
    try:
        with opener.open(req, timeout=12) as response:
            status = getattr(response, "status", getattr(response, "code", 200))
            if status != 200:
                raise SbsbIdentityError("identity_query_failed")
            raw = response.read(1500001)
            if len(raw) > 1500000:
                raise SbsbIdentityError("identity_page_too_large")
    except urllib.error.HTTPError as exc:
        location = urllib.parse.urlsplit(urllib.parse.urljoin("https://sb.sb/", exc.headers.get("Location", "")))
        login = (exc.code in (301, 302, 303, 307, 308) and location.scheme == "https"
                 and location.netloc == "sb.sb" and location.path in ("/login", "/login/"))
        exc.close()
        raise SbsbIdentityError("login_required" if login else "identity_query_failed") from None
    except (urllib.error.URLError, TimeoutError):
        raise SbsbIdentityError("identity_query_failed") from None
    except SbsbIdentityError:
        raise
    except Exception:
        raise SbsbIdentityError("identity_query_failed") from None
    try:
        uid = TopicPage(raw.decode("utf-8", errors="replace"), "redpacket").uid
    except Exception:
        raise SbsbIdentityError("identity_unconfirmed") from None
    if not re.fullmatch(r"[1-9]\d*", uid):
        raise SbsbIdentityError("identity_unconfirmed")
    return uid


def sbsb_private_messages_thread(bot):
    """烧饼论坛互动通知（回复/点赞/提及）与私信轮询线程"""
    if not bot.sbsb_cookie:
        print(f"[{datetime.now()}] ℹ️ 烧饼论坛私信与通知引擎未配置 SBSB_COOKIE，监听保持挂起。", flush=True)
        return

    print(f"[{datetime.now()}] 📬 烧饼论坛 (sb.sb) 互动通知与私信监听引擎已启动...", flush=True)
    first_run = len(bot.seen_msgs) == 0

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirectHandler)

    def warn_login_required():
        now_ts = time.time()
        if not getattr(bot, "paused", False) and now_ts - bot.last_cookie_warn_time > 43200:
            bot.last_cookie_warn_time = now_ts
            bot.send_msg(bot.admin_chat_id, (
                "⚠️ <b>烧饼登录已失效</b>\n\n暂时无法查询私信与互动，请更新登录凭据。"
            ))

    while True:
        try:
            try:
                uid = fetch_sbsb_current_uid(bot.sbsb_cookie, opener)
            except SbsbIdentityError as exc:
                bot.sbsb_uid = None
                print(f"[{datetime.now()}] 获取烧饼 UID 未确认: {exc.code}", flush=True)
                if exc.code == "login_required": warn_login_required()
                time.sleep(DEFAULT_PRIVATE_INTERVAL)
                continue
            if bot.sbsb_uid != uid:
                bot.sbsb_uid = uid
                print(f"[{datetime.now()}] 🆔 成功识别烧饼论坛当前用户 UID: {uid}", flush=True)
            notif_url = f"https://sb.sb/u/{uid}/notifications/"

            # 1. 拉取互动通知页（回复、点亮、提及、支持等）
            req_notif = urllib.request.Request(
                notif_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Cookie": bot.sbsb_cookie,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://sb.sb/"
                }
            )

            try:
                with opener.open(req_notif, timeout=15) as resp_notif:
                    if getattr(resp_notif, "status", getattr(resp_notif, "code", 200)) != 200:
                        raise SbsbIdentityError("notification_query_failed")
                    raw_notif = resp_notif.read(1500001)
                    if len(raw_notif) > 1500000:
                        raise SbsbIdentityError("notification_page_too_large")
                    html_notif = raw_notif.decode("utf-8", errors="replace")
                if TopicPage(html_notif, "redpacket").uid != uid:
                    bot.sbsb_uid = None
                    raise SbsbIdentityError("notification_identity_changed")
            except Exception as fe:
                bot.sbsb_uid = None
                if isinstance(fe, urllib.error.HTTPError):
                    location = urllib.parse.urlsplit(urllib.parse.urljoin("https://sb.sb/", fe.headers.get("Location", "")))
                    if (fe.code in (301, 302, 303, 307, 308) and location.scheme == "https"
                            and location.netloc == "sb.sb" and location.path in ("/login", "/login/")):
                        warn_login_required()
                    fe.close()
                code = fe.code if isinstance(fe, SbsbIdentityError) else type(fe).__name__
                print(f"[{datetime.now()}] 拉取烧饼通知未确认: {code}", flush=True)
                time.sleep(DEFAULT_PRIVATE_INTERVAL)
                continue

            # 解析通知列表
            notifications = parse_sbsb_notifications(html_notif, default_uid=uid)

            for notif in reversed(notifications):
                user = notif["user"]
                kind = notif["kind"]
                content = notif["content"]
                rel_time = notif["rel_time"]
                link = notif["link"]
                unique_key = notif["unique_key"]
                is_read = notif.get("is_read", False)

                if first_run:
                    bot.remember_seen_msg(unique_key)
                    continue

                if is_read:
                    # 识别已读分割线后的历史已读通知，仅记录去重 key，不向 TG 重复推送
                    bot.remember_seen_msg(unique_key)
                    continue

                if bot.remember_seen_msg(unique_key):
                    if getattr(bot, "paused", False):
                        continue
                    bot.total_private_notified += 1
                    bot.record_stat("private_notified")
                    print(f"[{datetime.now()}] 📬 命中烧饼论坛新互动通知: [{kind}] {user} - {content}", flush=True)

                    time_display = rel_time if rel_time else "未知时间"
                    msg_card = (
                        f"💬 <b>烧饼 · {tg_escape(kind, 40)}</b>\n\n"
                        f"<b>{tg_escape(user, 100)}</b>\n"
                        f"{tg_escape(content, 1000)}\n\n"
                        f"论坛显示：{tg_escape(time_display, 80)}"
                    )
                    bot.send_msg(bot.admin_chat_id, msg_card, reply_markup=keyboard([url_button("查看互动", link)]))

            # 2. 拉取私信信箱（1对1 私信消息）
            try:
                req_msg = urllib.request.Request(
                    "https://sb.sb/messages/",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                        "Cookie": bot.sbsb_cookie,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://sb.sb/"
                    }
                )
                resp_msg = opener.open(req_msg, timeout=15)
                html_msg = resp_msg.read().decode("utf-8", errors="ignore")
                threads = re.findall(r'href=["\']/messages/([a-f0-9]{16,64})/?["\']', html_msg)
                unique_threads = list(dict.fromkeys(threads))

                for thread_id in unique_threads:
                    thread_key = f"thread:{thread_id}"
                    if first_run:
                        bot.remember_seen_msg(thread_key)
                        continue

                    if bot.remember_seen_msg(thread_key):
                        if getattr(bot, "paused", False):
                            continue
                        bot.total_private_notified += 1
                        bot.record_stat("private_notified")
                        print(f"[{datetime.now()}] 📬 发现烧饼论坛新私信对话: {thread_id}", flush=True)

                        msg_text = (
                            "✉️ <b>烧饼 · 新私信对话</b>\n\n收到一条新对话，请打开论坛查看。"
                        )
                        bot.send_msg(bot.admin_chat_id, msg_text,
                                     reply_markup=keyboard([url_button("打开对话", f"https://sb.sb/messages/{thread_id}/")]))
            except Exception as me:
                print(f"[{datetime.now()}] 拉取私信异常: {type(me).__name__}", flush=True)

            bot.save_seen_msgs()
            if first_run:
                first_run = False
                print(f"[{datetime.now()}] ✅ 已初始化烧饼论坛通知与私信历史 ({len(bot.seen_msgs)} 条)，开启实时监听！", flush=True)

        except Exception as e:
            print(f"[{datetime.now()}] 通知监控循环异常: {e}", flush=True)

        time.sleep(DEFAULT_PRIVATE_INTERVAL)


def rss_monitor_thread(bot):
    print(f"[{datetime.now()}] 📡 多社区 RSS 监控引擎已就绪 (共 {len(bot.sources)} 个源)...", flush=True)
    first_run = len(bot.seen_ids) == 0
    last_sbsb_reconcile = 0.0

    while True:
        try:
            for source in bot.sources:
                source_id = source["id"]
                source_name = source["name"]
                source_icon = source["icon"]
                source_url = source["url"]
                author_tag = source.get("author_tag")

                is_enabled = bot.is_source_enabled(source_id)

                root = fetch_rss(source_url)
                if root is None:
                    bot.record_stat("poll_errors")
                    continue

                bot.record_stat("poll_success")
                channel = root.find("channel")
                if channel is None:
                    continue

                items = channel.findall("item")
                for item in reversed(items):
                    guid_elem = item.find("guid")
                    title_elem = item.find("title")
                    link_elem = item.find("link")
                    desc_elem = item.find("description")
                    category_elem = item.find("category")

                    raw_guid = guid_elem.text.strip() if guid_elem is not None and guid_elem.text else ""
                    link = link_elem.text.strip() if link_elem is not None and link_elem.text else ""
                    title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                    desc = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""
                    cat = category_elem.text.strip() if category_elem is not None and category_elem.text else "综合"

                    author = "未知"
                    if author_tag:
                        creator_elem = item.find(author_tag)
                        if creator_elem is not None and creator_elem.text:
                            author = creator_elem.text.strip()
                    elif source_id == "sbsb":
                        author = f"烧饼用户"

                    unique_id = f"{source_id}:{raw_guid or link}"
                    if not unique_id or unique_id in bot.seen_ids:
                        continue

                    # 1. 第一优先级：用户自定义关注词雷达（最高优先级直通）
                    matched_kw = bot.match_custom_keyword(title, desc)
                    if matched_kw:
                        bot.remember_seen_id(unique_id)
                        bot.total_checked += 1
                        bot.record_stat("total_scanned")
                        bot.record_stat("custom_hits")
                        if not bot.paused and is_enabled:
                            deliver_public_match(
                                bot, source_name, source_icon, cat, title, author,
                                desc, link, "custom", f"命中自定义关注词 [{matched_kw}]"
                            )
                        bot.save_seen_ids()
                        continue

                    # 2. 第二优先级：全自动抽奖与福利识别
                    if source_id == "nodeseek":
                        if unique_id in bot.nodeseek_ai_pending:
                            if not is_enabled:
                                bot.nodeseek_ai_pending.pop(unique_id, None)
                                bot.remember_seen_id(unique_id)
                                bot.save_nodeseek_ai_state()
                            continue

                        bot.total_checked += 1
                        bot.record_stat("total_scanned")
                        if first_run or not is_enabled:
                            bot.remember_seen_id(unique_id)
                            continue

                        bot.queue_nodeseek_post(unique_id, cat, title, author, desc, link)
                        continue

                    # 烧饼论坛普通 RSS 帖子未命中关键词则仅记录索引（其抽奖与红包由官方徽标轮询处理）
                    bot.remember_seen_id(unique_id)
                    bot.total_checked += 1
                    bot.record_stat("total_scanned")

            # NodeSeek 新帖进入 AI 事实抽取与裁决队列
            process_nodeseek_ai_queue(bot)

            # 烧饼论坛官方标记独立事件账本检测
            if any(source["id"] == "sbsb" for source in bot.sources):
                marker_batches = []
                homepage = fetch_sbsb_topic_page("/", bot.sbsb_cookie)
                if homepage is None:
                    bot.record_stat("marker_poll_errors")
                else:
                    bot.record_stat("marker_poll_success")
                    for badge_kind in ("lottery", "redpacket"):
                        marker_batches.append(parse_sbsb_badged_topics(homepage, badge_kind))

                now_monotonic = time.monotonic()
                reconcile_complete = False
                if now_monotonic - last_sbsb_reconcile >= SBSB_RECONCILE_INTERVAL:
                    reconcile_pages = {}
                    for badge_kind, path in SBSB_MARKER_PATHS:
                        page_html = fetch_sbsb_topic_page(path, bot.sbsb_cookie)
                        if page_html is None:
                            bot.record_stat("marker_poll_errors")
                        else:
                            bot.record_stat("marker_poll_success")
                            reconcile_pages[badge_kind] = page_html
                            marker_batches.append(parse_sbsb_badged_topics(page_html, badge_kind))
                    reconcile_complete = len(reconcile_pages) == 2
                    last_sbsb_reconcile = now_monotonic

                sbsb_enabled = bot.is_source_enabled("sbsb")
                participant = getattr(bot, "participant", None)
                participation_results = {}
                if participant:
                    candidates = {topic["link"]: topic for batch in marker_batches for topic in batch}
                    participation_results = bot.run_sbsb_participation(list(candidates.values()))
                    for link, stage in participation_results.items():
                        print(f"[{datetime.now()}] 🍪 自动参与阶段: {stage} ({link})", flush=True)
                may_notify = bot.sbsb_events_initialized
                for topics in marker_batches:
                    for topic in reversed(topics):
                        badge_kind = topic["badge_kind"]
                        if not bot.remember_sbsb_event(badge_kind, topic["link"]):
                            continue

                        # 初次升级先静默建立全量基线，防止历史抽奖和红包集中重推
                        if not may_notify:
                            continue

                        bot.record_stat("lottery_hits")
                        bot.record_stat(f"sbsb_{badge_kind}_events")
                        if not bot.paused and sbsb_enabled:
                            label = "抽奖" if badge_kind == "lottery" else "红包"
                            stage = participation_results.get(topic["link"])
                            participation_text = {
                                "waiting": "已安排随机等待，轮到时会复查活动状态再参与。",
                                "confirmed": "本程序提交后已核验本账号参与成功。",
                                "query_confirmed": "已查到本账号参与，提交结果不明确，未归因为本程序。",
                                "uncertain": "参与结果未确认，已保留预算；后台仅查询确认，不会重复提交。",
                                "manual_requirements": "已跳过：活动有程序无法满足的附加条件。",
                                "final_review_denied": "已跳过：第二轮终审未通过。",
                                "final_review_unclear": "已跳过：第二轮终审仍不明确。",
                                "final_review_unavailable": "已跳过：第二轮终审调用失败。",
                                "daily_budget": "今日参与预算已满，仅提醒。",
                                "already": "本账号已经参与，无需重复操作。",
                                "ended": "活动已结束，无需参与。",
                            }.get(stage, "")
                            deliver_public_match(
                                bot,
                                "烧饼论坛",
                                "🍪",
                                topic["category"],
                                topic["title"],
                                topic["author"],
                                f"论坛主题列表已显示官方{label}标记，正文可能仅登录用户可见。{participation_text}",
                                topic["link"],
                                badge_kind,
                                f"命中烧饼论坛官方{label}标记",
                            )

                if not bot.sbsb_events_initialized and reconcile_complete:
                    bot.sbsb_events_initialized = True
                    print(
                        f"[{datetime.now()}] ✅ 已建立烧饼官方标记基线 "
                        f"(抽奖 {len(bot.sbsb_events['lottery'])} / 红包 {len(bot.sbsb_events['redpacket'])})，开启事件监听！",
                        flush=True,
                    )
                bot.save_sbsb_events()

            bot.save_seen_ids()
            if first_run:
                first_run = False
                print(f"[{datetime.now()}] ✅ 已初始化历史帖子索引 ({len(bot.seen_ids)} 篇)，开启多源实时监听！", flush=True)

        except Exception as e:
            print(f"[{datetime.now()}] 监控循环异常: {e}", flush=True)

        time.sleep(bot.poll_interval)


def main():
    bot = BotManager()
    if not bot.bot_token or not bot.admin_chat_id:
        print("❌ 错误: 必须提供 TG_BOT_TOKEN 与 TG_CHAT_ID 环境变量！", flush=True)
        sys.exit(1)

    print(f"[{datetime.now()}] 🚀 多社区抽奖与热帖监控 Bot v{APP_VERSION} 启动完毕...", flush=True)

    t_tg = threading.Thread(target=telegram_polling_thread, args=(bot,), daemon=True)
    t_tg.start()

    t_rss = threading.Thread(target=rss_monitor_thread, args=(bot,), daemon=True)
    t_rss.start()

    t_private = threading.Thread(target=sbsb_private_messages_thread, args=(bot,), daemon=True)
    t_private.start()

    t_checkin = threading.Thread(target=sbsb_checkin_scheduler_thread, args=(bot,), daemon=True)
    t_checkin.start()

    t_report = threading.Thread(target=daily_quality_reporter_thread, args=(bot,), daemon=True)
    t_report.start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
