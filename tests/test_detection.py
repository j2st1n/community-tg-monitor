import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta

from app import main


class CustomKeywordAndBadgeTest(unittest.TestCase):
    def make_bot(self, keywords=None):
        bot = main.BotManager.__new__(main.BotManager)
        bot.lock = threading.RLock()
        bot.keywords = list(keywords or [])
        bot.stats = {}

        def record_stat(key, count=1):
            bot.stats[key] = bot.stats.get(key, 0) + count

        bot.record_stat = record_stat
        return bot

    def test_custom_keywords_top_priority_matching(self):
        bot = self.make_bot(keywords=["DMIT", "搬瓦工"])
        self.assertEqual("DMIT", bot.match_custom_keyword("【补货】DMIT LAX Pro 9929 机器上新", ""))
        self.assertEqual("DMIT", bot.match_custom_keyword("最新测评", "本帖测试 dmit 美西线路表现"))
        self.assertEqual("搬瓦工", bot.match_custom_keyword("搬瓦工 GIA-E 测评", ""))
        self.assertIsNone(bot.match_custom_keyword("普通帖子无关键词", "普通描述"))

    def test_custom_keywords_empty_by_default(self):
        bot = self.make_bot(keywords=[])
        self.assertIsNone(bot.match_custom_keyword("DMIT 补货", "搬瓦工"))

    def test_sbsb_redpacket_badge_detects_non_keyword_title(self):
        page_html = """
        <ul class="post-list">
          <li class="post-item">
            <div class="post-title-row">
              <a class="post-title" href="/t/2916/">普通主题</a>
            </div>
          </li>
          <li class="post-item post-entry">
            <div class="post-title-row">
              <span class="topic-badge redpacket-badge">红包</span>
              <a class="post-title" href="/t/2917/">吃饼了，饼友们！</a>
            </div>
            <div class="post-meta">
              <span><a href="/u/1206/">mubdao</a></span>
              <span class="post-forum-meta"><a href="/go/general/">综合</a></span>
            </div>
          </li>
        </ul>
        """
        topics = main.parse_sbsb_redpacket_topics(page_html)
        self.assertEqual(1, len(topics))
        self.assertEqual("吃饼了，饼友们！", topics[0]["title"])
        self.assertEqual("https://sb.sb/t/2917/", topics[0]["link"])
        self.assertEqual("mubdao", topics[0]["author"])
        self.assertEqual("综合", topics[0]["category"])
        self.assertEqual("redpacket", topics[0]["badge_kind"])

    def test_sbsb_lottery_badge_is_parsed_independently(self):
        page_html = """
        <ul class="post-list">
          <li class="post-item">
            <span class="topic-badge lottery-badge">抽奖</span>
            <a class="post-title" href="/t/2859/">面包云 breadcloud jp1.1+us1.1 双机</a>
            <a href="/u/42/">tester</a><a href="/go/review/">测评</a>
          </li>
          <li class="post-item">
            <span class="topic-badge redpacket-badge">红包</span>
            <a class="post-title" href="/t/2917/">吃饼了，饼友们！</a>
          </li>
        </ul>
        """
        topics = main.parse_sbsb_badged_topics(page_html, "lottery")
        self.assertEqual(1, len(topics))
        self.assertEqual("https://sb.sb/t/2859/", topics[0]["link"])
        self.assertEqual("lottery", topics[0]["badge_kind"])

    def test_sbsb_points_unclosed_td_regex(self):
        # 验证对 HTML5 省略 </td> 闭合标签的签到时间解析
        html_points = '<time class="minute">2026-08-27 00:02</time><td>每日签到<td class="num plus">+1<td class="num plus">'
        m = re.search(r'<time[^>]*>([^<]+)</time>\s*(?:</td>\s*)?<td>\s*每日签到', html_points)
        self.assertIsNotNone(m)
        self.assertEqual("2026-08-27 00:02", m.group(1).strip())


