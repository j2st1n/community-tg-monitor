#!/usr/bin/env python3
import os
import sys
import time
import json
import html
import re
import traceback
import threading
import urllib.request
import urllib.parse
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
MAX_NODESEEK_AI_PENDING = 500
MAX_NODESEEK_AI_HISTORY = 2000
MAX_AI_POSTS_PER_CYCLE = 5

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
    {"command": "status", "description": "📊 运行状态与控制台"},
    {"command": "keywords", "description": "🎯 自定义关注雷达"},
    {"command": "sources", "description": "📡 监控网站独立推送开关"},
    {"command": "signin", "description": "🍪 烧饼论坛一键签到与查分"},
]

LEGACY_BUILTIN_WORDS = {
    "抽奖", "抽", "福利", "roll", "Roll", "ROLL",
    "送只", "送个", "送台", "送一", "白送", "直接送", "先到先得", "免费送", "送小鸡", "送机器", "送码",
    "口令", "红包", "开奖", "盖楼", "中奖", "白嫖", "免费"
}


class BotManager:
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
        self.register_telegram_commands()

    def get_today_cst(self):
        tz_cst = timezone(timedelta(hours=8))
        return datetime.now(tz_cst).strftime("%Y-%m-%d")

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
        api_url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        payload = {"commands": BOT_COMMANDS}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": APP_USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    print(f"[{datetime.now()}] ✅ Telegram 官方快捷指令菜单已自动注册！", flush=True)
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ 注册菜单失败: {e}", flush=True)

    def load_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    raw_keywords = data.get("keywords", [])
                    self.keywords = [k for k in raw_keywords if k not in LEGACY_BUILTIN_WORDS]
                    self.poll_interval = data.get("poll_interval", DEFAULT_POLL_INTERVAL)
                    self.paused = data.get("paused", False)
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
            self.save_settings()

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
        elif not self.ai_enabled and self.env_ai_api_key and self.ai_endpoint and self.ai_model:
            self.ai_enabled = True

    def save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "keywords": self.keywords,
            "poll_interval": self.poll_interval,
            "paused": self.paused,
            "source_states": self.source_states,
            "ai_enabled": self.ai_enabled,
            "ai_endpoint": self.ai_endpoint,
            "ai_model": self.ai_model,
            "ai_judge_model": self.ai_judge_model,
            "ai_accept_threshold": self.ai_accept_threshold
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
                return {
                    "pending": pending if isinstance(pending, dict) else {},
                    "history": history if isinstance(history, list) else [],
                }
            except Exception as e:
                print(f"[{datetime.now()}] 读取 NodeSeek AI 状态异常: {e}", flush=True)
        return {"pending": {}, "history": []}

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
                {"pending": self.nodeseek_ai_pending, "history": self.nodeseek_ai_history},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def queue_nodeseek_post(self, unique_id, category, title, author, desc, link):
        if unique_id in self.nodeseek_ai_pending:
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

    def format_ai_card(self):
        configured = bool(self.ai_endpoint and self.ai_model and self.ai_api_key)
        running = self.ai_is_ready()
        source_tags = []
        if self.env_ai_api_key:
            source_tags.append("Key")
        if self.env_ai_endpoint:
            source_tags.append("API")
        if self.env_ai_model:
            source_tags.append("模型")
        env_source_desc = f"🔒 .env 注入 ({', '.join(source_tags)})" if source_tags else "本地存储"

        status_text = '🟢 运行中' if running else ('🟡 已配置但未启用' if configured else '⚪ 配置未完成')
        judge_text = self.ai_judge_model or '跟随主模型'
        lines = [
            "🧠 <b>NodeSeek AI 抽奖判定</b>",
            "",
            f"• 状态: {status_text}",
            f"• 配置来源: <code>{env_source_desc}</code>",
            f"• API: <code>{html.escape(self.ai_endpoint or '未设置')}</code>",
            f"• 模型: <code>{html.escape(self.ai_model or '未设置')}</code>",
            f"• 复审模型: <code>{html.escape(judge_text)}</code>",
            f"• API Key: <code>{html.escape(self.masked_ai_key())}</code>",
            f"• 自动通过阈值: <code>{self.ai_accept_threshold:.2f}</code>",
            f"• 待判定队列: <b>{len(self.nodeseek_ai_pending)}</b> 篇",
            "",
            "<i>💡 最佳实践：直接在服务器 .env 中配置 <code>AI_API_KEY</code> 等变量，安全且无需在对话框输入任何密钥。</i>"
        ]
        text = "\n".join(lines)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🔌 设置 API", "callback_data": "ai:set_endpoint"},
                    {"text": "🔑 设置 Key", "callback_data": "ai:set_key"},
                ],
                [
                    {"text": "🤖 设置模型", "callback_data": "ai:set_model"},
                    {"text": "⚖️ 复审模型", "callback_data": "ai:set_judge"},
                ],
                [{"text": "🎚️ 设置阈值", "callback_data": "ai:set_threshold"}],
                [
                    {"text": "🧪 测试连接", "callback_data": "ai:test"},
                    {"text": "⏯️ 启用/停用", "callback_data": "ai:toggle"},
                ],
            ]
        }
        return text, markup

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
            self.save_settings()
            return self.source_states[source_id]

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

    def send_msg(self, chat_id, text, reply_markup=None, disable_preview=True, retries=2):
        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data = json.dumps(payload).encode("utf-8")
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                api_url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": APP_USER_AGENT}
            )
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    if resp.status == 200:
                        return True
            except urllib.error.HTTPError as he:
                if he.code == 429:
                    time.sleep(2)
                else:
                    print(f"[{datetime.now()}] TG 发送 HTTP {he.code}: {he}", flush=True)
                    break
            except Exception as e:
                print(f"[{datetime.now()}] TG 发送异常 (尝试 {attempt+1}/{retries+1}): {e}", flush=True)
                time.sleep(1)
        return False

    def edit_msg_text(self, chat_id, message_id, text, reply_markup=None):
        api_url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": APP_USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"[{datetime.now()}] 编辑消息失败: {e}", flush=True)
            return False

    def delete_msg(self, chat_id, message_id):
        if not self.bot_token or not chat_id or not message_id:
            return False
        api_url = f"https://api.telegram.org/bot{self.bot_token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": message_id}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": APP_USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status == 200
        except Exception:
            return False

    def answer_callback_query(self, callback_query_id, text):
        api_url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id, "text": text, "show_alert": False}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": APP_USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def format_status_card(self):
        uptime = datetime.now() - self.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)

        ns_on = "🟢 开启" if self.source_states.get("nodeseek", True) else "🔴 暂停"
        sb_on = "🟢 开启" if self.source_states.get("sbsb", True) else "🔴 暂停"

        ai_status = "🟢 运行中" if self.ai_is_ready() else ("🟡 已停用" if self.ai_endpoint and self.ai_model and self.ai_api_key else "⚪ 未配置")
        sb_checkin_info = "官方标记 + 每日签到" + (f" (UID: {self.sbsb_uid})" if self.sbsb_uid else "")
        if not self.sbsb_cookie:
            sb_checkin_info = "官方标记 (未配置 Cookie 签到)"

        pause_label = "▶️ 恢复推送" if self.paused else "⏸️ 暂停推送"
        pause_action = "resume" if self.paused else "pause"

        kw_status = f"<b>{len(self.keywords)}</b> 个 (最高优先级直通)" if self.keywords else "未配置 (默认不启用)"

        stats = dict(self.daily_stats)
        scanned = stats.get("total_scanned", 0)
        hits = stats.get("custom_hits", 0) + stats.get("lottery_hits", 0)
        priv_notified = stats.get("private_notified", 0)

        lines = [
            "📊 <b>多社区监控实时守护状态</b>",
            "",
            f"⏱️ <b>连续运行</b>: {hours}小时 {minutes}分  |  🔔 <b>推送状态</b>: {'⏸️ 已暂停推送' if self.paused else '▶️ 正常推送中'}",
            f"📡 <b>轮询频率</b>: 每 {self.poll_interval} 秒扫描双源",
            "",
            "🌐 <b>监控网站状态</b>",
            f"• 🌐 <b>NodeSeek</b>: {ns_on} ({ai_status} AI 判定，待处理 {len(self.nodeseek_ai_pending)} 篇)",
            f"• 🍪 <b>烧饼论坛</b>: {sb_on} ({sb_checkin_info})",
            "",
            "🎯 <b>专属关注雷达</b>",
            f"• 状态: {kw_status}",
            "",
            "📈 <b>今日活跃概览</b>",
            f"• 扫描新帖: <b>{scanned}</b> 篇  |  今日累计命中: <b>{hits}</b> 篇",
            f"• 烧饼私信提醒: <b>{priv_notified}</b> 次  |  去重索引库: <b>{len(self.seen_ids)}</b> 篇公开帖",
        ]
        text = "\n".join(lines)
        markup = {
            "inline_keyboard": [
                [
                    {"text": pause_label, "callback_data": f"menu:{pause_action}"},
                    {"text": "📈 查看今日详细日报", "callback_data": "menu:report"},
                ],
                [
                    {"text": "📡 网站开关", "callback_data": "menu:sources"},
                    {"text": "🎯 关注词管理", "callback_data": "menu:keywords"},
                ],
            ]
        }
        return text, markup

    def format_keywords_card(self):
        with self.lock:
            kws = list(self.keywords)
        
        if kws:
            pills = " · ".join([f"<code>{k}</code>" for k in kws])
            body = f"🎯 <b>生效中的专属关注词</b> ({len(kws)} 个 - <b>最高优先级直通</b>)\n{pills}"
        else:
            body = "<i>（当前未配置关注词，专属雷达处于不启用状态）</i>"

        text = (
            f"🎯 <b>专属关注雷达管理</b>\n\n"
            f"{body}\n\n"
            "<i>💡 说明：关键词拥有最高判定优先级。一旦帖子标题或内容包含关注词，将立即作为【自定义关注】直通推送给您，不再进入后续抽奖判定队列。</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "➕ 添加关注词", "callback_data": "menu:add_kw"},
                    {"text": "🗑️ 点选删除", "callback_data": "menu:del_kw"}
                ]
            ]
        }
        return text, markup

    def format_sources_card(self):
        with self.lock:
            lines = []
            buttons = []
            for s in self.sources:
                sid = s["id"]
                sname = s["name"]
                sicon = s["icon"]
                is_on = self.source_states.get(sid, True)
                status_str = "🟢 开启推送中" if is_on else "🔴 已暂停推送"
                lines.append(f"{sicon} <b>{sname}</b>: {status_str}")
                btn_text = f"{sicon} {sname}: {'开启 🟢' if is_on else '关闭 🔴'}"
                buttons.append([{"text": btn_text, "callback_data": f"toggle_src:{sid}"}])

        body = "\n".join(lines)
        text = (
            "📡 <b>监控网站独立推送开关控制台</b>\n\n"
            f"{body}\n\n"
            "<i>💡 点击下方按钮可独立开启/关闭对应网站的抽奖与新帖推送：</i>"
        )
        markup = {"inline_keyboard": buttons}
        return text, markup

    def generate_daily_report_card(self):
        """生成每日运行统计与算法成功率报告卡片（总-分结构，无锁快照）"""
        stats = dict(self.daily_stats)
        scanned = stats.get("total_scanned", 0)
        lottery_hits = stats.get("lottery_hits", 0)
        custom_hits = stats.get("custom_hits", 0)
        total_captured = custom_hits + lottery_hits
        priv_notified = stats.get("private_notified", 0)
        poll_success = stats.get("poll_success", 0)
        poll_errors = stats.get("poll_errors", 0)
        delivery_success = stats.get("delivery_success", 0)
        delivery_errors = stats.get("delivery_errors", 0)
        marker_poll_success = stats.get("marker_poll_success", 0)
        marker_poll_errors = stats.get("marker_poll_errors", 0)
        sbsb_lottery_events = stats.get("sbsb_lottery_events", 0)
        sbsb_redpacket_events = stats.get("sbsb_redpacket_events", 0)
        ai_requests = stats.get("ai_requests", 0)
        ai_errors = stats.get("ai_errors", 0)
        ai_classified = stats.get("ai_classified", 0)
        ai_giveaway_hits = stats.get("ai_giveaway_hits", 0)
        ai_second_reviews = stats.get("ai_second_reviews", 0)
        ai_uncertain = stats.get("ai_uncertain", 0)

        total_polls = poll_success + poll_errors
        poll_rate = (poll_success / total_polls * 100) if total_polls > 0 else 100.0
        total_deliv = delivery_success + delivery_errors
        deliv_rate = (delivery_success / total_deliv * 100) if total_deliv > 0 else 100.0
        date_str = stats.get("date", self.get_today_cst())

        ns_is_on = "🟢 开启中" if self.source_states.get("nodeseek", True) else "🔴 已暂停"
        sb_is_on = "🟢 开启中" if self.source_states.get("sbsb", True) else "🔴 已暂停"
        sb_checkin_str = f"🟢 运行中" + (f" (UID: {self.sbsb_uid})" if self.sbsb_uid else "") if self.sbsb_cookie else "⚪ 未配置"

        lines = [
            "📈 <b>多社区监控每日运行与算法日报</b>",
            f"📅 <b>统计日期</b>: <code>{date_str}</code> (周期: 24h)",
            "",
            "📊 <b>【总览】全网数据大盘</b>",
            f"• 🌐 全网新发主题: <b>{scanned}</b> 篇",
            f"• 🎁 累计捕获推送: <b>{total_captured}</b> 篇 (关注直通 {custom_hits} / 福利抽奖 {lottery_hits})",
            f"• ✉️ Telegram 送达: <b>{delivery_success}</b> 成功 / <b>{delivery_errors}</b> 失败 (成功率 {deliv_rate:.1f}%)",
            f"• 📡 采集巡检成功率: <b>{poll_rate:.1f}%</b> ({poll_success} 成功 / {poll_errors} 异常)",
            "",
            "🌐 <b>【NodeSeek】AI 语义驱动</b>",
            f"• 📡 监控状态: {ns_is_on} (全站实时流)",
            f"• 🧠 AI 判定总数: <b>{ai_classified}</b> 篇 (API 请求 {ai_requests} 次 / 异常 {ai_errors})",
            f"• 🎯 判定结果分布: 抽奖命中 <b>{ai_giveaway_hits}</b> 篇 | 边界二审 <b>{ai_second_reviews}</b> 篇 | 存疑待审 <b>{ai_uncertain}</b> 篇",
            "",
            "🍪 <b>【烧饼论坛】官方标记与签到</b>",
            f"• 📡 监控状态: {sb_is_on} (RSS 30s + 官方标记 5m)",
            f"• 🎁 官方标记事件: 抽奖 <b>{sbsb_lottery_events}</b> 篇 | 红包 <b>{sbsb_redpacket_events}</b> 个 (巡检 {marker_poll_success} 次 / 异常 {marker_poll_errors})",
            f"• 📬 私信与互动通知: <b>{priv_notified}</b> 次提醒",
            f"• 🍪 每日自动签到: {sb_checkin_str}",
        ]
        return "\n".join(lines)


