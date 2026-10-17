"""Offline routing, evidence and failure-boundary tests for the model cascade."""
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from app import main
from app.ai_classifier import (AIClassificationError, NodeSeekCascadeClassifier,
                               OpenAICompatibleClassifier, validate_grounded_decision)
from app.participation import ContentPlanner, ParticipationError, load_typesafe_key


POST = {'title': '免费抽一份礼物', 'description': '回复本帖免费抽取一人，今天有效。',
        'author': 'private-account-not-for-model', 'link': 'https://example.test/private'}
SCORES = dict(injection=.01, prose=.99, active=.99, free=.99, readers=.99,
              mechanism=.99, excluded=.01, redpacket=.01, random=.99)


class CascadeTest(unittest.TestCase):
    def make(self, **scores):
        model = OpenAICompatibleClassifier('https://synthetic.example/v1', 'synthetic-key',
                                           'gemini-3.8-flash-high', 'legacy-judge')
        model._chat = Mock(return_value={'decision': 'lottery', 'evidence': [
            {'field': 'description', 'quote': '回复本帖免费抽取一人'}]})
        noul = Mock(return_value={**SCORES, **scores})
        return NodeSeekCascadeClassifier(noul, model), noul, model

    def test_clear_allow_uses_jev_only_and_whitelists_inputs(self):
        worker, jev, model = self.make()
        result = worker.classify(POST)
        self.assertEqual((result['decision'], result['sub_type'], result['reviewed']), ('giveaway', 'lottery', False))
        jev.assert_called_once(); model._chat.assert_not_called()
        state = jev.call_args.args[0]
        self.assertEqual(set(state), {'title', 'description'})
        self.assertNotIn('private-account', json.dumps(state))

    def test_clear_denials_and_unsafe_input_do_not_escalate(self):
        for scores in ({'active':.10}, {'free':.05}, {'excluded':.90}, {'injection':.31}, {'prose':.74}):
            worker, jev, model = self.make(**scores)
            self.assertEqual(worker.classify(POST)['decision'], 'not_giveaway')
            model._chat.assert_not_called(); jev.assert_called_once()

    def test_pending_gets_one_independent_gemini_final(self):
        worker, jev, model = self.make(active=.5)
        result = worker.classify(POST)
        self.assertEqual(result['decision'], 'giveaway'); self.assertTrue(result['reviewed'])
        jev.assert_called_once(); model._chat.assert_called_once()
        self.assertEqual(model._chat.call_args.args[1], 'gemini-3.8-flash-high')
        self.assertFalse(model._chat.call_args.kwargs['allow_format_retry'])
        sent = model._chat.call_args.args[0][1]['content']
        self.assertNotIn('first_scores', sent)
        self.assertNotIn('private-account', sent)
        self.assertEqual(result['confidence_source'], 'not_reported_by_final_model')

    def test_unclear_denial_and_final_fault_are_distinct_terminal_results(self):
        for verdict in ('unclear', 'not_giveaway'):
            worker, jev, model = self.make(active=.5)
            model._chat.return_value['decision'] = verdict
            result = worker.classify(POST)
            self.assertEqual(result['decision'], 'not_giveaway')
            self.assertNotIn('error', result)
            self.assertEqual(result['reason'], 'final_review_unclear' if verdict == 'unclear' else 'final_review_denied')
            self.assertEqual((jev.call_count, model._chat.call_count), (1, 1))
        worker, jev, model = self.make(active=.5)
        model._chat.side_effect = TimeoutError('private endpoint must not escape')
        result = worker.classify(POST)
        self.assertEqual(result['error'], 'final_review_unavailable')
        self.assertNotIn('private endpoint', json.dumps(result))

    def test_invalid_primary_response_cannot_enable_gemini_fallback(self):
        for malformed in ({}, {**SCORES, 'active':True}, {**SCORES, 'active':float('nan')}):
            worker, jev, model = self.make(); jev.return_value = malformed
            result = worker.classify(POST)
            self.assertEqual(result['error'], 'first_stage_unavailable'); model._chat.assert_not_called()
        worker, jev, model = self.make(); jev.side_effect = ParticipationError('guard_unavailable')
        self.assertEqual(worker.classify(POST)['error'], 'first_stage_unavailable')
        model._chat.assert_not_called()

    def test_gemini_hallucinated_evidence_never_becomes_push(self):
        worker, jev, model = self.make(active=.5)
        model._chat.return_value['evidence'][0]['quote'] = '原文没有这句话'
        result = worker.classify(POST)
        self.assertEqual(result['decision'], 'not_giveaway')
        self.assertEqual(result['error'], 'final_review_unavailable')

    def test_primary_facts_are_batched_and_complete(self):
        worker, jev, _ = self.make()
        worker.classify(POST)
        self.assertEqual(set(jev.call_args.args[1]), set(SCORES))

    def test_high_primary_subtypes(self):
        for scores, kind in (({'redpacket':.99}, 'redpacket'), ({'random':.01}, 'welfare')):
            worker, _, model = self.make(**scores)
            self.assertEqual(worker.classify(POST)['sub_type'], kind)
            model._chat.assert_not_called()

    def test_uncertain_activity_type_uses_final_model(self):
        worker, jev, model = self.make(random=.5)
        result = worker.classify(POST)
        self.assertTrue(result['reviewed'])
        self.assertEqual(result['sub_type'], 'lottery')
        jev.assert_called_once(); model._chat.assert_called_once()


