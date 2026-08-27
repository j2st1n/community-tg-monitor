import threading
import unittest
from unittest.mock import MagicMock, patch

from app.ai_classifier import (
    AIClassificationError,
    OpenAICompatibleClassifier,
    clean_for_ai,
    decide_extraction,
    normalize_chat_endpoint,
    parse_json_object,
    validate_facts,
)
from app import main


class AIClassifierTest(unittest.TestCase):
    def test_clean_for_ai(self):
        raw = "<p>Hello <script>alert(1)</script>world &amp; <b>friends</b></p>"
        cleaned = clean_for_ai(raw)
        self.assertEqual("Hello world & friends", cleaned)

        # Test limit capping
        long_str = "a" * 5000
        self.assertEqual(4000, len(clean_for_ai(long_str)))

    def test_normalize_chat_endpoint(self):
        self.assertEqual(
            "https://api.openai.com/v1/chat/completions",
            normalize_chat_endpoint("https://api.openai.com/v1"),
        )
        self.assertEqual(
            "https://api.openai.com/v1/chat/completions",
            normalize_chat_endpoint("https://api.openai.com/v1/chat/completions"),
        )
        self.assertEqual(
            "http://127.0.0.1:8000/v1/chat/completions",
            normalize_chat_endpoint("http://127.0.0.1:8000/v1"),
        )
        with self.assertRaises(ValueError):
            normalize_chat_endpoint("http://remote.server.com/v1")
        with self.assertRaises(ValueError):
            normalize_chat_endpoint("ftp://invalid.com")

    def test_parse_json_object(self):
        self.assertEqual({"a": 1}, parse_json_object('{"a": 1}'))
        self.assertEqual({"key": "val"}, parse_json_object('```json\n{"key": "val"}\n```'))
        self.assertEqual({"nested": True}, parse_json_object('Prefix text {"nested": true} suffix text'))

        with self.assertRaises(AIClassificationError):
            parse_json_object("not a json")
        with self.assertRaises(AIClassificationError):
            parse_json_object("[1, 2, 3]")

    def test_validate_facts_valid(self):
        raw = {
            "event_state": "active",
            "benefit": {"exists": True, "description": "VPS 1台", "free": True},
            "audience": "forum_readers",
            "participation": {"exists": True, "method": "reply"},
            "allocation": "random",
            "negative_intent": "none",
            "confidence": 0.95,
            "evidence": ["回帖抽奖"],
        }
        validated = validate_facts(raw)
        self.assertEqual("active", validated["event_state"])
        self.assertTrue(validated["benefit"]["free"])
        self.assertEqual(0.95, validated["confidence"])

    def test_validate_facts_invalid_enum(self):
        raw = {
            "event_state": "invalid_state",
            "benefit": {"exists": True, "free": True},
            "audience": "forum_readers",
            "participation": {"exists": True, "method": "reply"},
            "allocation": "random",
            "negative_intent": "none",
            "confidence": 0.9,
        }
        with self.assertRaises(AIClassificationError):
            validate_facts(raw)

    def test_decide_extraction_policy(self):
        # 1. High confidence giveaway -> accept
        valid_facts = {
            "event_state": "active",
            "benefit": {"exists": True, "description": "奖品", "free": True},
            "audience": "forum_readers",
            "participation": {"exists": True, "method": "reply"},
            "allocation": "random",
            "negative_intent": "none",
            "confidence": 0.95,
            "evidence": ["抽奖"],
        }
        policy, reason = decide_extraction(valid_facts, accept_threshold=0.90)
        self.assertEqual("accept", policy)

        # 2. Negative intent (trade) -> reject
        trade_facts = dict(valid_facts, negative_intent="trade")
        policy, reason = decide_extraction(trade_facts)
        self.assertEqual("reject", policy)

        # 3. Expired state -> reject
        expired_facts = dict(valid_facts, event_state="expired")
        policy, reason = decide_extraction(expired_facts)
        self.assertEqual("reject", policy)

        # 4. Specific person only -> reject
        person_facts = dict(valid_facts, audience="specific_person")
        policy, reason = decide_extraction(person_facts)
        self.assertEqual("reject", policy)

        # 5. Boundary case (confidence between review and accept) -> review
        border_facts = dict(valid_facts, confidence=0.75)
        policy, reason = decide_extraction(border_facts, accept_threshold=0.90, review_threshold=0.65)
        self.assertEqual("review", policy)


class AIQueueIntegrationTest(unittest.TestCase):
    def test_process_queue_deliver_giveaway(self):
        bot = main.BotManager.__new__(main.BotManager)
        bot.lock = threading.RLock()
        bot.ai_enabled = True
        bot.ai_endpoint = "https://api.example.com/v1"
        bot.ai_model = "test-model"
        bot.ai_judge_model = ""
        bot.ai_api_key = "sk-test"
        bot.ai_accept_threshold = 0.90
        bot.paused = False
        bot.source_states = {"nodeseek": True}
        bot.nodeseek_ai_pending = {
            "nodeseek:100": {
                "id": "nodeseek:100",
                "category": "daily",
                "title": "送小鸡一台",
                "author": "tester",
                "description": "回帖即抽",
                "link": "https://www.nodeseek.com/post-100-1.html",
                "attempts": 0,
                "next_retry_at": 0,
            }
        }
        bot.nodeseek_ai_history = []
        bot.seen_ids = set()
        bot.seen_id_order = []
        bot.stats = {}
        bot.save_nodeseek_ai_state = MagicMock()
        bot.save_seen_ids = MagicMock()

        def record_stat(key, count=1):
            bot.stats[key] = bot.stats.get(key, 0) + count

        def remember_seen_id(unique_id):
            with bot.lock:
                bot.seen_ids.add(unique_id)
                bot.seen_id_order.append(unique_id)
                return True

        bot.record_stat = record_stat
        bot.remember_seen_id = remember_seen_id

        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = {
            "decision": "giveaway",
            "confidence": 0.96,
            "reason": "有效抽奖",
            "evidence": ["送小鸡"],
            "reviewed": False,
        }
        bot.build_ai_classifier = MagicMock(return_value=mock_classifier)

        with patch("app.main.deliver_public_match") as mock_deliver:
            main.process_nodeseek_ai_queue(bot)
            self.assertEqual(0, len(bot.nodeseek_ai_pending))
            self.assertIn("nodeseek:100", bot.seen_ids)
            self.assertEqual(1, len(bot.nodeseek_ai_history))
            self.assertEqual("giveaway", bot.nodeseek_ai_history[0]["decision"])
            self.assertEqual(1, bot.stats.get("ai_giveaway_hits"))
            mock_deliver.assert_called_once()


if __name__ == "__main__":
    unittest.main()
