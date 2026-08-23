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
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SEEN_IDS_FILE = os.path.join(DATA_DIR, "seen_ids.json")

RSS_URL = "https://rss.nodeseek.com/"
DEFAULT_POLL_INTERVAL = 30

# 默认关键词库（精准组合，避免单字误伤）
DEFAULT_KEYWORDS = [
    "抽奖", "抽", "福利", "roll", "Roll", "ROLL",
    "送只", "送个", "送台", "送一", "白送", "直接送", "先到先得", "免费送", "送小鸡", "送机器", "送码",
    "口令", "红包", "开奖", "盖楼", "中奖", "白嫖"
]

DEFAULT_BLOCKWORDS = ["收", "求", "买", "询", "出", "出出", "出台", "出个", "出只"]

BOT_COMMANDS = [
    {"command": "status", "description": "📊 监控状态与运行统计"},
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
    return re.sub(r'^[【\[\(（\s]+', '', title)

class BotManager:
    def __init__(self):
        self.bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
        self.admin_chat_id = str(os.environ.get("TG_CHAT_ID", "")).strip()
        self.lock = threading.Lock()
        self.start_time = datetime.now()
        self.paused = False
        self.total_checked = 0
        self.total_hit = 0
        
        self.user_states = {}
        
        self.load_settings()
        self.seen_ids = self.load_seen_ids()
        self.register_telegram_commands()

    def register_telegram_commands(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        payload = {"commands": BOT_COMMANDS}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "NodeSeek-Bot/2.0"}
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
        id_list = list(self.seen_ids)[-500:]
        with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
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
                headers={"Content-Type": "application/json", "User-Agent": "NodeSeek-Bot/2.0"}
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
            headers={"Content-Type": "application/json", "User-Agent": "NodeSeek-Bot/2.0"}
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
            headers={"Content-Type": "application/json", "User-Agent": "NodeSeek-Bot/2.0"}
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
            f"🎯 <b>NodeSeek 监控关键词库</b> (共 {len(kws)} 个)\n\n"
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
            f"🛡️ <b>NodeSeek 噪音过滤屏蔽库</b>\n\n"
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
        help_text = (
            "🤖 <b>NodeSeek 抽奖与热帖监控 Bot 指令中心</b>\n\n"
            "📊 <b>状态与词库</b>\n"
            "├ /status - 监控运行统计与健康报告\n"
            "├ /keywords - 🎯 <b>查看并交互式管理监控关键词</b>\n"
            "└ /blocks - 🚫 <b>查看并交互式管理屏蔽词</b>\n\n"
            "⚙️ <b>快捷控制</b>\n"
            "├ /pause - 暂停推送通知 (免打扰)\n"
            "├ /resume - 恢复推送通知\n"
            "└ /test - 发送格式测试卡片"
        )
        bot.send_msg(chat_id, help_text)

    elif cmd == "/status":
        uptime = datetime.now() - bot.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        status_text = (
            "📊 <b>NodeSeek 监控守护状态报告</b>\n\n"
            f"⏱️ <b>运行时间</b>: {hours}小时 {minutes}分 {seconds}秒\n"
            f"🔔 <b>推送状态</b>: {'⏸️ 已暂停' if bot.paused else '▶️ 运行中'}\n"
            f"📡 <b>轮询周期</b>: 每 {bot.poll_interval} 秒\n"
            f"🎯 <b>监控关键词数</b>: {len(bot.keywords)} 个\n"
            f"🚫 <b>屏蔽词数</b>: {len(bot.blockwords)} 个\n"
            f"📈 <b>已扫描去重库</b>: {len(bot.seen_ids)} 篇帖子\n"
            f"🎁 <b>累计命中推送</b>: {bot.total_hit} 篇\n\n"
            "<i>💡 输入 /keywords 可查看或管理监控词库</i>"
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
        test_msg = (
            "🎁 <b>NodeSeek 发现抽奖/福利新帖！</b> (手动测试)\n\n"
            "📌 <b>标题</b>: [日常] 测试抽奖演示贴\n"
            "👤 <b>作者</b>: NodeSeeker  |  🏷️ <b>板块</b>: #daily\n"
            "📝 <b>摘要</b>: 这是一条手动触发的测试卡片，格式与直达 HTTP 链接已配置完毕。\n\n"
            "🔗 <b>链接</b>: https://www.nodeseek.com/post-889000-1"
        )
        bot.send_msg(chat_id, test_msg, disable_preview=False)
    
    else:
        bot.send_msg(chat_id, f"❓ 未识别的指令：<code>{cmd}</code>\n请输入 /help 查看可用指令列表。")

def telegram_polling_thread(bot):
    offset = 0
    print(f"[{datetime.now()}] 🤖 Telegram 交互指令与按钮监听器已启动...", flush=True)
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot.bot_token}/getUpdates?offset={offset}&timeout=20"
            req = urllib.request.Request(url, headers={"User-Agent": "NodeSeek-Bot/2.0"})
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

def fetch_rss():
    req = urllib.request.Request(
        RSS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return ET.fromstring(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[{datetime.now()}] RSS 拉取异常: {e}", flush=True)
        return None

def rss_monitor_thread(bot):
    print(f"[{datetime.now()}] 📡 NodeSeek RSS 监控引擎已就绪...", flush=True)
    first_run = len(bot.seen_ids) == 0

    while True:
        try:
            root = fetch_rss()
            if root is not None:
                channel = root.find("channel")
                if channel is not None:
                    items = channel.findall("item")
                    for item in reversed(items):
                        guid_elem = item.find("guid")
                        title_elem = item.find("title")
                        link_elem = item.find("link")
                        desc_elem = item.find("description")
                        creator_elem = item.find("{http://purl.org/dc/elements/1.1/}creator")
                        category_elem = item.find("category")

                        post_id = guid_elem.text.strip() if guid_elem is not None else ""
                        title = title_elem.text.strip() if title_elem is not None else ""
                        link = link_elem.text.strip() if link_elem is not None else ""
                        desc = desc_elem.text.strip() if desc_elem is not None else ""
                        author = creator_elem.text.strip() if creator_elem is not None else "未知"
                        cat = category_elem.text.strip() if category_elem is not None else "其他"

                        if not post_id or post_id in bot.seen_ids:
                            continue

                        bot.seen_ids.add(post_id)
                        bot.total_checked += 1

                        if first_run:
                            continue

                        if not bot.paused and bot.is_match(title, desc):
                            bot.total_hit += 1
                            print(f"[{datetime.now()}] 🎁 命中抽奖/福利贴: [{cat}] {title} (作者: {author})", flush=True)
                            msg = (
                                f"🎁 <b>NodeSeek 发现抽奖/福利新帖！</b>\n\n"
                                f"📌 <b>标题</b>: {title}\n"
                                f"👤 <b>作者</b>: {author}  |  🏷️ <b>板块</b>: #{cat}\n"
                                f"📝 <b>摘要</b>: {desc[:120]}{'...' if len(desc) > 120 else ''}\n\n"
                                f"🔗 <b>链接</b>: {link}"
                            )
                            bot.send_msg(bot.admin_chat_id, msg, disable_preview=False)

                    bot.save_seen_ids()
                    if first_run:
                        first_run = False
                        print(f"[{datetime.now()}] ✅ 已初始化历史帖子索引 ({len(bot.seen_ids)} 篇)，开启监听！", flush=True)

        except Exception as e:
            print(f"[{datetime.now()}] 监控循环异常: {e}", flush=True)

        time.sleep(bot.poll_interval)

def main():
    bot = BotManager()
    if not bot.bot_token or not bot.admin_chat_id:
        print("❌ 错误: 必须提供 TG_BOT_TOKEN 与 TG_CHAT_ID 环境变量！", flush=True)
        sys.exit(1)

    print(f"[{datetime.now()}] 🚀 NodeSeek Monitor v2.3 启动完毕...", flush=True)

    t_tg = threading.Thread(target=telegram_polling_thread, args=(bot,), daemon=True)
    t_tg.start()

    t_rss = threading.Thread(target=rss_monitor_thread, args=(bot,), daemon=True)
    t_rss.start()

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
