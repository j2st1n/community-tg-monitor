#!/usr/bin/env python3
import os
import sys
import time
import json
import re
import traceback
import threading
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SEEN_IDS_FILE = os.path.join(DATA_DIR, "seen_ids.json")
SEEN_MSGS_FILE = os.path.join(DATA_DIR, "seen_msgs.json")
CHECKIN_STATE_FILE = os.path.join(DATA_DIR, "checkin_state.json")
DAILY_STATS_FILE = os.path.join(DATA_DIR, "daily_stats.json")

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

# 默认用户自定义关键词（初始为空，抽奖活动由内置决策树全自动监控）
DEFAULT_KEYWORDS = []

# 默认屏蔽词
DEFAULT_BLOCKWORDS = ["收", "求", "买", "询", "出", "出出", "出台", "出个", "出只", "求购", "慢收", "溢价", "剩余价值"]

BOT_COMMANDS = [
    {"command": "status", "description": "📊 监控状态与运行统计"},
    {"command": "report", "description": "📈 今日算法过滤与成功率日报"},
    {"command": "sources", "description": "📡 监控网站独立推送开关"},
    {"command": "signin", "description": "🍪 烧饼论坛一键签到与查分"},
    {"command": "keywords", "description": "🎯 查看并管理自定义关注词"},
    {"command": "blocks", "description": "🚫 查看并管理屏蔽词"},
    {"command": "pause", "description": "⏸️ 全局暂停推送"},
    {"command": "resume", "description": "▶️ 全局恢复推送"},
    {"command": "test", "description": "🧪 发送测试卡片"},
    {"command": "help", "description": "📖 显示帮助菜单"}
]

LEGACY_BUILTIN_WORDS = {
    "抽奖", "抽", "福利", "roll", "Roll", "ROLL",
    "送只", "送个", "送台", "送一", "白送", "直接送", "先到先得", "免费送", "送小鸡", "送机器", "送码",
    "口令", "红包", "开奖", "盖楼", "中奖", "白嫖", "免费"
}

