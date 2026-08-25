#!/usr/bin/env python3
import os
import sys
import time
import json
import re
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

# 默认关键词库（精准组合，避免单字误伤）
DEFAULT_KEYWORDS = [
    "抽奖", "抽", "福利", "roll", "Roll", "ROLL",
    "送只", "送个", "送台", "送一", "白送", "直接送", "先到先得", "免费送", "送小鸡", "送机器", "送码",
    "口令", "红包", "开奖", "盖楼", "中奖", "白嫖"
]

DEFAULT_BLOCKWORDS = ["收", "求", "买", "询", "出", "出出", "出台", "出个", "出只"]

BOT_COMMANDS = [
    {"command": "status", "description": "📊 监控状态与运行统计"},
    {"command": "signin", "description": "🍪 烧饼论坛一键签到与查分"},
    {"command": "keywords", "description": "🎯 查看并管理监控关键词"},
    {"command": "blocks", "description": "🚫 查看并管理屏蔽词"},
    {"command": "pause", "description": "⏸️ 暂停推送"},
    {"command": "resume", "description": "▶️ 恢复推送"},
    {"command": "test", "description": "🧪 发送测试卡片"},
    {"command": "help", "description": "📖 显示帮助菜单"}
]

BUILTIN_LOTTERY = {"抽奖", "抽", "开奖", "中奖", "roll", "Roll", "ROLL", "盖楼"}
BUILTIN_WELFARE = {"福利", "送只", "送个", "送台", "送一", "白送", "直接送", "先到先得", "免费送", "白嫖", "口令", "红包", "送小鸡", "送机器", "送码"}

def clean_title_prefix(title):
    """去除标题开头的括号和特殊标点符号，如 '【出】' -> '出】'"""
    return re.sub(r'^[【\[\(（〖\s]+', '', title)