class GroundingAndTransportTest(unittest.TestCase):
    def test_strict_grounding(self):
        sources = {'body':'回帖即领'}
        valid = {'decision':'allow', 'evidence':[{'field':'body','quote':'回帖'}]}
        self.assertEqual(validate_grounded_decision(valid, sources, {'allow':'x'}), valid)
        for raw in ([], {'decision':[],'evidence':[]}, {**valid,'extra':True},
                    {**valid,'evidence':[]}, {**valid,'evidence':[{'field':[],'quote':'回帖'}]},
                    {**valid,'evidence':[{'field':'body','quote':' '}]},
                    {**valid,'evidence':[{'field':'body','quote':'虚构'}]}):
            with self.assertRaises(AIClassificationError): validate_grounded_decision(raw, sources, {'allow':'x'})

    def test_final_call_does_not_retry_http_400(self):
        model = OpenAICompatibleClassifier('https://synthetic.example/v1', 'synthetic-key', 'gemini-3.8-flash-high')
        model._post = Mock(side_effect=urllib.error.HTTPError('https://synthetic.example',400,'bad',{},None))
        with self.assertRaises(AIClassificationError):
            model.final_decision({'body':'合成原文'}, '合成任务', {'body':'合成原文'}, {'allow':'允许','unclear':'不明'})
        model._post.assert_called_once()

    def test_jev_key_priority_and_file_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'synthetic.key')
            with open(path, 'w') as out: out.write('synthetic-file-key\n')
            with patch.dict(os.environ, {'TYPESAFE_API_KEY':'synthetic-env-key', 'TYPESAFE_API_KEY_FILE':path}):
                self.assertEqual(load_typesafe_key(), 'synthetic-env-key')
            with patch.dict(os.environ, {'TYPESAFE_API_KEY':'', 'TYPESAFE_API_KEY_FILE':path}):
                self.assertEqual(load_typesafe_key(), 'synthetic-file-key')

    def test_builder_routes_nodeseek_to_jev_without_sbsb_cookie(self):
        bot = main.BotManager.__new__(main.BotManager)
        bot.build_ai_classifier = Mock(return_value=Mock(model='gemini-3.8-flash-high'))
        with patch('app.main.load_typesafe_key', return_value='synthetic-key'):
            worker = bot.build_nodeseek_classifier()
        self.assertIsInstance(worker, NodeSeekCascadeClassifier)
        self.assertEqual(worker.noul.__self__.key, 'synthetic-key')

    def test_actual_sbsb_pipeline_final_does_not_repeat_eligibility_or_generate_lottery_reply(self):
        model = OpenAICompatibleClassifier('https://synthetic.example/v1','synthetic-key','gemini-3.8-flash-high')
        model._chat = Mock(return_value={'decision':'ordinary_entry','evidence':[{'field':'topic_body','quote':'回帖即领'}]})
        planner = ContentPlanner('synthetic-jev-key', lambda:model)
        planner.noul = Mock(return_value={'injection':.01,'relevant':.99,'additional':.5})
        self.assertEqual(planner.prepare('合成抽奖','回帖即领','lottery'), '')
        planner.noul.assert_called_once(); model._chat.assert_called_once()
        self.assertEqual(planner.last_diagnostics['eligibility'], {'rounds':2,'first':'pending','final':'allow'})