class SeenIdPersistenceTest(unittest.TestCase):
    def test_seen_ids_keep_insertion_order_when_trimmed(self):
        bot = main.BotManager.__new__(main.BotManager)
        bot.lock = threading.RLock()
        bot.seen_id_order = []
        bot.seen_ids = set()

        old_path = main.SEEN_IDS_FILE
        old_limit = main.MAX_SEEN_IDS
        old_data_dir = main.DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                main.DATA_DIR = tmpdir
                main.SEEN_IDS_FILE = os.path.join(tmpdir, "seen_ids.json")
                main.MAX_SEEN_IDS = 3
                for value in ["a", "b", "c", "d"]:
                    self.assertTrue(bot.remember_seen_id(value))
                self.assertFalse(bot.remember_seen_id("d"))
                bot.save_seen_ids()

                with open(main.SEEN_IDS_FILE, encoding="utf-8") as handle:
                    self.assertEqual(["b", "c", "d"], json.load(handle))
                self.assertEqual({"b", "c", "d"}, bot.seen_ids)
        finally:
            main.SEEN_IDS_FILE = old_path
            main.MAX_SEEN_IDS = old_limit
            main.DATA_DIR = old_data_dir

    def test_sbsb_marker_events_do_not_share_rss_or_badge_deduplication(self):
        bot = main.BotManager.__new__(main.BotManager)
        bot.sbsb_events_initialized = True
        bot.sbsb_event_order = {"lottery": [], "redpacket": []}
        bot.sbsb_events = {"lottery": set(), "redpacket": set()}

        link = "https://sb.sb/t/2917/"
        self.assertTrue(bot.remember_sbsb_event("redpacket", link))
        self.assertFalse(bot.remember_sbsb_event("redpacket", link))
        self.assertTrue(bot.remember_sbsb_event("lottery", link))


if __name__ == "__main__":
    unittest.main()


class SigninParserTest(unittest.TestCase):
    def test_points_time_and_values_extraction(self):
        html_sample = """
        <div class="user-assets">
          <span>可用烧饼</span> <span class="value">88</span>
          <span>成长值</span> <span class="value">120</span>
          <span>等级</span> <span class="value">Lv.3 进阶会员</span>
        </div>
        <table>
          <tr>
            <time class="minute">2026-08-27 00:02</time><td>每日签到<td class="num plus">+1
          </tr>
        </table>
        """
        pts_m = re.search(r'可用烧饼</span>\s*<span[^>]*class="[^"]*value[^"]*"[^>]*>(\d+)</span>', html_sample)
        exp_m = re.search(r'成长值</span>\s*<span[^>]*class="[^"]*value[^"]*"[^>]*>(\d+)</span>', html_sample)
        lv_m = re.search(r'等级</span>\s*<span[^>]*class="[^"]*value[^"]*"[^>]*>([^<]+)</span>', html_sample)
        time_m = re.search(r'<time[^>]*>([^<]+)</time>\s*(?:</td>\s*)?<td>\s*每日签到', html_sample)

        self.assertIsNotNone(pts_m)
        self.assertEqual("88", pts_m.group(1))
        self.assertIsNotNone(exp_m)
        self.assertEqual("120", exp_m.group(1))
        self.assertIsNotNone(lv_m)
        self.assertEqual("Lv.3 进阶会员", lv_m.group(1))
        self.assertIsNotNone(time_m)
        self.assertEqual("2026-08-27 00:02", time_m.group(1).strip())

    def test_already_signed_button_detected(self):
        html_sample = """
        <div class="signin-hero-action">
          <button class="btn-post" type="button" disabled>今日已签到</button>
          <div class="signin-hint">距 7 天连击 +10 还有 5 天</div>
        </div>
        """
        already_signed = bool(
            re.search(r'<button[^>]*class=["\'][^"\']*btn-post[^"\']*["\'][^>]*disabled[^>]*>\s*今日已签到\s*</button>', html_sample, re.IGNORECASE)
            or re.search(r'disabled[^>]*>\s*今日已签到\s*<', html_sample)
        )
        self.assertTrue(already_signed)
