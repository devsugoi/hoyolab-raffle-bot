"""Integration checks for keyword gating and the real watcher notification path."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cogs.hoyolab_watcher import HoyolabWatcher
from utils.api import HoyolabPost
from utils.db import Database
from utils.raffle_dates import RaffleDates
from utils.reminders import ReminderWorker
from backfill_raffle_reminders import backfill


def post(id='new', title='Giveaway', snippet='Comment to enter. Winners announced September 25.'):
    return HoyolabPost(id, 2, title, snippet, f'https://www.hoyolab.com/article/{id}',
                       'Official', True, datetime.now(timezone.utc), None, None, 1)


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watcher = HoyolabWatcher(Mock())
        self.watcher.db = Database(Path(self.tmp.name) / 'db.sqlite')
        await self.watcher.db.init()
        await self.watcher.db.mark_seen(post('seed'), False)
        self.watcher.client.get_post_full = AsyncMock(side_effect=lambda p: p)
        self.watcher._notify = AsyncMock(return_value=True)
        self.watcher.analyzer.analyze = AsyncMock()
        self.watcher.reminders = ReminderWorker(self.watcher.db, self.watcher.analyzer, self.watcher._send_reminder)

    async def asyncTearDown(self):
        await self.watcher.cog_unload()
        self.tmp.cleanup()

    async def test_keyword_first_then_queue_only_matched_post(self):
        self.watcher._fetch_all_posts = AsyncMock(return_value=(
            [post(), post('other', 'Version update', 'Update details')], []))
        await self.watcher.run_scan(notify=True)
        await self.watcher.run_scan(notify=True)
        self.watcher._notify.assert_awaited_once()
        self.watcher.analyzer.analyze.assert_not_awaited()
        rows = await self.watcher.db.reminder_query('SELECT * FROM raffle_reminders')
        self.assertEqual([r['post_id'] for r in rows], ['new'])
        self.assertEqual(rows[0]['status'], 'analysis_pending')

    async def test_failed_original_alert_stays_retryable(self):
        self.watcher._fetch_all_posts = AsyncMock(return_value=([post()], []))
        self.watcher._notify.return_value = False
        await self.watcher.run_scan(notify=True)
        self.assertFalse(await self.watcher.db.is_seen('new'))
        self.assertFalse(await self.watcher.db.reminder_exists('new'))

    async def test_dry_scan_does_not_queue(self):
        self.watcher._fetch_all_posts = AsyncMock(return_value=([post()], []))
        await self.watcher.run_scan(notify=False)
        self.assertFalse(await self.watcher.db.reminder_exists('new'))
        self.watcher._notify.assert_not_awaited()

    async def test_destination_pinned_and_no_webhook_fallback(self):
        now = datetime.now(timezone.utc)
        self.watcher.analyzer.analyze.return_value = RaffleDates(None, now, 'Date')
        await self.watcher.db.enqueue_raffle(post())
        await self.watcher.reminders.analyze_one(now)
        self.watcher.get_alert_channel_id = AsyncMock(return_value=123)
        self.watcher._send_channel = AsyncMock(side_effect=[False, True])
        self.watcher._send_webhook = AsyncMock(return_value=True)
        await self.watcher.reminders.send_one(now)
        self.watcher.get_alert_channel_id.return_value = 456
        await self.watcher.reminders.send_one(now + timedelta(minutes=6))
        self.assertEqual(self.watcher._send_channel.await_args.kwargs['channel_id'], 123)
        self.watcher._send_webhook.assert_not_awaited()

    async def test_database_failure_before_claim_cannot_send(self):
        self.watcher.db.claim_reminder = AsyncMock(side_effect=sqlite3.OperationalError('locked'))
        self.watcher.reminders.sender = AsyncMock()
        with self.assertRaises(sqlite3.OperationalError):
            await self.watcher.reminders.send_one()
        self.watcher.reminders.sender.assert_not_awaited()

    async def test_database_failure_after_send_keeps_claim_until_expiry(self):
        now = datetime.now(timezone.utc)
        self.watcher.analyzer.analyze.return_value = RaffleDates(None, now, 'Date')
        await self.watcher.db.enqueue_raffle(post())
        await self.watcher.reminders.analyze_one(now)
        self.watcher.reminders.sender = AsyncMock(return_value=True)
        with patch.object(self.watcher.db, 'finish_reminder', AsyncMock(side_effect=sqlite3.OperationalError('locked'))):
            with self.assertRaises(sqlite3.OperationalError):
                await self.watcher.reminders.send_one(now)
        self.assertIsNone(await self.watcher.reminders.send_one(now))
        # A send accepted before failed persistence is an explicitly documented
        # at-least-once window, rather than falsely claiming exact-once delivery.
        row = (await self.watcher.db.reminder_query('SELECT * FROM raffle_reminders'))[0]
        self.assertEqual(row['status'], 'pending')
        self.assertGreater(row['lease_until'], now.timestamp())

    async def test_backfill_limit_still_analyzes_queued_work(self):
        for id in ('1', '2'):
            await self.watcher.db.mark_seen(post(id), True)
        self.watcher.client.get_post_full = AsyncMock(side_effect=lambda p, **kw: post(p.post_id))
        self.watcher.analyzer.analyze.return_value = RaffleDates(None, None, 'No date')
        result = await backfill(self.watcher.db, self.watcher.client, self.watcher.reminders, limit=1)
        self.assertEqual(result['created'], 1)
        self.assertEqual(result['no_date'], 1)
        self.watcher.client.get_post_full.assert_awaited_once()