def make_keyword_buttons(keywords, prefix="del_kw"):
    buttons = []
    row = []
    for kw in keywords:
        row.append({"text": f"❌ {kw}", "callback_data": f"{prefix}:{kw}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "🔙 返回关注词列表", "callback_data": f"{prefix}:__back__"}])
    return {"inline_keyboard": buttons}


def do_sbsb_signin(cookie):
    """执行烧饼论坛自动签到并精确解析资产、成长值、连续天数与官方签到时间"""
    if not cookie:
        return {"success": False, "msg": "未配置 SBSB_COOKIE，请先在 VPS 配置 Cookie"}

    if "=" not in cookie:
        cookie = f"__Host-bbs_session={cookie}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://sb.sb/",
        "Origin": "https://sb.sb",
    }

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def http_error_302(self, req, fp, code, msg, headers):
            return fp
        def http_error_303(self, req, fp, code, msg, headers):
            return fp

    opener = urllib.request.build_opener(NoRedirectHandler)

    try:
        req = urllib.request.Request("https://sb.sb/signin/", headers=headers)
        resp = opener.open(req, timeout=15)
        status_code = getattr(resp, "status", getattr(resp, "code", 200))
        location = resp.headers.get("Location", "")
        if status_code in [302, 303] and ("login" in location or "/login" in location):
            return {"success": False, "msg": "Cookie 已失效，请在 VPS 重新填入 SBSB_COOKIE"}

        html_signin = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return {"success": False, "msg": f"访问签到页面异常: {e}"}

    # 精确判断签到状态（结构化匹配按钮或表单）
    already_signed = bool(
        re.search(r'<button[^>]*class=["\'][^"\']*btn-post[^"\']*["\'][^>]*disabled[^>]*>\s*今日已签到\s*</button>', html_signin, re.IGNORECASE)
        or re.search(r'disabled[^>]*>\s*今日已签到\s*<', html_signin)
    )

    flash_msg = ""
    # 如果未签到，寻找 signin 表单并发起 POST 签到
    if not already_signed:
        signin_form_m = re.search(r'<form[^>]*action=["\'](?:https://sb\.sb)?/signin/?["\'][^>]*>(.*?)</form>', html_signin, re.DOTALL | re.IGNORECASE)
        form_body = signin_form_m.group(1) if signin_form_m else html_signin
        csrf_match = (
            re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', form_body)
            or re.search(r'value=["\']([^"\']+)["\']\s+name=["\']_csrf["\']', form_body)
            or re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', html_signin)
        )
        if csrf_match:
            csrf_token = csrf_match.group(1)
            post_data = urllib.parse.urlencode({"_csrf": csrf_token}).encode("utf-8")
            post_headers = dict(headers)
            post_headers["Content-Type"] = "application/x-www-form-urlencoded"
            post_headers["Referer"] = "https://sb.sb/signin/"
            post_headers["Origin"] = "https://sb.sb"
            try:
                post_req = urllib.request.Request("https://sb.sb/signin/", data=post_data, headers=post_headers)
                opener.open(post_req, timeout=15)
                # 签到 POST 成功后重新拉取 /signin/ 页面以获取更新后的连签天数与提示
                req_refreshed = urllib.request.Request("https://sb.sb/signin/", headers=headers)
                with opener.open(req_refreshed, timeout=15) as resp_refreshed:
                    html_signin = resp_refreshed.read().decode("utf-8", errors="ignore")
                already_signed = True
            except Exception as e:
                print(f"[{datetime.now()}] 提交签到请求异常: {e}", flush=True)

    # 提取 flash 消息
    flash_match = re.search(r'window\.__pageFlash=["\']([^"\']*)["\']', html_signin) or re.search(r'class=["\'][^"\']*toast[^"\']*["\']>([^<]+)<', html_signin)
    if flash_match and flash_match.group(1).strip():
        flash_msg = flash_match.group(1).strip()
    else:
        flash_msg = "今日已完成签到" if already_signed else "签到成功"

    streak_m = re.search(r'class=\"[^\"]*signin-streak-num[^\"]*\"[^>]*>(\d+)</div>', html_signin)
    streak_days = streak_m.group(1) if streak_m else "1"

    total_days_m = re.search(r'<span[^>]*class=\"signin-stat-value\">(\d+)</span>\s*<span[^>]*class=\"signin-stat-label\">累计签到', html_signin)
    total_days = total_days_m.group(1) if total_days_m else streak_days

    points = "0"
    exp = "0"
    level = "会员"
    signin_time_str = ""

    try:
        req_points = urllib.request.Request("https://sb.sb/points/", headers=headers)
        resp_points = opener.open(req_points, timeout=15)
        html_points = resp_points.read().decode("utf-8", errors="ignore")

        pts_match = re.search(r'可用烧饼</span>\s*<span[^>]*class=\"[^\"]*value[^\"]*\"[^>]*>(\d+)</span>', html_points) or re.search(r'可用烧饼.*?(\d+)', html_points)
        if pts_match:
            points = pts_match.group(1)

        exp_match = re.search(r'成长值</span>\s*<span[^>]*class=\"[^\"]*value[^\"]*\"[^>]*>(\d+)</span>', html_points) or re.search(r'成长值.*?(\d+)', html_points)
        if exp_match:
            exp = exp_match.group(1)

        lv_match = re.search(r'等级</span>\s*<span[^>]*class=\"[^\"]*value[^\"]*\"[^>]*>([^<]+)</span>', html_points) or re.search(r'等级.*?(Lv\.\d+[^<\n]+)', html_points)
        if lv_match:
            level = lv_match.group(1).strip()

        # 匹配账本中的每日签到记录（兼容现代 HTML 省略 </td> 闭合标签的写法）
        time_with_iso_m = re.search(r'<time[^>]*datetime=["\']([^"\']+)["\'][^>]*>([^<]+)</time>\s*(?:</td>\s*)?<td>\s*每日签到', html_points)
        if time_with_iso_m:
            iso_str = time_with_iso_m.group(1)
            try:
                dt_utc = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
                dt_cst = dt_utc.astimezone(timezone(timedelta(hours=8)))
                signin_time_str = dt_cst.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                signin_time_str = time_with_iso_m.group(2).strip()
        else:
            time_plain_m = re.search(r'<time[^>]*>([^<]+)</time>\s*(?:</td>\s*)?<td>\s*每日签到', html_points)
            if time_plain_m:
                signin_time_str = time_plain_m.group(1).strip()
    except Exception as pe:
        print(f"[{datetime.now()}] 访问 points 页面异常: {pe}", flush=True)

    if not signin_time_str:
        tz_cst = timezone(timedelta(hours=8))
        signin_time_str = datetime.now(tz_cst).strftime('%Y-%m-%d %H:%M:%S')

    return {
        "success": True,
        "already": already_signed or ("已签到" in flash_msg),
        "msg": flash_msg,
        "consecutive_days": f"{streak_days} 天",
        "total_days": f"{total_days} 天",
        "total_points": f"{points} 饼",
        "exp": exp,
        "level": level,
        "signin_time": signin_time_str
    }


def handle_callback_query(bot, query):
    query_id = query.get("id")
    from_user = str(query.get("from", {}).get("id", ""))
    msg = query.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    data = query.get("data", "")

    if from_user != bot.admin_chat_id:
        bot.answer_callback_query(query_id, "⚠️ 无权操作此机器人。")
        return

    if data.startswith("toggle_src:"):
        target_src = data.split("toggle_src:", 1)[1]
        new_state = bot.toggle_source(target_src)
        state_text = "开启 🟢" if new_state else "关闭 🔴"
        bot.answer_callback_query(query_id, f"已切换为: {state_text}")
        text, markup = bot.format_sources_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "menu:pause":
        with bot.lock:
            bot.paused = True
            bot.save_settings()
        bot.answer_callback_query(query_id, "⏸️ 已暂停全局推送")
        text, markup = bot.format_status_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "menu:resume":
        with bot.lock:
            bot.paused = False
            bot.save_settings()
        bot.answer_callback_query(query_id, "▶️ 已恢复全局推送")
        text, markup = bot.format_status_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "menu:report":
        bot.answer_callback_query(query_id, "正在获取今日日报")
        report_text = bot.generate_daily_report_card()
        bot.send_msg(chat_id, report_text)

    elif data == "menu:sources":
        bot.answer_callback_query(query_id, "切换到监控源设置")
        text, markup = bot.format_sources_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "menu:keywords":
        bot.answer_callback_query(query_id, "切换到关注词管理")
        text, markup = bot.format_keywords_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "menu:status":
        bot.answer_callback_query(query_id, "返回状态面板")
        text, markup = bot.format_status_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "ai:set_endpoint":
        if bot.env_ai_endpoint:
            bot.answer_callback_query(query_id, "已由环境变量锁定")
            bot.send_msg(
                chat_id,
                f"🔒 <b>API 地址已由系统环境变量 <code>AI_ENDPOINT</code> 锁定：</b>\n"
                f"<code>{html.escape(bot.env_ai_endpoint)}</code>\n\n"
                f"如需更改，请直接修改服务器 <code>.env</code> 文件并重启容器。"
            )
        else:
            bot.user_states[chat_id] = "waiting_for_ai_endpoint"
            bot.answer_callback_query(query_id, "请发送 API 地址")
            bot.send_msg(
                chat_id,
                "🔌 <b>请输入 OpenAI-compatible API 地址</b>\n"
                "例如：<code>https://api.openai.com/v1</code>\n"
                "也可以直接填写完整的 <code>/chat/completions</code> 地址。\n\n"
                "<i>💡 推荐直接在 .env 文件中设置 <code>AI_ENDPOINT</code></i>",
            )

    elif data == "ai:set_key":
        if bot.env_ai_api_key:
            bot.answer_callback_query(query_id, "Key 已由环境变量锁定")
            bot.send_msg(
                chat_id,
                "🔒 <b>API Key 已由系统环境变量 <code>AI_API_KEY</code> 注入锁定</b>\n\n"
                "零对话框泄露风险。如需更换，请直接修改服务器 <code>.env</code> 文件并重启容器。"
            )
        else:
            bot.user_states[chat_id] = "waiting_for_ai_key"
            bot.answer_callback_query(query_id, "请发送 API Key")
            bot.send_msg(
                chat_id,
                "🔑 <b>请输入 API Key</b>\n\n"
                "<i>💡 最佳实践：推荐直接在服务器 <code>.env</code> 中配置 <code>AI_API_KEY</code>，完全无需在对话框输入。\n"
                "若在此处发送，Bot 接收后会<b>立即自动删除你的原消息</b>以防留下聊天记录。</i>",
            )

    elif data == "ai:set_model":
        if bot.env_ai_model:
            bot.answer_callback_query(query_id, "模型已由环境变量锁定")
            bot.send_msg(
                chat_id,
                f"🔒 <b>主模型已由环境变量 <code>AI_MODEL</code> 锁定：</b>\n"
                f"<code>{html.escape(bot.env_ai_model)}</code>\n\n"
                f"如需更改，请直接修改服务器 <code>.env</code> 文件并重启容器。"
            )
        else:
            bot.user_states[chat_id] = "waiting_for_ai_model"
            bot.answer_callback_query(query_id, "请发送模型名")
            bot.send_msg(chat_id, "🤖 <b>请输入主模型名称</b>\n例如：<code>gpt-4o-mini</code> 或 <code>deepseek-chat</code>\n\n<i>💡 推荐直接在 .env 中设置 <code>AI_MODEL</code></i>")

    elif data == "ai:set_judge":
        bot.user_states[chat_id] = "waiting_for_ai_judge"
        bot.answer_callback_query(query_id, "请发送复审模型名")
        bot.send_msg(chat_id, "⚖️ <b>请输入复审模型名称</b>\n发送 <code>-</code> 可改为跟随主模型。")

    elif data == "ai:set_threshold":
        bot.user_states[chat_id] = "waiting_for_ai_threshold"
        bot.answer_callback_query(query_id, "请发送阈值")
        bot.send_msg(chat_id, "🎚️ <b>请输入自动通过阈值</b>\n允许范围 <code>0.70～0.99</code>，建议 <code>0.90</code>。")

    elif data == "ai:toggle":
        if not (bot.ai_endpoint and bot.ai_model and bot.ai_api_key):
            bot.answer_callback_query(query_id, "请先完成 API、Key 和模型配置")
            bot.send_msg(chat_id, "⚠️ <b>AI 配置尚未完成，无法启用。</b>")
        else:
            bot.ai_enabled = not bot.ai_enabled
            if bot.ai_enabled:
                for record in bot.nodeseek_ai_pending.values():
                    record["next_retry_at"] = 0
            bot.save_settings()
            bot.save_nodeseek_ai_state()
            bot.answer_callback_query(query_id, "AI 已启用" if bot.ai_enabled else "AI 已停用")
            text_card, markup = bot.format_ai_card()
            bot.edit_msg_text(chat_id, msg_id, text_card, reply_markup=markup)

    elif data == "ai:test":
        if not (bot.ai_endpoint and bot.ai_model and bot.ai_api_key):
            bot.answer_callback_query(query_id, "请先完成配置")
            bot.send_msg(chat_id, "⚠️ <b>请先设置 API 地址、API Key 和主模型。</b>")
        else:
            bot.answer_callback_query(query_id, "正在测试连接")
            bot.send_msg(chat_id, "⏳ <b>正在调用模型进行结构化判定测试...</b>")
            try:
                facts = bot.test_ai_connection()
                bot.send_msg(
                    chat_id,
                    "✅ <b>AI 连接和 JSON 输出验证成功</b>\n"
                    f"模型识别状态：<code>{facts['event_state']}</code>\n"
                    f"置信度：<code>{facts['confidence']:.2f}</code>",
                )
            except (AIClassificationError, ValueError) as exc:
                bot.send_msg(chat_id, f"❌ <b>AI 测试失败</b>\n<code>{html.escape(str(exc))}</code>")

    elif data == "menu:add_kw":
        bot.user_states[chat_id] = "waiting_for_add"
        bot.answer_callback_query(query_id, "请直接输入新关注词")
        bot.send_msg(chat_id, "➕ <b>请输入你想添加的专属关注词：</b>\n<i>（支持一次输入多个词，用空格分隔，例如：<code>DMIT 搬瓦工 9929 传家宝</code>）</i>\n\n💡 <i>提示：添加后立即生效并享有最高推送优先级！</i>")

    elif data == "menu:del_kw":
        bot.answer_callback_query(query_id, "请点击按钮删除")
        with bot.lock:
            kws = list(bot.keywords)
        if not kws:
            bot.edit_msg_text(chat_id, msg_id, "⚠️ <b>当前没有已添加的自定义关注词。</b>")
        else:
            text = f"🗑️ <b>请点击下方按钮删除对应的关注词（共 {len(kws)} 个）：</b>"
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=make_keyword_buttons(kws, "del_kw"))

    elif data.startswith("del_kw:"):
        target_kw = data.split("del_kw:", 1)[1]
        if target_kw == "__back__":
            bot.answer_callback_query(query_id, "返回")
            text, markup = bot.format_keywords_card()
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)
            return
        
        with bot.lock:
            if target_kw in bot.keywords:
                bot.keywords.remove(target_kw)
                bot.save_settings()
                bot.answer_callback_query(query_id, f"已删除: {target_kw}")
            else:
                bot.answer_callback_query(query_id, "该词已不存在")
        
        with bot.lock:
            kws = list(bot.keywords)
        if not kws:
            text, markup = bot.format_keywords_card()
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)
        else:
            text = f"🗑️ <b>请点击下方按钮删除对应的关注词（剩余 {len(kws)} 个）：</b>"
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=make_keyword_buttons(kws, "del_kw"))


