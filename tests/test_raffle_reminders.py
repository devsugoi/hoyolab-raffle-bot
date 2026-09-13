import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import config
from backfill_raffle_reminders import backfill
from utils.api import HoyolabClient, HoyolabPost, HoyolabAPIError
from utils.db import Database
from utils.raffle_ai import AnalysisUnavailable, RaffleAnalyzer
from utils.raffle_dates import RaffleDates, validate_dates
from utils.reminders import ReminderWorker, build_reminder_embed

UTC = timezone.utc
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def post(id='1', body='Giveaway. Winners announced September 25.'):
    return HoyolabPost(id, 2, 'Giveaway', body, f'https://www.hoyolab.com/article/{id}',
                       'Official', True, NOW - timedelta(days=3), None, None, 1)


def facts(**kwargs):
    data = dict(start_at=None, end_at=None, winner_at=None,
                winner_offset_days=None, confidence='high', reason='Date stated in post')
    return data | kwargs


def validate(data, publication=NOW):
    return validate_dates(data, published_at=publication, zone='Asia/Singapore', default_hour=18)


class DateTests(unittest.TestCase):
    def test_explicit_winner_preferred(self):
        dates = validate(facts(end_at='2026-09-20', winner_at='2026-09-25'))
        self.assertEqual(dates.reminder_at.day, 25)

    def test_end_only(self):
        self.assertEqual(validate(facts(end_at='2026-09-20')).reminder_at.day, 20)

    def test_relative_winner(self):
        dates = validate(facts(end_at='2026-09-20', winner_offset_days=3))
        self.assertEqual(dates.reminder_at.day, 23)

    def test_range(self):
        self.assertEqual(validate(facts(start_at='2026-09-10', end_at='2026-09-20')).end_at.day, 20)

    def test_no_date(self):
        self.assertIsNone(validate(facts()).reminder_at)

    def test_ambiguous(self):
        self.assertIsNone(validate(facts(end_at='2026-09-20', confidence='low')).reminder_at)

    def test_timezone_default_and_evening(self):
        date = validate(facts(end_at='2026-09-20')).end_at
        self.assertEqual(date.hour, 18)
        self.assertEqual(date.utcoffset(), timedelta(hours=8))

    def test_time_without_timezone(self):
        date = validate(facts(end_at='2026-09-20T15:30')).end_at
        self.assertEqual(date.hour, 15)
        self.assertEqual(date.utcoffset(), timedelta(hours=8))

    def test_explicit_timezone(self):
        date = validate(facts(end_at='2026-09-20T15:30-04:00')).end_at
        self.assertEqual(date.utcoffset(), timedelta(hours=-4))

    def test_invalid_responses(self):
        for value in ([], {}, facts(end_at='2026-02-30'), facts(end_at=123),
                      facts(end_at='September 20'), facts(winner_offset_days=True),
                      facts(end_at='2026-09-20', winner_at='2026-09-19'),
                      facts(end_at='2026-09-20', start_at='2026-09-21'),
                      facts(end_at='2025-09-20'), facts(winner_offset_days=3),
                      facts(end_at='2026-09-20', winner_at='2026-09-22', winner_offset_days=3)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_historical_uses_publication_not_today(self):
        date = validate(facts(end_at='2025-03-20'), datetime(2025, 3, 10, tzinfo=UTC)).end_at
        self.assertEqual(date.year, 2025)

    def test_dst_gap_rejected(self):
        with self.assertRaises(ValueError):
            validate_dates(facts(end_at='2026-03-08T02:30'), published_at=None,
                           zone='America/New_York', default_hour=18)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'test.db')
        await self.db.init()
        self.analyzer = AsyncMock()
        self.sender = AsyncMock(return_value=True)
        self.worker = ReminderWorker(self.db, self.analyzer, self.sender)
        self.analyzer.analyze.return_value = RaffleDates(None, NOW, 'Explicit winner date')

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def rows(self):
        return await self.db.reminder_query('SELECT * FROM raffle_reminders ORDER BY post_id')

    async def schedule(self, id='1', date=NOW, historical=False):
        await self.db.enqueue_raffle(post(id), historical=historical)
        self.analyzer.analyze.return_value = RaffleDates(None, date, 'Explicit winner date')
        return await self.worker.analyze_one(NOW)

    async def test_duplicate_processing(self):
        await self.schedule()
        self.assertFalse(await self.db.enqueue_raffle(post()))
        self.assertEqual(len(await self.rows()), 1)

    async def test_persistence_and_offline_due(self):
        await self.schedule(date=NOW + timedelta(days=1))
        self.assertIsNone(await self.worker.send_one(NOW))
        restarted_db = Database(self.db.path)
        await restarted_db.init()
        restarted = ReminderWorker(restarted_db, self.analyzer, self.sender)
        self.assertEqual(await restarted.send_one(NOW + timedelta(days=2)), 'sent')
        self.sender.assert_awaited_once()

    async def test_sent_never_reset(self):
        await self.schedule()
        await self.worker.send_one(NOW)
        await self.db.enqueue_raffle(replace(post(), snippet='Changed result date'))
        self.assertIsNone(await self.worker.send_one(NOW + timedelta(days=10)))
        self.assertEqual((await self.rows())[0]['status'], 'sent')
        self.sender.assert_awaited_once()

    async def test_multiple_due(self):
        for id in ('1', '2', '3'):
            await self.schedule(id)
        for _ in range(3):
            self.assertEqual(await self.worker.send_one(NOW), 'sent')
        self.assertEqual(self.sender.await_count, 3)

    async def test_concurrent_workers(self):
        await self.schedule()
        second = ReminderWorker(Database(self.db.path), self.analyzer, self.sender)
        await asyncio.gather(self.worker.send_one(NOW), second.send_one(NOW))
        self.sender.assert_awaited_once()

    async def test_discord_failure_retry(self):
        await self.schedule()
        self.sender.side_effect = [RuntimeError('failed'), True]
        self.assertEqual(await self.worker.send_one(NOW), 'failed')
        self.assertIsNone(await self.worker.send_one(NOW))
        self.assertEqual(await self.worker.send_one(NOW + timedelta(minutes=6)), 'sent')

    async def test_interrupted_claim_expires(self):
        await self.schedule()
        self.assertIsNotNone(await self.db.claim_reminder('delivery', NOW.timestamp(), 'old'))
        self.assertIsNone(await self.worker.send_one(NOW))
        self.assertEqual(await self.worker.send_one(NOW + timedelta(minutes=6)), 'sent')
        await self.db.finish_reminder('1', 'old', status='pending')
        self.assertEqual((await self.rows())[0]['status'], 'sent')

    async def test_historical_future(self):
        self.assertEqual(await self.schedule(date=NOW + timedelta(days=10), historical=True), 'pending')
        self.assertIsNone(await self.worker.send_one(NOW))

    async def test_historical_recent(self):
        self.assertEqual(await self.schedule(date=NOW - timedelta(days=10), historical=True), 'pending')
        self.assertEqual(await self.worker.send_one(NOW), 'sent')
        embed = build_reminder_embed((await self.rows())[0])
        self.assertIn('older raffle', embed.description)

    async def test_historical_expired(self):
        self.assertEqual(await self.schedule(date=NOW - timedelta(days=180), historical=True), 'expired')
        self.assertIsNone(await self.worker.send_one(NOW))

    async def test_live_old_due_not_expired(self):
        self.assertEqual(await self.schedule(date=NOW - timedelta(days=180)), 'pending')
        self.assertEqual(await self.worker.send_one(NOW), 'sent')

    async def test_no_date_completed(self):
        await self.db.enqueue_raffle(post())
        self.analyzer.analyze.return_value = RaffleDates(None, None, 'No date')
        self.assertEqual(await self.worker.analyze_one(NOW), 'no_date')
        self.assertIsNone(await self.worker.analyze_one(NOW))
        self.assertIsNone(await self.worker.send_one(NOW))

    async def test_ai_unavailable_retries_persistently(self):
        await self.db.enqueue_raffle(post())
        self.analyzer.analyze.side_effect = AnalysisUnavailable()
        self.assertEqual(await self.worker.analyze_one(NOW), 'deferred')
        self.assertIsNone(await self.worker.analyze_one(NOW))
        self.assertEqual((await self.rows())[0]['status'], 'analysis_pending')

    async def test_invalid_ai_bounded_retries(self):
        await self.db.enqueue_raffle(post())
        self.analyzer.analyze.side_effect = ValueError('bad JSON')
        for hours in range(3):
            await self.worker.analyze_one(NOW + timedelta(hours=hours))
        self.assertEqual((await self.rows())[0]['status'], 'invalid')
        self.assertIsNone(await self.worker.analyze_one(NOW + timedelta(days=2)))

    async def test_backfill_twice_skips_existing_and_non_raffles(self):
        await self.db.mark_seen(post('1'), True)
        await self.db.mark_seen(post('2'), False)
        client = AsyncMock()
        client.get_post_full.return_value = post('1')
        first = await backfill(self.db, client, self.worker)
        second = await backfill(self.db, client, self.worker)
        self.assertEqual(first['created'], 1)
        self.assertEqual(second['skipped'], 1)
        client.get_post_full.assert_awaited_once()
        self.analyzer.analyze.assert_awaited_once()

    async def test_backfill_sent_skipped(self):
        await self.db.mark_seen(post(), True)
        await self.schedule()
        await self.worker.send_one(NOW)
        client = AsyncMock()
        await backfill(self.db, client, self.worker)
        client.get_post_full.assert_not_awaited()
        self.assertEqual((await self.rows())[0]['status'], 'sent')

    async def test_backfill_unavailable_post(self):
        await self.db.mark_seen(post(), True)
        client = AsyncMock()
        client.get_post_full.side_effect = HoyolabAPIError('deleted')
        result = await backfill(self.db, client, self.worker)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(await self.rows(), [])
        self.analyzer.analyze.assert_not_awaited()

    async def test_backfill_sequential_ai(self):
        active = peak = 0
        async def analyze(p):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return RaffleDates(None, NOW + timedelta(days=10), 'Date')
        self.analyzer.analyze.side_effect = analyze
        for id in ('1', '2', '3'):
            await self.db.mark_seen(post(id), True)
        client = AsyncMock()
        client.get_post_full.side_effect = lambda p, **kw: post(p.post_id)
        await backfill(self.db, client, self.worker)
        self.assertEqual(peak, 1)
        self.assertEqual(self.analyzer.analyze.await_count, 3)

    async def test_additive_migration(self):
        await self.db.mark_seen(post(), True)
        await self.db.set_setting('alert_channel_id', '123')
        await self.db.init()
        self.assertTrue(await self.db.is_seen('1'))
        self.assertEqual(await self.db.get_setting('alert_channel_id'), '123')


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.analyzer = RaffleAnalyzer()
        self.settings = patch.multiple(config, GEMINI_API_KEYS=('test-key',), GEMINI_FALLBACK_MODELS=(), GEMINI_MIN_INTERVAL_SECONDS=0)
        self.settings.start()
        self.requests = []

    async def asyncTearDown(self):
        await self.analyzer.aclose()
        self.settings.stop()

    async def transport(self, handler):
        await self.analyzer._client.aclose()
        self.analyzer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_language_fixtures_use_single_structured_call(self):
        fixtures = [
            ('Winners announced September 25', facts(winner_at='2026-09-25'), 25),
            ('Event ends September 20', facts(end_at='2026-09-20'), 20),
            ('Event ends September 20. Winners announced 3 days later.', facts(end_at='2026-09-20', winner_offset_days=3), 23),
            ('Event Period: September 10–20', facts(start_at='2026-09-10', end_at='2026-09-20'), 20),
            ('Results will be announced within three days after the event ends September 20.', facts(end_at='2026-09-20', winner_offset_days=3), 23),
            ('Winner list will be posted next Friday', facts(winner_at='2026-09-11'), 11),
        ]
        for body, extracted, day in fixtures:
            with self.subTest(body=body):
                calls = []
                def handler(request):
                    calls.append(json.loads(request.content))
                    return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(extracted)}]}}]})
                await self.transport(handler)
                result = await self.analyzer.analyze(post(body=body))
                self.assertEqual(result.reminder_at.day, day)
                self.assertEqual(len(calls), 1)
                context = json.loads(calls[0]['contents'][0]['parts'][0]['text'])
                self.assertEqual(context['publication'], post().created_at.isoformat())
                self.assertEqual(context['body'], body)
                self.assertIn('responseSchema', calls[0]['generationConfig'])

    async def test_malformed_ai_response(self):
        for payload in ({'candidates': []}, {'candidates': [None]},
                        {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [None]}}]}):
            with self.subTest(payload=payload):
                await self.transport(lambda request: httpx.Response(200, json=payload))
                with self.assertRaises(ValueError):
                    await self.analyzer.analyze(post())

    async def test_single_exhausted_key_is_not_retried_during_cooldown(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429)
        await self.transport(handler)
        for _ in range(2):
            with self.assertRaises(AnalysisUnavailable):
                await self.analyzer.analyze(post())
        self.assertEqual(len(calls), 1)

    async def test_transient_retry(self):
        statuses = iter([503, 503, 200])
        def handler(request):
            return httpx.Response(next(statuses), json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(facts())}]}}]})
        await self.transport(handler)
        self.assertIsNone((await self.analyzer.analyze(post())).reminder_at)

    async def test_strict_hoyolab_missing_post(self):
        client = HoyolabClient('en-us')
        client._get = AsyncMock(return_value={})
        try:
            with self.assertRaises(HoyolabAPIError):
                await client.get_post_full(post(), strict=True)
        finally:
            await client.aclose()


if __name__ == '__main__':
    unittest.main()