def clean_title_prefix(title):
    """去除标题开头的括号和特殊标点符号，如 '【出】' -> '出】'"""
    return re.sub(r'^[【\[\(（〖\s]+', '', title)

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

        self.lock = threading.Lock()
        self.start_time = datetime.now()
        self.paused = False
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
        
        self.load_settings()
        self.seen_ids = self.load_seen_ids()
        self.seen_msgs = self.load_seen_msgs()
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
            "trade_blocked": 0,
            "noise_blocked": 0,
            "private_notified": 0,
            "poll_success": 0,
            "poll_errors": 0
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
            except Exception:
                pass
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
                    "trade_blocked": 0,
                    "noise_blocked": 0,
                    "private_notified": 0,
                    "poll_success": 0,
                    "poll_errors": 0
                }
            self.daily_stats[key] = self.daily_stats.get(key, 0) + count
            self.save_daily_stats()

    def register_telegram_commands(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        payload = {"commands": BOT_COMMANDS}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/4.7"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    print(f"[{datetime.now()}] ✅ Telegram 官方快捷指令菜单已自动注册！", flush=True)
                    return True
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ 注册菜单失败: {e}", flush=True)
        return False

    def load_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    raw_keywords = data.get("keywords", [])
                    self.keywords = [k for k in raw_keywords if k not in LEGACY_BUILTIN_WORDS]
                    self.blockwords = data.get("blockwords", DEFAULT_BLOCKWORDS)
                    self.poll_interval = data.get("poll_interval", DEFAULT_POLL_INTERVAL)
                    self.paused = data.get("paused", False)
                    saved_source_states = data.get("source_states", {})
                    for s in self.sources:
                        sid = s["id"]
                        if sid in saved_source_states:
                            self.source_states[sid] = bool(saved_source_states[sid])
                    return
            except Exception as e:
                print(f"[{datetime.now()}] 读取 settings.json 异常: {e}", flush=True)
        
        self.keywords = list(DEFAULT_KEYWORDS)
        self.blockwords = list(DEFAULT_BLOCKWORDS)
        self.poll_interval = DEFAULT_POLL_INTERVAL
        self.paused = False
        self.source_states = {s["id"]: True for s in self.sources}
        self.save_settings()

    def save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "keywords": self.keywords,
            "blockwords": self.blockwords,
            "poll_interval": self.poll_interval,
            "paused": self.paused,
            "source_states": self.source_states
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def is_source_enabled(self, source_id):
        with self.lock:
            return self.source_states.get(source_id, True)

    def toggle_source(self, source_id):
        with self.lock:
            current = self.source_states.get(source_id, True)
            self.source_states[source_id] = not current
            self.save_settings()
            return self.source_states[source_id]

    def load_seen_ids(self):
        if os.path.exists(SEEN_IDS_FILE):
            try:
                with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def save_seen_ids(self):
        id_list = list(self.seen_ids)[-1000:]
        with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(id_list, f, ensure_ascii=False)

    def load_seen_msgs(self):
        if os.path.exists(SEEN_MSGS_FILE):
            try:
                with open(SEEN_MSGS_FILE, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def save_seen_msgs(self):
        id_list = list(self.seen_msgs)[-500:]
        with open(SEEN_MSGS_FILE, "w", encoding="utf-8") as f:
            json.dump(id_list, f, ensure_ascii=False)

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
                headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/4.7"}
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
            headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/4.7"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"[{datetime.now()}] 编辑消息失败: {e}", flush=True)
            return False

    def answer_callback_query(self, callback_query_id, text):
        api_url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id, "text": text, "show_alert": False}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/4.7"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def evaluate_post(self, source_name, cat, title, desc):
        """
        全口径高精度抽奖、福利与红包意图决策树算法（带全量审计日志输出）
        返回: (is_matched: bool, category_tag: str, reason: str)
        """
        clean_t = clean_title_prefix(title)

        with self.lock:
            # 0. 烧饼论坛官方抽奖专区/标签绝对直通 (Authority Match)
            if source_name == "烧饼论坛" and (cat == "抽奖" or "抽奖" in cat or clean_t.startswith("〖抽奖〗")):
                self.record_stat("lottery_hits")
                return True, "lottery", "命中烧饼论坛官方抽奖专区/标签"

            # 1. 第一层：硬性买卖求购过滤
            for bw in self.blockwords:
                if bw and (clean_t.startswith(bw) or title.startswith(bw)):
                    self.record_stat("trade_blocked")
                    print(f"[{datetime.now()}] 🚫 [买卖拦截] [{source_name}] {title} (前缀: {bw})", flush=True)
                    return False, "trade_blocked", f"命中买卖前缀 [{bw}]"

            if any(w in title for w in ['剩余价值', '求收', '收个', '出个全新', '求一个', '求推荐', '收一台', '出台', '出只', '慢出', '慢收', '带价']):
                self.record_stat("trade_blocked")
                print(f"[{datetime.now()}] 🚫 [买卖拦截] [{source_name}] {title} (交易词汇)", flush=True)
                return False, "trade_blocked", "命中交易词汇"

            # 2. 第二层：非抽奖意图词过滤（商业新闻/抽卡/比喻/求助）
            anti_intent_words = [
                '抽成', '抽水', '抽烟', '抽风', '抽空', '抽卡', '抽签', '抽检', '抽屉', '抽走',
                '如抽奖', '像抽奖', '当抽奖', '中奖了', '中过奖', '怎么填写', '怎么填', '如何填',
                '如何获得', '如何免费', '怎样免费', '推荐入坑', '的选择', '有套路吗', '清退', '谈判'
            ]
            for w in anti_intent_words:
                if w in title:
                    self.record_stat("noise_blocked")
                    print(f"[{datetime.now()}] 🚫 [噪音拦截] [{source_name}] {title} (意图干扰: {w})", flush=True)
                    return False, "noise_blocked", f"命中干扰意图 [{w}]"

            # 3. 疑问句拦截
            if re.search(r'(吗|么|呢|？|\?)$', title.strip()) and not re.search(r'[【\[〖]抽奖[】\]〗]', title):
                if any(qw in title for qw in ['怎么', '如何', '还能', '有没有', '谁有', '什么好', '哪个好', '推荐']):
                    self.record_stat("noise_blocked")
                    print(f"[{datetime.now()}] 🚫 [噪音拦截] [{source_name}] {title} (疑问咨询句式)", flush=True)
                    return False, "noise_blocked", "命中疑问咨询句式"

            # 4. 第三层：黄金强特征（全口径覆盖：抽/送/roll/红包/福利）
            gold_patterns = [
                r'^[【\[〖\s]*抽奖',
                r'[【\[〖]抽奖[】\]〗]',
                r'抽奖[🎉🔥🎁]',
                r'[🎉🔥🎁]抽奖',
                r'抽\s*(?:\d+|[一两三四五六七八九十]|台|个|只|位|份|张|条|组|点|些|波|\$|刀|元|u|U|烧饼)',
                r'抽(?:一台|一个|只小鸡|台小鸡|机器|激活码|兑换码|体验金|年付|月付|烧饼|域名)',
                r'抽(?:选|出|送)\s*(?:\d+|[一两三四五六七八九十])',
                r'^[【\[\(（〖\s]*(?:送|免费送|白送|直接送|送只|送个|送台|送一|送点|送波|发红包|开红包)',
                r'给大家送点',
                r'红包(?:帖|第[一二三四五六七八九十\d]+弹)',
                r'玩(?:一玩)?红包',
                r'口令红包',
                r'盖楼抽',
                r'回帖抽',
                r'评论区?留.*?(?:抽|送)',
                r'\broll\s*(?:一个|一台|只|点|\d+|机|个|波)',
                r'自选一台',
                r'先到先得'
            ]
            for pat in gold_patterns:
                if re.search(pat, title, re.IGNORECASE):
                    self.record_stat("lottery_hits")
                    return True, "lottery", "命中抽奖强特征"

            if '抽奖' in title and any(k in title for k in ['开奖', '送', '第一期', '第二期', '第三期', '见者有份', '福利', '奖品', '吧', '活动']):
                self.record_stat("lottery_hits")
                return True, "lottery", "命中抽奖组合特征"

            # 5. 用户自定义专属关注词
            for c_kw in self.keywords:
                if c_kw and (c_kw in title or c_kw in desc):
                    self.record_stat("custom_hits")
                    return True, "custom", f"命中自定义词 [{c_kw}]"

            # 6. 正文强抽奖规则检测
            desc_clean = desc.replace('送中', '').replace('没送中', '').replace('未送中', '')
            desc_lottery_patterns = [
                r'评论区?留.*?(?:抽|送|中奖)',
                r'本帖(?:回复|盖楼).*?抽',
                r'开\s*\d+\s*份红包',
                r'开奖时间.*?(?:\d+|随机)',
                r'随机抽\s*\d+\s*(?:位|个|台)',
                r'截止时间.*?(?:抽|开奖)',
            ]
            for pat in desc_lottery_patterns:
                if re.search(pat, desc_clean):
                    self.record_stat("lottery_hits")
                    return True, "lottery", "命中正文抽奖规则"

        return False, "none", "未达触发阈值"

    def format_keywords_card(self):
        """纯粹的自定义专属关注词管理卡片"""
        with self.lock:
            kws = list(self.keywords)

        if kws:
            pills = " · ".join([f"<code>{k}</code>" for k in kws])
            body = f"🏷️ <b>正在关注的专属词</b> ({len(kws)} 个)\n{pills}"
        else:
            body = "<i>（暂无自定义词，点击下方按钮添加你想关注的商家、机型或线路）</i>"

        text = (
            f"🎯 <b>自定义关注词库</b> (共 {len(kws)} 个)\n\n"
            f"{body}\n\n"
            "💡 <i>提示：全网抽奖、送机与福利贴由内置智能引擎全自动监听，无需在此添加。此处仅用于添加你关心的特价、商家或捡漏词（如 <code>搬瓦工</code>、<code>9929</code>、<code>传家宝</code>）。</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "➕ 添加关注词", "callback_data": "menu:add_kw"},
                    {"text": "🗑️ 按钮删除词", "callback_data": "menu:del_kw"}
                ]
            ]
        }
        return text, markup

    def format_blocks_card(self):
        with self.lock:
            bws = list(self.blockwords)
        
        if bws:
            pills = " · ".join([f"<code>{b}</code>" for b in bws])
            body = f"🚫 <b>生效中的屏蔽词</b> ({len(bws)} 个)\n{pills}"
        else:
            body = "<i>（当前无屏蔽词）</i>"

        text = (
            f"🛡️ <b>噪音过滤屏蔽库</b>\n\n"
            f"{body}\n\n"
            "<i>💡 标题包含以上词汇或以其为前缀（如 【出】、【收】）的帖子将自动忽略</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "➕ 添加屏蔽词", "callback_data": "menu:add_bw"},
                    {"text": "🗑️ 按钮解除屏蔽", "callback_data": "menu:del_bw"}
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
        """生成每日运行统计与算法成功率报告卡片（无锁安全快照）"""
        stats = dict(self.daily_stats)
        uptime = datetime.now() - self.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        
        scanned = stats.get("total_scanned", 0)
        lottery_hits = stats.get("lottery_hits", 0)
        custom_hits = stats.get("custom_hits", 0)
        trade_blocked = stats.get("trade_blocked", 0)
        noise_blocked = stats.get("noise_blocked", 0)
        priv_notified = stats.get("private_notified", 0)
        poll_success = stats.get("poll_success", 0)
        poll_errors = stats.get("poll_errors", 0)

        total_polls = poll_success + poll_errors
        success_rate = (poll_success / total_polls * 100) if total_polls > 0 else 100.0
        total_blocked = trade_blocked + noise_blocked
        date_str = stats.get("date", self.get_today_cst())

        # 格式化活跃源状态
        src_status = []
        for s in self.sources:
            is_on = self.source_states.get(s["id"], True)
            src_status.append(f"{s['icon']} {s['name']}: {'🟢' if is_on else '🔴'}")
        src_line = "  ".join(src_status)

        text = (
            "📈 <b>多社区监控每日运行与算法健康度报告</b>\n"
            f"📅 <b>统计日期</b>: <code>{date_str}</code> (周期: 24h)\n\n"
            "🔍 <b>公开帖子扫描与过滤分析</b>\n"
            f"• 📊 <b>全网新发主题</b>: <b>{scanned}</b> 篇\n"
            f"• 📡 <b>高频轮询巡检</b>: <b>{poll_success}</b> 轮次 (每30s双源扫描)\n"
            f"• 🎁 <b>精准抽奖命中</b>: <b>{lottery_hits}</b> 篇 (100% 决策树交付)\n"
            f"• 🏷️ <b>自定义词命中</b>: <b>{custom_hits}</b> 篇\n"
            f"• 🛡️ <b>噪音负向拦截</b>: <b>{total_blocked}</b> 篇 (交易 {trade_blocked} / 噪音 {noise_blocked})\n"
            f"• 📬 <b>私信与互动通知</b>: <b>{priv_notified}</b> 次\n\n"
            "🌐 <b>各站点推送开关</b>\n"
            f"• {src_line}\n\n"
            "⚙️ <b>系统守护健康度</b>\n"
            f"• ⏱️ <b>连续运行</b>: {hours}小时 {minutes}分\n"
            f"• 📡 <b>RSS 巡检成功率</b>: <b>{success_rate:.1f}%</b> ({poll_success} 成功 / {poll_errors} 异常)\n"
            f"• 🎯 <b>已去重索引容量</b>: {len(self.seen_ids)} 篇帖 / {len(self.seen_msgs)} 条通知\n\n"
            "<i>💡 每日 22:00 (UTC+8) 自动总结推送，随时输入 /report 查阅实时数据</i>"
        )
        return text

def make_keyword_buttons(keywords, prefix="del_kw"):
    keyboard = []
    row = []
    for kw in keywords:
        btn = {"text": f"❌ {kw}", "callback_data": f"{prefix}:{kw}"}
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([{"text": "🔙 返回列表", "callback_data": f"{prefix}:__back__"}])
    return {"inline_keyboard": keyboard}

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
        "Referer": "https://sb.sb/"
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

    csrf_match = re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', html_signin) or re.search(r'value=["\']([^"\']+)["\']\s+name=["\']_csrf["\']', html_signin)
    if csrf_match and ("今日已签" not in html_signin and "明日再来" not in html_signin):
        csrf_token = csrf_match.group(1)
        post_data = urllib.parse.urlencode({"_csrf": csrf_token}).encode("utf-8")
        post_headers = dict(headers)
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"
        post_headers["Referer"] = "https://sb.sb/signin/"
        try:
            post_req = urllib.request.Request("https://sb.sb/signin/", data=post_data, headers=post_headers)
            post_resp = opener.open(post_req, timeout=15)
            html_signin = post_resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[{datetime.now()}] 提交签到请求异常: {e}", flush=True)

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

        signin_time_m = re.search(r'<time datetime=\"([^\"]+)\"[^>]*>([^<]+)</time>\s*</td>\s*<td>\s*每日签到', html_points)
        if signin_time_m:
            iso_str = signin_time_m.group(1)
            try:
                dt_utc = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
                dt_cst = dt_utc.astimezone(timezone(timedelta(hours=8)))
                signin_time_str = dt_cst.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                signin_time_str = signin_time_m.group(2)
    except Exception as pe:
        print(f"[{datetime.now()}] 访问 points 页面异常: {pe}", flush=True)

    if not signin_time_str:
        tz_cst = timezone(timedelta(hours=8))
        signin_time_str = datetime.now(tz_cst).strftime('%Y-%m-%d %H:%M:%S')

    flash_match = re.search(r'window\.__pageFlash=["\']([^"\']*)["\']', html_signin) or re.search(r'class=["\'][^"\']*toast[^"\']*["\']>([^<]+)<', html_signin)
    flash_msg = flash_match.group(1).strip() if flash_match and flash_match.group(1).strip() else "签到成功"

    already = "已签到" in flash_msg or "明日再来" in html_signin or "今日已签" in html_signin

    return {
        "success": True,
        "already": already,
        "msg": flash_msg,
        "consecutive_days": f"{streak_days} 天",
        "total_days": f"{total_days} 天",
        "total_points": f"{points} 饼",
        "exp": exp,
        "level": level,
        "signin_time": signin_time_str
    }

def handle_callback_query(bot, query):
    query_id = query["id"]
    from_user = str(query.get("from", {}).get("id", ""))
    message = query.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    msg_id = message.get("message_id")
    data = query.get("data", "")

    if from_user != bot.admin_chat_id:
        bot.answer_callback_query(query_id, "⚠️ 无权操作")
        return

    print(f"[{datetime.now()}] 🔘 点击按钮: '{data}'", flush=True)

    if data.startswith("toggle_src:"):
        target_src = data.split("toggle_src:", 1)[1]
        new_state = bot.toggle_source(target_src)
        state_text = "开启 🟢" if new_state else "关闭 🔴"
        bot.answer_callback_query(query_id, f"已切换为: {state_text}")
        text, markup = bot.format_sources_card()
        bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data == "menu:add_kw":
        bot.user_states[chat_id] = "waiting_for_add"
        bot.answer_callback_query(query_id, "请直接输入新关键词")
        bot.send_msg(chat_id, "➕ <b>请输入你想添加的自定义关注词：</b>\n<i>（支持一次输入多个词，用空格分隔，例如：<code>搬瓦工 9929 传家宝</code>）</i>")

    elif data == "menu:del_kw":
        bot.answer_callback_query(query_id, "请点击按钮删除")
        with bot.lock:
            kws = list(bot.keywords)
        if not kws:
            bot.edit_msg_text(chat_id, msg_id, "⚠️ <b>当前没有已添加的自定义关注词。</b>")
        else:
            text = f"🗑️ <b>请点击下方按钮删除对应的关注词（共 {len(kws)} 个）：</b>"
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=make_keyword_buttons(kws, "del_kw"))

    elif data == "menu:add_bw":
        bot.user_states[chat_id] = "waiting_for_block"
        bot.answer_callback_query(query_id, "请直接输入新屏蔽词")
        bot.send_msg(chat_id, "⛔ <b>请输入你想添加的屏蔽词：</b>\n<i>（例如输入：<code>慢收 求购 出</code>）</i>")

    elif data == "menu:del_bw":
        bot.answer_callback_query(query_id, "请点击按钮解除")
        with bot.lock:
            bws = list(bot.blockwords)
        if not bws:
            bot.edit_msg_text(chat_id, msg_id, "⚠️ <b>当前没有已添加的屏蔽词。</b>")
        else:
            text = f"🗑️ <b>请点击下方按钮解除对应的屏蔽词（共 {len(bws)} 个）：</b>"
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=make_keyword_buttons(bws, "del_bw"))

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
            
            if bot.keywords:
                text = f"🗑️ <b>请点击下方按钮删除对应的关注词（剩余 {len(bot.keywords)} 个）：</b>"
                bot.edit_msg_text(chat_id, msg_id, text, reply_markup=make_keyword_buttons(bot.keywords, "del_kw"))
            else:
                text, markup = bot.format_keywords_card()
                bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

    elif data.startswith("del_bw:"):
        target_bw = data.split("del_bw:", 1)[1]
        if target_bw == "__back__":
            bot.answer_callback_query(query_id, "返回")
            text, markup = bot.format_blocks_card()
            bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)
            return
        
        with bot.lock:
            if target_bw in bot.blockwords:
                bot.blockwords.remove(target_bw)
                bot.save_settings()
                bot.answer_callback_query(query_id, f"已解除: {target_bw}")
            else:
                bot.answer_callback_query(query_id, "该词已不存在")
            
            if bot.blockwords:
                text = f"🗑️ <b>请点击下方按钮解除对应的屏蔽词（剩余 {len(bot.blockwords)} 个）：</b>"
                bot.edit_msg_text(chat_id, msg_id, text, reply_markup=make_keyword_buttons(bot.blockwords, "del_bw"))
            else:
                text, markup = bot.format_blocks_card()
                bot.edit_msg_text(chat_id, msg_id, text, reply_markup=markup)