def handle_command_or_text(bot, chat_id, text, message_id=None):
    text = text.strip()

    if chat_id in bot.user_states and not text.startswith("/"):
        state = bot.user_states.pop(chat_id)
        if state == "waiting_for_ai_endpoint":
            if bot.env_ai_endpoint:
                bot.send_msg(chat_id, "🔒 <b>API 地址已由环境变量 <code>AI_ENDPOINT</code> 锁定。</b>")
                return
            try:
                normalize_chat_endpoint(text)
                bot.ai_endpoint = text.strip().rstrip("/")
                bot.save_settings()
                for record in bot.nodeseek_ai_pending.values():
                    record["next_retry_at"] = 0
                bot.save_nodeseek_ai_state()
                msg_text, markup = bot.format_ai_card()
                bot.send_msg(chat_id, f"✅ <b>API 地址已保存</b>\n\n{msg_text}", reply_markup=markup)
            except ValueError as exc:
                bot.send_msg(chat_id, f"❌ <b>API 地址无效</b>\n<code>{html.escape(str(exc))}</code>")
            return

        if state == "waiting_for_ai_key":
            if message_id:
                bot.delete_msg(chat_id, message_id)
            if bot.env_ai_api_key:
                bot.send_msg(
                    chat_id,
                    "🔒 <b>API Key 已由系统环境变量锁定，无需在对话框中设置（包含密钥的原消息已自动销毁）。</b>"
                )
                return
            api_key = text.strip()
            if not api_key or len(api_key) > 1000:
                bot.send_msg(chat_id, "❌ <b>API Key 不能为空或过长。</b>")
            else:
                bot.save_ai_secret(api_key)
                for record in bot.nodeseek_ai_pending.values():
                    record["next_retry_at"] = 0
                bot.save_nodeseek_ai_state()
                msg_text, markup = bot.format_ai_card()
                bot.send_msg(chat_id, f"✅ <b>API Key 已安全保存（包含密钥的消息已自动销毁）</b>\n\n{msg_text}", reply_markup=markup)
            return

        if state == "waiting_for_ai_model":
            if bot.env_ai_model:
                bot.send_msg(chat_id, "🔒 <b>主模型已由环境变量 <code>AI_MODEL</code> 锁定。</b>")
                return
            model = text.strip()
            if not model or len(model) > 200 or any(ch.isspace() for ch in model):
                bot.send_msg(chat_id, "❌ <b>模型名无效，模型名不能包含空格。</b>")
            else:
                bot.ai_model = model
                bot.save_settings()
                for record in bot.nodeseek_ai_pending.values():
                    record["next_retry_at"] = 0
                bot.save_nodeseek_ai_state()
                msg_text, markup = bot.format_ai_card()
                bot.send_msg(chat_id, f"✅ <b>主模型已保存</b>\n\n{msg_text}", reply_markup=markup)
            return

        if state == "waiting_for_ai_judge":
            model = "" if text.strip() == "-" else text.strip()
            if len(model) > 200 or any(ch.isspace() for ch in model):
                bot.send_msg(chat_id, "❌ <b>复审模型名无效，模型名不能包含空格。</b>")
            else:
                bot.ai_judge_model = model
                bot.save_settings()
                msg_text, markup = bot.format_ai_card()
                bot.send_msg(chat_id, f"✅ <b>复审模型已保存</b>\n\n{msg_text}", reply_markup=markup)
            return

        if state == "waiting_for_ai_threshold":
            try:
                threshold = float(text.strip())
                if not 0.70 <= threshold <= 0.99:
                    raise ValueError
                bot.ai_accept_threshold = threshold
                bot.save_settings()
                msg_text, markup = bot.format_ai_card()
                bot.send_msg(chat_id, f"✅ <b>自动通过阈值已设为 {threshold:.2f}</b>\n\n{msg_text}", reply_markup=markup)
            except ValueError:
                bot.send_msg(chat_id, "❌ <b>阈值无效，请输入 0.70 到 0.99 之间的数字。</b>")
            return

        if state == "waiting_for_add":
            new_kws = [k for k in text.split() if k not in LEGACY_BUILTIN_WORDS]
            added = []
            with bot.lock:
                for kw in new_kws:
                    if kw not in bot.keywords:
                        bot.keywords.append(kw)
                        added.append(kw)
                bot.save_settings()
            
            text_card, markup = bot.format_keywords_card()
            if added:
                bot.send_msg(chat_id, f"✅ <b>已成功添加关注词 (最高优先级)</b>: <code>{', '.join(added)}</code>\n\n{text_card}", reply_markup=markup)
            else:
                bot.send_msg(chat_id, f"⚠️ <b>未添加新关注词</b>（可能词汇已存在）。\n\n{text_card}", reply_markup=markup)
            return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""

    try:
        if cmd in ["/start", "/help"]:
            help_text = (
                "🤖 <b>多社区监控守护机器人使用指南</b>\n\n"
                "<b>核心指令菜单 (4大核心功能)</b>:\n"
                "├ /status - 📊 <b>监控状态与控制台</b>（含暂停/恢复、日报查看）\n"
                "├ /keywords - 🎯 <b>自定义关注雷达</b>（最高优先级直通推送）\n"
                "├ /sources - 📡 <b>各网站独立开关管理</b>\n"
                "└ /signin - 🍪 <b>烧饼论坛一键签到与查分</b>\n\n"
                "💡 <i>NodeSeek 抽奖由 AI 双阶段语义模型自动判定，烧饼论坛抽奖/红包由官方徽标驱动。</i>"
            )
            bot.send_msg(chat_id, help_text)

        elif cmd in ["/status", "/stats"]:
            text_card, markup = bot.format_status_card()
            bot.send_msg(chat_id, text_card, reply_markup=markup)

        elif cmd == "/report":
            report_text = bot.generate_daily_report_card()
            bot.send_msg(chat_id, report_text)

        elif cmd == "/sources":
            text_card, markup = bot.format_sources_card()
            bot.send_msg(chat_id, text_card, reply_markup=markup)

        elif cmd == "/ai":
            text_card, markup = bot.format_ai_card()
            bot.send_msg(chat_id, text_card, reply_markup=markup)

        elif cmd in ["/signin", "/checkin"]:
            bot.send_msg(chat_id, "⏳ <b>正在连接烧饼论坛执行签到与资产同步...</b>")
            res = do_sbsb_signin(bot.sbsb_cookie)
            if res.get("success"):
                # 手动签到成功，同步更新本地签到状态记录
                today_str = bot.get_today_cst()
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(CHECKIN_STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump({"last_date": today_str}, f, ensure_ascii=False)
                except Exception:
                    pass

                status_badge = "✅ 签到成功！" if not res.get("already") else "✨ 今日已完成签到"
                report_msg = (
                    f"🍪 <b>烧饼论坛 (sb.sb) 签到与资产报告</b>\n\n"
                    f"🎉 <b>签到状态</b>: {status_badge}\n"
                    f"📅 <b>连续签到</b>: <b>{res.get('consecutive_days')}</b> (累计: {res.get('total_days')})\n"
                    f"💰 <b>可用烧饼</b>: <b>{res.get('total_points')}</b>\n"
                    f"⭐ <b>成长等级</b>: <b>{res.get('level')}</b> (成长值: {res.get('exp')})\n"
                    f"🕒 <b>签到时间</b>: {res.get('signin_time')}\n\n"
                    "<i>💡 系统将在每日 08:05 (UTC+8) 自动执行定时签到</i>"
                )
                bot.send_msg(chat_id, report_msg)
            else:
                bot.send_msg(chat_id, f"⚠️ <b>烧饼论坛签到失败</b>\n原因: <code>{res.get('msg')}</code>")

        elif cmd in ["/keywords", "/list", "/add", "/del"]:
            if cmd == "/add" and arg:
                new_kws = [k for k in arg.split() if k not in LEGACY_BUILTIN_WORDS]
                added = []
                with bot.lock:
                    for kw in new_kws:
                        if kw not in bot.keywords:
                            bot.keywords.append(kw)
                            added.append(kw)
                    bot.save_settings()
                text_card, markup = bot.format_keywords_card()
                bot.send_msg(chat_id, f"✅ 已添加关注词：<code>{', '.join(added)}</code>\n\n{text_card}", reply_markup=markup)
                return
            
            text_card, markup = bot.format_keywords_card()
            bot.send_msg(chat_id, text_card, reply_markup=markup)

        elif cmd == "/pause":
            with bot.lock:
                bot.paused = True
                bot.save_settings()
            bot.send_msg(chat_id, "⏸️ <b>全局监控推送已暂停！</b>\n（后台继续记录去重索引，免打扰，发送 /resume 可随时恢复）")

        elif cmd == "/resume":
            with bot.lock:
                bot.paused = False
                bot.save_settings()
            bot.send_msg(chat_id, "▶️ <b>全局监控推送已恢复正常运行！</b>")

        elif cmd == "/test":
            test_msg_ns = (
                "🎁 <b>🌐 [NodeSeek] 发现抽奖/福利新帖！</b> (演示卡片)\n\n"
                "📌 <b>标题</b>: [日常] 测试抽奖演示贴\n"
                "👤 <b>作者</b>: NodeSeeker  |  🏷️ <b>板块</b>: #daily\n"
                "📝 <b>摘要</b>: 这是一条手动触发的 NodeSeek 测试卡片，格式与直达 HTTP 链接已配置完毕。\n\n"
                "🔗 <b>链接</b>: https://www.nodeseek.com/post-889000-1"
            )
            test_msg_sb = (
                "🎁 <b>🍪 [烧饼论坛] 发现抽奖/福利新帖！</b> (演示卡片)\n\n"
                "📌 <b>标题</b>: 〖抽奖〗新人见面礼｜抽 10 台 LAXPre Nano，65 折循环续费\n"
                "👤 <b>作者</b>: 烧饼用户  |  🏷️ <b>板块</b>: #优惠\n"
                "📝 <b>摘要</b>: 这是一条手动触发的烧饼论坛测试卡片，官方抽奖与红包标记已接入监控。\n\n"
                "🔗 <b>链接</b>: https://sb.sb/t/931/"
            )
            test_msg_kw = (
                "🎯 <b>🌐 [NodeSeek] 发现专属关注新帖！</b> (演示卡片)\n\n"
                "📌 <b>标题</b>: DMIT LAX Pro 机器补货通知与深度评测\n"
                "👤 <b>作者</b>: VPS_Master  |  🏷️ <b>板块</b>: #vps\n"
                "📝 <b>摘要</b>: 命中您的专属关注词 [DMIT]，最高优先级直通推送。\n\n"
                "🔗 <b>链接</b>: https://www.nodeseek.com/post-999999-1"
            )
            bot.send_msg(chat_id, test_msg_ns, disable_preview=False)
            bot.send_msg(chat_id, test_msg_sb, disable_preview=False)
            bot.send_msg(chat_id, test_msg_kw, disable_preview=False)
        
        else:
            bot.send_msg(chat_id, f"❓ 未识别的指令：<code>{cmd}</code>\n请输入 /help 查看可用指令列表。")
    except Exception as ie:
        print(f"[{datetime.now()}] ❌ 执行指令 {cmd} 发生内部异常: {ie}", flush=True)
        traceback.print_exc()


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
    bot.total_hit += 1
    hit_badges = {
        "lottery": "🎁 [抽奖]",
        "redpacket": "🧧 [红包]",
        "custom": "🎯 [自定义关注]",
    }
    hit_badge = hit_badges.get(hit_type, "🎁 [抽奖/福利]")
    print(f"[{datetime.now()}] {hit_badge} 命中: [{source_name}] [{category}] {title} ({hit_reason})", flush=True)

    summary = desc[:140] + ("..." if len(desc) > 140 else "")
    event_name = {"lottery": "抽奖", "redpacket": "红包", "custom": "专属关注"}.get(hit_type, "抽奖/福利")
    msg = (
        f"{hit_badge.split()[0]} <b>{source_icon} [{source_name}] 发现{event_name}新帖！</b>\n\n"
        f"📌 <b>标题</b>: {title}\n"
        f"👤 <b>作者</b>: {author}  |  🏷️ <b>板块</b>: #{category}\n"
        f"📝 <b>摘要</b>: {summary}\n\n"
        f"🔗 <b>链接</b>: {link}"
    )
    delivered = bot.send_msg(bot.admin_chat_id, msg, disable_preview=False)
    bot.record_stat("delivery_success" if delivered else "delivery_errors")
    if not delivered:
        print(f"[{datetime.now()}] ❌ Telegram 推送失败: [{source_name}] {title}", flush=True)
    return delivered


def process_nodeseek_ai_queue(bot):
    """Classify queued NodeSeek posts; API failures remain pending for retry."""
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
            retry_seconds = min(1800, 60 * (2 ** min(attempts - 1, 5)))
            record["attempts"] = attempts
            record["next_retry_at"] = now_ts + retry_seconds
            record["last_error"] = clean_for_ai(str(exc), 180)
            bot.record_stat("ai_errors")
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
            bot.record_stat("ai_giveaway_hits")
            bot.record_stat("lottery_hits")
            evidence = "；".join(result.get("evidence", [])[:2])
            reason = result.get("reason", "AI 语义判定为有效抽奖")
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
                    "lottery",
                    f"AI 双阶段语义判定（置信度 {result.get('confidence', 0):.2f}）：{reason}",
                )
        elif decision == "uncertain":
            bot.record_stat("ai_uncertain")

        bot.nodeseek_ai_history.append({
            "id": unique_id,
            "title": record.get("title", ""),
            "link": record.get("link", ""),
            "decision": decision,
            "confidence": result.get("confidence", 0),
            "reason": result.get("reason", ""),
            "reviewed": bool(result.get("reviewed")),
            "classified_at": int(time.time()),
        })
        bot.nodeseek_ai_pending.pop(unique_id, None)
        bot.remember_seen_id(unique_id)
        bot.save_nodeseek_ai_state()
        bot.save_seen_ids()
        print(
            f"[{datetime.now()}] 🧠 [NodeSeek AI] {decision} "
            f"({result.get('confidence', 0):.2f}): {record.get('title', '')}",
            flush=True,
        )