class BotManager:
    def __init__(self):
        self.bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
        self.admin_chat_id = str(os.environ.get("TG_CHAT_ID", "")).strip()
        
        # 兼容 SBSB_COOKIE 或直接配置 __Host-bbs_session 变量
        sbsb_cookie_val = os.environ.get("SBSB_COOKIE", "").strip()
        if not sbsb_cookie_val and os.environ.get("__Host-bbs_session"):
            sbsb_cookie_val = f"__Host-bbs_session={os.environ.get('__Host-bbs_session').strip()}"
        self.sbsb_cookie = sbsb_cookie_val
        self.sbsb_uid = None

        self.lock = threading.Lock()
        self.start_time = datetime.now()
        self.paused = False
        self.total_checked = 0
        self.total_hit = 0
        self.total_private_notified = 0
        self.last_cookie_warn_time = 0
        
        self.user_states = {}
        
        self.sources = list(DEFAULT_SOURCES)
        
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

    def register_telegram_commands(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        payload = {"commands": BOT_COMMANDS}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/3.4"}
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
                    self.keywords = data.get("keywords", DEFAULT_KEYWORDS)
                    self.blockwords = data.get("blockwords", DEFAULT_BLOCKWORDS)
                    self.poll_interval = data.get("poll_interval", DEFAULT_POLL_INTERVAL)
                    self.paused = data.get("paused", False)
                    return
            except Exception as e:
                print(f"[{datetime.now()}] 读取 settings.json 异常: {e}", flush=True)
        
        self.keywords = list(DEFAULT_KEYWORDS)
        self.blockwords = list(DEFAULT_BLOCKWORDS)
        self.poll_interval = DEFAULT_POLL_INTERVAL
        self.paused = False
        self.save_settings()

    def save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "keywords": self.keywords,
            "blockwords": self.blockwords,
            "poll_interval": self.poll_interval,
            "paused": self.paused
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
                headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/3.4"}
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
            headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/3.4"}
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
            headers={"Content-Type": "application/json", "User-Agent": "Community-Monitor-Bot/3.4"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def is_match(self, title, desc):
        clean_t = clean_title_prefix(title)
        with self.lock:
            # 1. 屏蔽词前缀与内容过滤（自动支持 【出】、【收】 等）
            for bw in self.blockwords:
                if bw:
                    if clean_t.startswith(bw) or title.startswith(bw):
                        return False

            # 2. 标题赠送语法直通（如 '送只小鸡', '送一台' 等以送开头的帖子）
            if clean_t.startswith("送") and not clean_t.startswith("送中"):
                return True

            # 3. 清理正文中的常见网络术语干扰（如 '送中', '没送中', '未送中', '推送' 等）
            clean_desc = desc.replace("送中", "").replace("没送中", "").replace("未送中", "").replace("推送", "").replace("发送", "")

            # 4. 关键词扫描
            for kw in self.keywords:
                if kw and (kw in title or kw in clean_desc):
                    return True
        return False

    def format_keywords_card(self):
        with self.lock:
            kws = list(self.keywords)
        
        lottery = [k for k in kws if k in BUILTIN_LOTTERY]
        welfare = [k for k in kws if k in BUILTIN_WELFARE and k not in lottery]
        custom = [k for k in kws if k not in BUILTIN_LOTTERY and k not in BUILTIN_WELFARE]

        sections = []
        if lottery:
            pills = " · ".join([f"<code>{k}</code>" for k in lottery])
            sections.append(f"🎲 <b>抽奖活动</b> ({len(lottery)})\n{pills}")
        if welfare:
            pills = " · ".join([f"<code>{k}</code>" for k in welfare])
            sections.append(f"🧧 <b>福利赠送</b> ({len(welfare)})\n{pills}")
        if custom:
            pills = " · ".join([f"<code>{k}</code>" for k in custom])
            sections.append(f"🏷️ <b>自定义关注词</b> ({len(custom)})\n{pills}")

        body = "\n\n".join(sections) if sections else "<i>（暂无关键词，请点击下方添加）</i>"
        text = (
            f"🎯 <b>社区监控关键词库</b> (共 {len(kws)} 个)\n\n"
            f"{body}\n\n"
            "<i>💡 命中以上任意词的新帖都会即时推送提醒</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "➕ 添加新词", "callback_data": "menu:add_kw"},
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
    """执行烧饼论坛自动签到并精确解析资产与连续天数"""
    if not cookie:
        return {"success": False, "msg": "未配置 SBSB_COOKIE，请先在 VPS 配置 Cookie"}

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

    # 1. 访问 /signin/ 页面
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

    # 提取 CSRF Token 并尝试提交签到表单
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

    # 提取连续签到天数
    days_match = re.search(r'<span[^>]*class=\"[^\"]*counter-value[^\"]*\"[^>]*>(\d+)</span>\s*<span[^>]*>连续签到', html_signin) or re.search(r'(\d+)\s*连续签到', html_signin)
    days = days_match.group(1) if days_match else "1"

    # 2. 访问 /points/ 页面提取精确资产与等级
    points = "0"
    exp = "0"
    level = "会员"
    try:
        req_points = urllib.request.Request("https://sb.sb/points/", headers=headers)
        resp_points = opener.open(req_points, timeout=15)
        html_points = resp_points.read().decode("utf-8", errors="ignore")
        
        # 提取可用烧饼
        pts_match = re.search(r'可用烧饼</span>\s*<span[^>]*class=\"[^\"]*value[^\"]*\"[^>]*>(\d+)</span>', html_points) or re.search(r'可用烧饼.*?(\d+)', html_points)
        if pts_match:
            points = pts_match.group(1)
            
        # 提取成长值
        exp_match = re.search(r'成长值</span>\s*<span[^>]*class=\"[^\"]*value[^\"]*\"[^>]*>(\d+)</span>', html_points) or re.search(r'成长值.*?(\d+)', html_points)
        if exp_match:
            exp = exp_match.group(1)

        # 提取等级
        lv_match = re.search(r'等级</span>\s*<span[^>]*class=\"[^\"]*value[^\"]*\"[^>]*>([^<]+)</span>', html_points) or re.search(r'等级.*?(Lv\.\d+[^<\n]+)', html_points)
        if lv_match:
            level = lv_match.group(1).strip()
    except Exception as pe:
        print(f"[{datetime.now()}] 访问 points 页面异常: {pe}", flush=True)

    # 提取反馈消息
    flash_match = re.search(r'window\.__pageFlash=["\']([^"\']*)["\']', html_signin) or re.search(r'class=["\'][^"\']*toast[^"\']*["\']>([^<]+)<', html_signin)
    flash_msg = flash_match.group(1).strip() if flash_match and flash_match.group(1).strip() else "签到成功"

    already = "已签到" in flash_msg or "明日再来" in html_signin or "今日已签" in html_signin

    return {
        "success": True,
        "already": already,
        "msg": flash_msg,
        "consecutive_days": f"{days} 天",
        "total_points": f"{points} 饼",
        "exp": exp,
        "level": level
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

    if data == "menu:add_kw":
        bot.user_states[chat_id] = "waiting_for_add"
        bot.answer_callback_query(query_id, "请直接输入新关键词")
        bot.send_msg(chat_id, "➕ <b>请输入你想添加的监控关键词：</b>\n<i>（支持一次输入多个词，用空格分隔，例如：<code>搬瓦工 9929 传家宝</code>）</i>")

    elif data == "menu:del_kw":
        bot.answer_callback_query(query_id, "请点击按钮删除")
        with bot.lock:
            kws = list(bot.keywords)
        if not kws:
            bot.edit_msg_text(chat_id, msg_id, "⚠️ <b>当前没有可删除的关键词。</b>")
        else:
            text = f"🗑️ <b>请点击下方按钮删除对应的监控词（共 {len(kws)} 个）：</b>"
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
                text = f"🗑️ <b>请点击下方按钮删除对应的监控词（剩余 {len(bot.keywords)} 个）：</b>"
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
            new_kws = text.split()
            added = []
            with bot.lock:
                for kw in new_kws:
                    if kw not in bot.keywords:
                        bot.keywords.append(kw)
                        added.append(kw)
                bot.save_settings()
            if added:
                msg_text, markup = bot.format_keywords_card()
                bot.send_msg(chat_id, f"✅ <b>已成功添加 {len(added)} 个监控词：</b>\n<code>{', '.join(added)}</code>\n\n{msg_text}", reply_markup=markup)
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

    print(f"[{datetime.now()}] 📨 处理指令: '{text}'", flush=True)

    if cmd in ["/start", "/help"]:
        sources_desc = "、".join([f"{s['icon']} {s['name']}" for s in bot.sources])
        sbsb_private_desc = "🟢 已启用" if bot.sbsb_cookie else "⚪ 未配置 (可选)"
        help_text = (
            f"🤖 <b>多社区抽奖与热帖监控 Bot 指令中心</b>\n\n"
            f"📡 <b>当前公开源</b>: {sources_desc}\n"
            f"📬 <b>烧饼私信/签到引擎</b>: {sbsb_private_desc}\n\n"
            "📊 <b>状态与签到</b>\n"
            "├ /status - 监控运行统计与健康报告\n"
            "├ /signin - 🍪 <b>烧饼论坛一键签到与查分</b>\n"
            "├ /keywords - 🎯 <b>查看并交互式管理监控关键词</b>\n"
            "└ /blocks - 🚫 <b>查看并交互式管理屏蔽词</b>\n\n"
            "⚙️ <b>快捷控制</b>\n"
            "├ /pause - 暂停推送通知 (免打扰)\n"
            "├ /resume - 恢复推送通知\n"
            "└ /test - 发送格式测试卡片"
        )
        bot.send_msg(chat_id, help_text)

    elif cmd in ["/signin", "/checkin"]:
        bot.send_msg(chat_id, "⏳ <b>正在连接烧饼论坛执行签到与资产同步...</b>")
        res = do_sbsb_signin(bot.sbsb_cookie)
        if res.get("success"):
            status_badge = "✅ 签到成功！" if not res.get("already") else "✨ 今日已完成签到"
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            report_msg = (
                f"🍪 <b>烧饼论坛 (sb.sb) 签到与资产报告</b>\n\n"
                f"🎉 <b>签到状态</b>: {status_badge}\n"
                f"📅 <b>连续签到</b>: <b>{res.get('consecutive_days')}</b>\n"
                f"💰 <b>可用烧饼</b>: <b>{res.get('total_points')}</b>\n"
                f"⭐ <b>成长等级</b>: <b>{res.get('level')}</b> (成长值: {res.get('exp')})\n"
                f"🕒 <b>同步时间</b>: {now_str}\n\n"
                "<i>💡 系统将在每日 08:05 (UTC+8) 自动执行定时签到</i>"
            )
            bot.send_msg(chat_id, report_msg)
        else:
            bot.send_msg(chat_id, f"⚠️ <b>烧饼论坛签到失败</b>\n原因: <code>{res.get('msg')}</code>")

    elif cmd == "/status":
        uptime = datetime.now() - bot.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        sources_list = "\n".join([f"  • {s['icon']} <b>{s['name']}</b> ({s['url']})" for s in bot.sources])
        sbsb_private_status = "🟢 实时运行中（含每日 08:05 自动签到与通知）" if bot.sbsb_cookie else "⚪ 未配置 SBSB_COOKIE"
        status_text = (
            "📊 <b>社区监控守护状态报告</b>\n\n"
            f"⏱️ <b>运行时间</b>: {hours}小时 {minutes}分 {seconds}秒\n"
            f"🔔 <b>推送状态</b>: {'⏸️ 已暂停' if bot.paused else '▶️ 运行中'}\n"
            f"📡 <b>轮询周期</b>: 每 {bot.poll_interval} 秒\n"
            f"🌐 <b>已启用公开监控源 ({len(bot.sources)})</b>:\n{sources_list}\n"
            f"📬 <b>烧饼私信/签到引擎</b>: {sbsb_private_status}\n"
            f"🎯 <b>监控关键词数</b>: {len(bot.keywords)} 个\n"
            f"🚫 <b>屏蔽词数</b>: {len(bot.blockwords)} 个\n"
            f"📈 <b>已扫描去重库</b>: {len(bot.seen_ids)} 篇公开帖 / {len(bot.seen_msgs)} 条私信与通知\n"
            f"🎁 <b>累计公开帖命中</b>: {bot.total_hit} 篇\n"
            f"💌 <b>累计互动通知</b>: {bot.total_private_notified} 次\n\n"
            "<i>💡 输入 /signin 可一键手动签到，输入 /keywords 可管理监控词库</i>"
        )
        bot.send_msg(chat_id, status_text)

    elif cmd in ["/keywords", "/list", "/add", "/del"]:
        if cmd == "/add" and arg:
            new_kws = arg.split()
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
        bot.send_msg(chat_id, "⏸️ <b>监控推送已暂停！</b>\n（后台继续记录去重索引，免打扰，发送 /resume 可随时恢复）")

    elif cmd == "/resume":
        with bot.lock:
            bot.paused = False
            bot.save_settings()
        bot.send_msg(chat_id, "▶️ <b>监控推送已恢复正常运行！</b>")

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
            "📅 <b>连续签到</b>: <b>1 天</b>\n"
            "💰 <b>可用烧饼</b>: <b>25 饼</b>\n"
            "⭐ <b>成长等级</b>: <b>Lv.2 新手上路</b> (成长值: 41)\n"
            "🕒 <b>同步时间</b>: 2026-08-25 08:05:00\n\n"
            "<i>💡 系统将在每日 08:05 (UTC+8) 自动执行定时签到</i>"
        )
        bot.send_msg(chat_id, test_msg_ns, disable_preview=False)
        bot.send_msg(chat_id, test_msg_sb, disable_preview=False)
        bot.send_msg(chat_id, test_msg_pm, disable_preview=False)
        bot.send_msg(chat_id, test_msg_signin, disable_preview=False)
    
    else:
        bot.send_msg(chat_id, f"❓ 未识别的指令：<code>{cmd}</code>\n请输入 /help 查看可用指令列表。")

def telegram_polling_thread(bot):
    offset = 0
    print(f"[{datetime.now()}] 🤖 Telegram 交互指令与按钮监听器已启动...", flush=True)
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot.bot_token}/getUpdates?offset={offset}&timeout=20"
            req = urllib.request.Request(url, headers={"User-Agent": "Community-Monitor-Bot/3.4"})
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
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (CommunityFeed/3.4)"}
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
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        report_msg = (
                            f"🍪 <b>烧饼论坛 (sb.sb) 每日自动签到报告</b>\n\n"
                            f"🎉 <b>签到状态</b>: {status_badge}\n"
                            f"📅 <b>连续签到</b>: <b>{res.get('consecutive_days')}</b>\n"
                            f"💰 <b>可用烧饼</b>: <b>{res.get('total_points')}</b>\n"
                            f"⭐ <b>成长等级</b>: <b>{res.get('level')}</b> (成长值: {res.get('exp')})\n"
                            f"🕒 <b>完成时间</b>: {now_str}\n\n"
                            "<i>💡 每日 08:05 (UTC+8) 定时自动执行</i>"
                        )
                        bot.send_msg(bot.admin_chat_id, report_msg)
                        print(f"[{datetime.now()}] ✅ 烧饼论坛每日自动签到执行完毕并推送通知！", flush=True)
                    else:
                        print(f"[{datetime.now()}] ⚠️ 自动签到未能成功: {res.get('msg')}", flush=True)

        except Exception as e:
            print(f"[{datetime.now()}] 签到调度器异常: {e}", flush=True)

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
        return "1218"

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
            
            # 倒序遍历（从旧到新推送）
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

                root = fetch_rss(source_url)
                if root is None:
                    continue

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

                    if first_run:
                        continue

                    if not bot.paused and bot.is_match(title, desc):
                        bot.total_hit += 1
                        print(f"[{datetime.now()}] 🎁 命中抽奖/福利贴: [{source_name}] [{cat}] {title} (作者: {author})", flush=True)
                        
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

    print(f"[{datetime.now()}] 🚀 多社区抽奖与热帖监控 Bot v3.4 启动完毕...", flush=True)

    t_tg = threading.Thread(target=telegram_polling_thread, args=(bot,), daemon=True)
    t_tg.start()

    t_rss = threading.Thread(target=rss_monitor_thread, args=(bot,), daemon=True)
    t_rss.start()

    t_private = threading.Thread(target=sbsb_private_messages_thread, args=(bot,), daemon=True)
    t_private.start()

    t_checkin = threading.Thread(target=sbsb_checkin_scheduler_thread, args=(bot,), daemon=True)
    t_checkin.start()

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