def handle_command_or_text(bot, chat_id, text):
    text = text.strip()

    if chat_id in bot.user_states and not text.startswith("/"):
        state = bot.user_states.pop(chat_id)
        if state == "waiting_for_add":
            new_kws = [k for k in text.split() if k not in LEGACY_BUILTIN_WORDS]
            added = []
            with bot.lock:
                for kw in new_kws:
                    if kw not in bot.keywords:
                        bot.keywords.append(kw)
                        added.append(kw)
                bot.save_settings()
            if added:
                msg_text, markup = bot.format_keywords_card()
                bot.send_msg(chat_id, f"✅ <b>已成功添加 {len(added)} 个关注词：</b>\n<code>{', '.join(added)}</code>\n\n{msg_text}", reply_markup=markup)
            else:
                bot.send_msg(chat_id, "⚠️ 所输入的关键词均已存在。")
            return

        elif state == "waiting_for_block":
            new_bws = text.split()
            added = []
            with bot.lock:
                for bw in new_bws:
                    if bw not in bot.blockwords:
                        bot.blockwords.append(bw)
                        added.append(bw)
                bot.save_settings()
            if added:
                msg_text, markup = bot.format_blocks_card()
                bot.send_msg(chat_id, f"🚫 <b>已成功添加 {len(added)} 个屏蔽词：</b>\n<code>{', '.join(added)}</code>\n\n{msg_text}", reply_markup=markup)
            else:
                bot.send_msg(chat_id, "⚠️ 所输入的屏蔽词均已存在。")
            return

    parts = text.split(maxsplit=1)
    raw_cmd = parts[0].lower()
    cmd = raw_cmd.split('@')[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    print(f"[{datetime.now()}] 📨 处理指令: '{text}' (识别命令: {cmd})", flush=True)

    try:
        if cmd in ["/start", "/help"]:
            sources_desc = "、".join([f"{s['icon']} {s['name']}" for s in bot.sources])
            sbsb_private_desc = "🟢 已启用" if bot.sbsb_cookie else "⚪ 未配置 (可选)"
            help_text = (
                f"🤖 <b>多社区抽奖与热帖监控 Bot 指令中心</b>\n\n"
                f"📡 <b>当前公开源</b>: {sources_desc}\n"
                f"📬 <b>烧饼私信/签到引擎</b>: {sbsb_private_desc}\n\n"
                "📊 <b>状态与报告</b>\n"
                "├ /status - 监控运行统计与健康报告\n"
                "├ /report - 📈 <b>今日算法过滤与成功率日报</b>\n"
                "├ /sources - 📡 <b>各网站推送独立开关管理</b>\n"
                "├ /signin - 🍪 <b>烧饼论坛一键签到与查分</b>\n"
                "├ /keywords - 🎯 <b>查看并管理自定义关注词</b>\n"
                "└ /blocks - 🚫 <b>查看并交互式管理屏蔽词</b>\n\n"
                "⚙️ <b>快捷控制</b>\n"
                "├ /pause - 全局暂停推送通知 (免打扰)\n"
                "├ /resume - 全局恢复推送通知\n"
                "└ /test - 发送格式测试卡片"
            )
            res = bot.send_msg(chat_id, help_text)
            print(f"[{datetime.now()}] ✅ 指令 {cmd} 发送结果: {res}", flush=True)

        elif cmd in ["/report", "/daily"]:
            report_text = bot.generate_daily_report_card()
            res = bot.send_msg(chat_id, report_text)
            print(f"[{datetime.now()}] ✅ 指令 {cmd} 发送结果: {res}", flush=True)

        elif cmd in ["/sources", "/sites"]:
            text_card, markup = bot.format_sources_card()
            res = bot.send_msg(chat_id, text_card, reply_markup=markup)
            print(f"[{datetime.now()}] ✅ 指令 {cmd} 发送结果: {res}", flush=True)

        elif cmd in ["/signin", "/checkin"]:
            bot.send_msg(chat_id, "⏳ <b>正在连接烧饼论坛执行签到与资产同步...</b>")
            res = do_sbsb_signin(bot.sbsb_cookie)
            if res.get("success"):
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

        elif cmd == "/status":
            uptime = datetime.now() - bot.start_time
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            
            sources_list = []
            for s in bot.sources:
                sid = s["id"]
                is_on = bot.source_states.get(sid, True)
                status_tag = "🟢 开启" if is_on else "🔴 已暂停"
                sources_list.append(f"  • {s['icon']} <b>{s['name']}</b> ({status_tag})")
            sources_text = "\n".join(sources_list)

            sbsb_private_status = "🟢 实时运行中（含每日 08:05 自动签到与通知）" if bot.sbsb_cookie else "⚪ 未配置 SBSB_COOKIE"
            status_text = (
                "📊 <b>社区监控守护状态报告</b>\n\n"
                f"⏱️ <b>运行时间</b>: {hours}小时 {minutes}分 {seconds}秒\n"
                f"🔔 <b>全局推送状态</b>: {'⏸️ 全局已暂停' if bot.paused else '▶️ 运行中'}\n"
                f"📡 <b>轮询周期</b>: 每 {bot.poll_interval} 秒\n"
                f"🌐 <b>监控网站状态 ({len(bot.sources)})</b>:\n{sources_text}\n"
                f"📬 <b>烧饼私信/签到引擎</b>: {sbsb_private_status}\n"
                f"🎯 <b>自定义关注词数</b>: {len(bot.keywords)} 个\n"
                f"🚫 <b>屏蔽词数</b>: {len(bot.blockwords)} 个\n"
                f"📈 <b>已扫描去重库</b>: {len(bot.seen_ids)} 篇公开帖 / {len(bot.seen_msgs)} 条私信与通知\n"
                f"🎁 <b>累计公开帖命中</b>: {bot.total_hit} 篇\n"
                f"💌 <b>累计互动通知</b>: {bot.total_private_notified} 次\n\n"
                "<i>💡 输入 /report 查看算法过滤日报，输入 /sources 开关各网站推送</i>"
            )
            bot.send_msg(chat_id, status_text)

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
                bot.send_msg(chat_id, f"✅ 已添加：<code>{', '.join(added)}</code>\n\n{text_card}", reply_markup=markup)
                return
            
            text_card, markup = bot.format_keywords_card()
            bot.send_msg(chat_id, text_card, reply_markup=markup)

        elif cmd in ["/blocks", "/block", "/delblock"]:
            if cmd == "/block" and arg:
                with bot.lock:
                    if arg not in bot.blockwords:
                        bot.blockwords.append(arg)
                        bot.save_settings()
                text_card, markup = bot.format_blocks_card()
                bot.send_msg(chat_id, f"🚫 已添加屏蔽：<code>{arg}</code>\n\n{text_card}", reply_markup=markup)
                return
                
            text_card, markup = bot.format_blocks_card()
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
                "📝 <b>摘要</b>: 这是一条手动触发的烧饼论坛测试卡片，抽奖专区与全站流已全部接入监控。\n\n"
                "🔗 <b>链接</b>: https://sb.sb/t/931/"
            )
            test_msg_pm = (
                "📬 <b>🍪 [烧饼论坛] 收到新的互动通知！</b> (演示卡片)\n\n"
                "👤 <b>用户</b>: 西风  |  🏷️ <b>类型</b>: #回复\n"
                "💬 <b>内容</b>: 回复了你的主题：人多了就卡，毕竟这个程序很简单 @Xshell #2\n"
                "🕒 <b>时间</b>: 3小时前\n\n"
                "🔗 <b>直达链接</b>: https://sb.sb/t/2028/?reply_id=25485"
            )
            test_msg_signin = (
                "🍪 <b>烧饼论坛 (sb.sb) 签到与资产报告</b> (演示卡片)\n\n"
                "🎉 <b>签到状态</b>: ✨ 今日已完成签到\n"
                "📅 <b>连续签到</b>: <b>2 天</b> (累计: 3 天)\n"
                "💰 <b>可用烧饼</b>: <b>45 饼</b>\n"
                "⭐ <b>成长等级</b>: <b>Lv.2 新手上路</b> (成长值: 61)\n"
                "🕒 <b>签到时间</b>: 2026-08-26 09:55:35\n\n"
                "<i>💡 系统将在每日 08:05 (UTC+8) 自动执行定时签到</i>"
            )
            bot.send_msg(chat_id, test_msg_ns, disable_preview=False)
            bot.send_msg(chat_id, test_msg_sb, disable_preview=False)
            bot.send_msg(chat_id, test_msg_pm, disable_preview=False)
            bot.send_msg(chat_id, test_msg_signin, disable_preview=False)
        
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
            req = urllib.request.Request(url, headers={"User-Agent": "Community-Monitor-Bot/4.7"})
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
                            if from_user == bot.admin_chat_id and text:
                                try:
                                    handle_command_or_text(bot, from_user, text)
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
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (CommunityFeed/4.7)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return ET.fromstring(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[{datetime.now()}] RSS 拉取异常 ({url}): {e}", flush=True)
        return None

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

            # 每天 22:00 执行一次推送
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
            html = resp.read().decode("utf-8", errors="ignore")
            uid_m = re.search(r'/u/(\d+)/\?tab=notifications', html) or re.search(r'/u/(\d+)/', html)
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
                    bot.seen_msgs.add(unique_key)
                    continue

                if unique_key not in bot.seen_msgs:
                    bot.seen_msgs.add(unique_key)
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
                        bot.seen_msgs.add(thread_key)
                        continue

                    if thread_key not in bot.seen_msgs:
                        bot.seen_msgs.add(thread_key)
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

                    bot.seen_ids.add(unique_id)
                    bot.total_checked += 1
                    bot.record_stat("total_scanned")

                    if first_run or not is_enabled:
                        continue

                    # 第二代全口径高精度抽奖意图过滤与分类决策
                    is_hit, hit_type, hit_reason = bot.evaluate_post(source_name, cat, title, desc)
                    if not bot.paused and is_hit:
                        bot.total_hit += 1
                        hit_badge = "🎁 [抽奖/福利]" if hit_type == "lottery" else "🎯 [自定义关注]"
                        print(f"[{datetime.now()}] {hit_badge} 命中: [{source_name}] [{cat}] {title} ({hit_reason})", flush=True)
                        
                        summary = desc[:140] + ("..." if len(desc) > 140 else "")
                        msg = (
                            f"🎁 <b>{source_icon} [{source_name}] 发现抽奖/福利新帖！</b>\n\n"
                            f"📌 <b>标题</b>: {title}\n"
                            f"👤 <b>作者</b>: {author}  |  🏷️ <b>板块</b>: #{cat}\n"
                            f"📝 <b>摘要</b>: {summary}\n\n"
                            f"🔗 <b>链接</b>: {link}"
                        )
                        bot.send_msg(bot.admin_chat_id, msg, disable_preview=False)

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

    print(f"[{datetime.now()}] 🚀 多社区抽奖与热帖监控 Bot v4.7 启动完毕...", flush=True)

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