def sbsb_checkin_scheduler_thread(bot):
    """烧饼论坛每日自动签到调度线程 (北京时间每天 08:05 执行)"""
    if not bot.sbsb_cookie:
        return

    print(f"[{datetime.now()}] ⏰ 烧饼论坛每日自动签到调度器已就绪 (目标时间: 北京时间 08:05)...", flush=True)

    def get_last_checkin_date():
        if os.path.exists(CHECKIN_STATE_FILE):
            try:
                with open(CHECKIN_STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f).get("last_date", "")
            except Exception:
                return ""
        return ""

    def save_last_checkin_date(d_str):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CHECKIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_date": d_str}, f, ensure_ascii=False)

    while True:
        try:
            tz_cst = timezone(timedelta(hours=8))
            now_cst = datetime.now(tz_cst)
            today_str = now_cst.strftime("%Y-%m-%d")
            last_date = get_last_checkin_date()

            if last_date != today_str:
                if now_cst.hour >= 8 or last_date == "":
                    print(f"[{datetime.now()}] 🎯 触发烧饼论坛每日自动签到流水线...", flush=True)
                    res = do_sbsb_signin(bot.sbsb_cookie)
                    if res.get("success"):
                        save_last_checkin_date(today_str)
                        status_badge = "✅ 自动签到成功！" if not res.get("already") else "✨ 今日已自动完成签到"
                        report_msg = (
                            f"🍪 <b>烧饼论坛 (sb.sb) 每日自动签到报告</b>\n\n"
                            f"🎉 <b>签到状态</b>: {status_badge}\n"
                            f"📅 <b>连续签到</b>: <b>{res.get('consecutive_days')}</b> (累计: {res.get('total_days')})\n"
                            f"💰 <b>可用烧饼</b>: <b>{res.get('total_points')}</b>\n"
                            f"⭐ <b>成长等级</b>: <b>{res.get('level')}</b> (成长值: {res.get('exp')})\n"
                            f"🕒 <b>签到时间</b>: {res.get('signin_time')}\n\n"
                            "<i>💡 每日 08:05 (UTC+8) 定时自动执行</i>"
                        )
                        bot.send_msg(bot.admin_chat_id, report_msg)
                        print(f"[{datetime.now()}] ✅ 烧饼论坛每日自动签到执行完毕并推送通知！", flush=True)
                    else:
                        print(f"[{datetime.now()}] ⚠️ 自动签到未能成功: {res.get('msg')}", flush=True)

        except Exception as e:
            print(f"[{datetime.now()}] 签到调度器异常: {e}", flush=True)

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
                bot.send_msg(bot.admin_chat_id, report_text)

        except Exception as e:
            print(f"[{datetime.now()}] 每日报告调度异常: {e}", flush=True)

        time.sleep(300)


