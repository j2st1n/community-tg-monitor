import json
import os
import tempfile
import threading
import unittest

from app import main


class DetectionRulesTest(unittest.TestCase):
    def make_bot(self, keywords=None, blockwords=None):
        bot = main.BotManager.__new__(main.BotManager)
        bot.lock = threading.RLock()
        bot.keywords = list(keywords or [])
        bot.blockwords = list(blockwords or main.DEFAULT_BLOCKWORDS)
        bot.stats = {}

        def record_stat(key, count=1):
            bot.stats[key] = bot.stats.get(key, 0) + count

        bot.record_stat = record_stat
        return bot

    def evaluate(self, title, source="NodeSeek", category="daily", desc="", **kwargs):
        bot = self.make_bot(**kwargs)
        return bot.evaluate_post(source, category, title, desc)

    def test_real_giveaway_titles_are_detected(self):
        titles = [
            "🔥抽奖🔥+测评 抽一个 Yunyoo US LAX 一个月",
            "【抽奖】zouter 节点下午重置流量，还剩1T，抽个奖",
            "抽一台闲置ovh.ie的VPS-1 2026",
            "【抽】抽一台vmiss US.LA.9929.Basic",
            "送饼",
            "【抽奖】免费送一个香港 Lite 小鸡",
            "免费送一只好鸡 tyyun hk 20M",
            "抽奖第二期 云机一台",
            "杂鱼论坛上线啦 抽 10 份兑换码",
            "抽奖送一个 Gomami 新加坡机器",
            "送码",
            "发红包",
        ]
        for title in titles:
            with self.subTest(title=title):
                matched, kind, _ = self.evaluate(title)
                self.assertTrue(matched)
                self.assertEqual("lottery", kind)

    def test_route_and_delivery_terms_are_not_giveaways(self):
        titles = [
            "送中IP又恢复整场了，美国大豆包又可以用了",
            "送中鸡变送美鸡？",
            "送修的电脑终于回来了",
            "快递已经送达",
        ]
        for title in titles:
            with self.subTest(title=title):
                matched, _, _ = self.evaluate(title)
                self.assertFalse(matched)

    def test_broad_trade_words_respect_word_boundaries(self):
        self.assertFalse(main.matches_blockword("收藏电子书选什么格式好？", "收"))
        self.assertFalse(main.matches_blockword("求助，宽带区别大吗", "求"))
        self.assertFalse(main.matches_blockword("出炉了一份新教程", "出"))
        self.assertTrue(main.matches_blockword("收两个能升级的永久凭证", "收"))
        self.assertTrue(main.matches_blockword("【出】VMISS 年付机器", "出"))
        self.assertTrue(main.matches_blockword("出售一个稳定的账号", "出"))

    def test_custom_keywords_override_generic_question_noise(self):
        matched, kind, reason = self.evaluate(
            "DMIT超量暂停后如何恢复？",
            keywords=["DMIT"],
        )
        self.assertTrue(matched)
        self.assertEqual("custom", kind)
        self.assertIn("DMIT", reason)

        matched, kind, _ = self.evaluate(
            "收藏电子书选什么格式好？",
            keywords=["电子书"],
        )
        self.assertTrue(matched)
        self.assertEqual("custom", kind)

    def test_explicit_trade_block_still_overrides_custom_keyword(self):
        matched, kind, _ = self.evaluate(
            "【出】DMIT LAX 年付机器",
            keywords=["DMIT"],
        )
        self.assertFalse(matched)
        self.assertEqual("trade_blocked", kind)


class SeenIdPersistenceTest(unittest.TestCase):
    def test_seen_ids_keep_insertion_order_when_trimmed(self):
        bot = main.BotManager.__new__(main.BotManager)
        bot.seen_id_order = []
        bot.seen_ids = set()

        old_path = main.SEEN_IDS_FILE
        old_limit = main.MAX_SEEN_IDS
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
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


if __name__ == "__main__":
    unittest.main()
