"""Offline checks for the companion bot's key and model fallback conventions."""
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import config
from utils.api import HoyolabPost
from utils.raffle_ai import AnalysisUnavailable, RaffleAnalyzer


def success():
    facts = dict(start_at=None, end_at=None, winner_at=None,
                 winner_offset_days=None, confidence='high', reason='No date')
    return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP',
        'content': {'parts': [{'text': json.dumps(facts)}]}}]})


class ConfigurationTests(unittest.TestCase):
    def test_numbered_values_skip_gaps_blanks_and_duplicates(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': ' first ', 'GEMINI_API_KEY_2': '',
                        'GEMINI_API_KEY_4': 'second', 'GEMINI_API_KEY_5': 'first',
                        'GEMINI_API_KEY_20': 'last', 'GEMINI_API_KEY_21': 'ignored'}, clear=True):
            self.assertEqual(config._numbered_env('GEMINI_API_KEY'), ('first', 'second', 'last'))

    def test_backup_without_primary(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY_3': 'backup'}, clear=True):
            self.assertEqual(config._numbered_env('GEMINI_API_KEY'), ('backup',))

    def test_numbered_models_same_convention(self):
        with patch.dict(os.environ, {'GEMINI_FALLBACK_MODEL': 'lite',
                        'GEMINI_FALLBACK_MODEL_3': 'other',
                        'GEMINI_FALLBACK_MODEL_4': 'lite'}, clear=True):
            self.assertEqual(config._numbered_env('GEMINI_FALLBACK_MODEL'), ('lite', 'other'))


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = patch.multiple(config, GEMINI_API_KEYS=('secret-primary', 'secret-backup'),
                                      GEMINI_MODEL='primary', GEMINI_FALLBACK_MODELS=('fallback',),
                                      GEMINI_MIN_INTERVAL_SECONDS=0)
        self.settings.start()
        self.sleep = patch('utils.raffle_ai.asyncio.sleep', new_callable=AsyncMock)
        self.sleeper = self.sleep.start()
        self.analyzer = RaffleAnalyzer()
        self.post = HoyolabPost('1', 2, 'Raffle', 'Giveaway', 'https://www.hoyolab.com/article/1',
                                'Official', True, None, None, None, 1)
        self.calls = []
        self.payloads = []

    async def asyncTearDown(self):
        await self.analyzer.aclose()
        self.sleep.stop()
        self.settings.stop()

    async def responses(self, handler):
        await self.analyzer._client.aclose()
        def capture(request):
            pair = (request.headers['x-goog-api-key'], request.url.path.split('/')[-1].split(':')[0])
            self.calls.append(pair)
            self.payloads.append(json.loads(request.content))
            return handler(pair)
        self.analyzer._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))

    async def test_primary_success_uses_one_request(self):
        await self.responses(lambda pair: success())
        await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [('secret-primary', 'primary')])

    async def test_quota_rotates_key_and_skips_it_on_next_raffle(self):
        await self.responses(lambda pair: httpx.Response(429) if pair[0] == 'secret-primary' else success())
        with self.assertLogs('utils.raffle_ai', level='INFO') as logs:
            await self.analyzer.analyze(self.post)
            await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [('secret-primary', 'primary'), ('secret-backup', 'primary'),
                                     ('secret-backup', 'primary')])
        self.assertNotIn('secret-', '\n'.join(logs.output))
        self.assertGreater(self.analyzer._key_unavailable_until['secret-primary'], time.monotonic() + 3500)

    async def test_all_keys_exhausted_defers_without_more_requests(self):
        await self.responses(lambda pair: httpx.Response(429))
        for _ in range(2):
            with self.assertRaises(AnalysisUnavailable) as error:
                await self.analyzer.analyze(self.post)
            self.assertGreater(error.exception.delay, 3500)
        self.assertEqual(len(self.calls), 2)

    async def test_expired_key_cooldown_restores_primary_preference(self):
        self.analyzer._key_unavailable_until['secret-primary'] = time.monotonic() - 1
        await self.responses(lambda pair: success())
        await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls[0], ('secret-primary', 'primary'))

    async def test_missing_model_falls_back_and_is_remembered(self):
        await self.responses(lambda pair: httpx.Response(404) if pair[1] == 'primary' else success())
        await self.analyzer.analyze(self.post)
        await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [('secret-primary', 'primary'), ('secret-primary', 'fallback'),
                                     ('secret-primary', 'fallback')])
        self.assertEqual(self.payloads[0], self.payloads[1])

    async def test_busy_model_retried_twice_then_fallback(self):
        await self.responses(lambda pair: httpx.Response(503) if pair[1] == 'primary' else success())
        await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [('secret-primary', 'primary')] * 3 + [('secret-primary', 'fallback')])

    async def test_every_model_unavailable_defers_without_rotating_good_key(self):
        await self.responses(lambda pair: httpx.Response(404))
        for _ in range(2):
            with self.assertRaises(AnalysisUnavailable) as error:
                await self.analyzer.analyze(self.post)
            self.assertGreater(error.exception.delay, 290)
        self.assertEqual(self.calls, [('secret-primary', 'primary'), ('secret-primary', 'fallback')])

    async def test_model_and_key_fallback_combination(self):
        def handler(pair):
            if pair[1] == 'primary':
                return httpx.Response(404)
            if pair[0] == 'secret-primary':
                return httpx.Response(429)
            return success()
        await self.responses(handler)
        await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [('secret-primary', 'primary'), ('secret-primary', 'fallback'),
                                     ('secret-backup', 'fallback')])

    async def test_multiple_fallback_models_order_and_deduplication(self):
        with patch.object(config, 'GEMINI_FALLBACK_MODELS', ('primary', 'fallback', 'fallback', 'last')):
            await self.responses(lambda pair: success() if pair[1] == 'last' else httpx.Response(404))
            await self.analyzer.analyze(self.post)
        self.assertEqual([model for key, model in self.calls], ['primary', 'fallback', 'last'])

    async def test_bad_credentials_rotate_key(self):
        await self.responses(lambda pair: httpx.Response(403) if pair[0] == 'secret-primary' else success())
        await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [('secret-primary', 'primary'), ('secret-backup', 'primary')])

    async def test_bad_request_does_not_fan_out(self):
        await self.responses(lambda pair: httpx.Response(400))
        for _ in range(2):
            with self.assertRaises(AnalysisUnavailable):
                await self.analyzer.analyze(self.post)
        self.assertEqual(len(self.calls), 1)

    async def test_no_keys_makes_no_request(self):
        await self.responses(lambda pair: success())
        with patch.object(config, 'GEMINI_API_KEYS', ()), self.assertRaises(AnalysisUnavailable):
            await self.analyzer.analyze(self.post)
        self.assertEqual(self.calls, [])

    async def test_pacing_applies_to_key_and_model_fallback(self):
        await self.responses(lambda pair: httpx.Response(429) if pair[0] == 'secret-primary' else success())
        with patch.object(config, 'GEMINI_MIN_INTERVAL_SECONDS', 15):
            await self.analyzer.analyze(self.post)
        self.assertEqual(self.sleeper.await_count, 2)
        self.assertGreater(self.sleeper.await_args.args[0], 14)

    async def test_invalid_json_is_not_retried_across_keys_or_models(self):
        await self.responses(lambda pair: httpx.Response(200, json={'candidates': []}))
        with self.assertRaises(ValueError):
            await self.analyzer.analyze(self.post)
        self.assertEqual(len(self.calls), 1)