def sbsb_private_messages_thread(bot):
    """烧饼论坛互动通知（回复/点赞/提及）与私信轮询线程"""
    if not bot.sbsb_cookie:
        print(f"[{datetime.now()}] ℹ️ 烧饼论坛私信与通知引擎未配置 SBSB_COOKIE，监听保持挂起。", flush=True)
        return

    print(f"[{datetime.now()}] 📬 烧饼论坛 (sb.sb) 互动通知与私信监听引擎已启动...", flush=True)
    first_run = len(bot.seen_msgs) == 0

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def http_error_302(self, req, fp, code, msg, headers):
            return fp
        def http_error_303(self, req, fp, code, msg, headers):
            return fp

    opener = urllib.request.build_opener(NoRedirectHandler)

    def get_user_uid():
        if bot.sbsb_uid:
            return bot.sbsb_uid
        try:
            req = urllib.request.Request(
                "https://sb.sb/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Cookie": bot.sbsb_cookie,
                    "Referer": "https://sb.sb/"
                }
            )
            resp = opener.open(req, timeout=12)
            html_text = resp.read().decode("utf-8", errors="ignore")
            uid_m = re.search(r'/u/(\d+)/\?tab=notifications', html_text) or re.search(r'/u/(\d+)/', html_text)
            if uid_m:
                bot.sbsb_uid = uid_m.group(1)
                print(f"[{datetime.now()}] 🆔 成功识别烧饼论坛当前用户 UID: {bot.sbsb_uid}", flush=True)
                return bot.sbsb_uid
        except Exception as ue:
            print(f"[{datetime.now()}] 获取 UID 失败: {ue}", flush=True)
        return "7069"

    while True:
        try:
            uid = get_user_uid()
            notif_url = f"https://sb.sb/u/{uid}/?tab=notifications"

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
                resp_notif = opener.open(req_notif, timeout=15)
                status_code = getattr(resp_notif, "status", getattr(resp_notif, "code", 200))
                location = resp_notif.headers.get("Location", "")
                if status_code in [302, 303] and ("login" in location or "/login" in location):
                    now_ts = time.time()
                    if now_ts - bot.last_cookie_warn_time > 43200:
                        bot.last_cookie_warn_time = now_ts
                        warn_msg = (
                            "⚠️ <b>🍪 [烧饼论坛] 登录凭据 (SBSB_COOKIE) 已过期！</b>\n\n"
                            "互动通知推送与自动签到已暂停。请在电脑浏览器重新登录烧饼论坛并复制 Cookie，更新至 VPS 的 <code>.env</code> 中。"
                        )
                        bot.send_msg(bot.admin_chat_id, warn_msg)
                        print(f"[{datetime.now()}] ⚠️ 烧饼论坛 Cookie 已过期 (重定向至 {location})", flush=True)
                    time.sleep(DEFAULT_PRIVATE_INTERVAL)
                    continue

                html_notif = resp_notif.read().decode("utf-8", errors="ignore")
            except Exception as fe:
                print(f"[{datetime.now()}] 拉取烧饼通知失败: {fe}", flush=True)
                time.sleep(DEFAULT_PRIVATE_INTERVAL)
                continue

            # 解析通知列表
            notif_blocks = re.findall(r'<li[^>]*class=\"[^\"]*notification-item[^\"]*\"[^>]*>(.*?)(?=<li[^>]*class=\"[^\"]*notification-item|$)', html_notif, re.DOTALL)
            
            for block in reversed(notif_blocks):
                user_match = re.search(r'<a[^>]*class=\"[^\"]*post-title[^\"]*\"[^>]*>([^<]+)</a>', block)
                user = user_match.group(1).strip() if user_match else "烧饼用户"

                kind_match = re.search(r'<span[^>]*class=\"[^\"]*notification-kind[^\"]*\"[^>]*>([^<]+)</span>', block)
                kind = kind_match.group(1).strip() if kind_match else "提醒"

                time_match = re.search(r'<time datetime=\"([^\"]+)\">([^<]+)</time>', block)
                iso_time = time_match.group(1).strip() if time_match else ""
                rel_time = time_match.group(2).strip() if time_match else "刚刚"

                content_match = re.search(r'<div[^>]*class=\"[^\"]*notification-content[^\"]*\"[^>]*>(.*?)</div>', block, re.DOTALL)
                if content_match:
                    content_raw = content_match.group(1)
                    content = re.sub(r'<[^>]+>', ' ', content_raw)
                    content = re.sub(r'\s+', ' ', content).strip()
                else:
                    content = "收到一条新的互动提醒"

                link_match = re.search(r'<a[^>]*class=\"[^\"]*notification-reply-action[^\"]*\"[^>]*href=\"([^\"]+)\"', block) or re.search(r'href=\"(/t/\d+/[^\"]*)\"', block)
                link = f"https://sb.sb{link_match.group(1)}" if link_match else f"https://sb.sb/u/{uid}/?tab=notifications"

                unique_key = f"notif:{iso_time}_{user}_{kind}"

                if first_run:
                    bot.remember_seen_msg(unique_key)
                    continue

                if bot.remember_seen_msg(unique_key):
                    bot.total_private_notified += 1
                    bot.record_stat("private_notified")
                    print(f"[{datetime.now()}] 📬 命中烧饼论坛新互动通知: [{kind}] {user} - {content}", flush=True)

                    msg_card = (
                        f"📬 <b>🍪 [烧饼论坛] 收到新的互动通知！</b>\n\n"
                        f"👤 <b>用户</b>: {user}  |  🏷️ <b>类型</b>: #{kind}\n"
                        f"💬 <b>内容</b>: {content}\n"
                        f"🕒 <b>时间</b>: {rel_time}\n\n"
                        f"🔗 <b>直达链接</b>: {link}"
                    )
                    bot.send_msg(bot.admin_chat_id, msg_card, disable_preview=False)

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
                        bot.total_private_notified += 1
                        bot.record_stat("private_notified")
                        print(f"[{datetime.now()}] 📬 发现烧饼论坛新私信对话: {thread_id}", flush=True)

                        msg_text = (
                            "📬 <b>🍪 [烧饼论坛] 收到新的私信对话！</b>\n\n"
                            f"💬 <b>对话编号</b>: <code>{thread_id[:16]}...</code>\n"
                            "💡 <i>您有新的私信消息，请点击下方直达链接查看。</i>\n\n"
                            f"🔗 <b>直达链接</b>: https://sb.sb/messages/{thread_id}/"
                        )
                        bot.send_msg(bot.admin_chat_id, msg_text, disable_preview=False)
            except Exception as me:
                print(f"[{datetime.now()}] 拉取私信异常: {me}", flush=True)

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
                    for badge_kind, path in (("lottery", "/lottery/"), ("redpacket", "/redpacket/")):
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
                            deliver_public_match(
                                bot,
                                "烧饼论坛",
                                "🍪",
                                topic["category"],
                                topic["title"],
                                topic["author"],
                                f"论坛主题列表已显示官方{label}标记，正文可能仅登录用户可见。",
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
