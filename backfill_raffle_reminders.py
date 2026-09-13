"""Manual, idempotent recovery of previously notified raffle posts."""
import argparse
import asyncio
from collections import Counter
import logging

import config
from utils.api import HoyolabClient, HoyolabPost
from utils.db import Database
from utils.raffle_ai import RaffleAnalyzer
from utils.reminders import ReminderWorker

log = logging.getLogger(__name__)


async def backfill(db, client, worker, *, limit=None):
    counts = Counter()
    log.info('Starting raffle reminder backfill')
    # Keyset pagination bounds memory and avoids rescanning on application startup.
    after = ''
    while True:
        rows = await db.reminder_query(
            'SELECT * FROM seen_posts WHERE notified=1 AND post_id>? ORDER BY post_id LIMIT 100', (after,))
        if not rows:
            break
        for row in rows:
            after = row['post_id']
            counts['found'] += 1
            if await db.reminder_exists(row['post_id']):
                counts['skipped'] += 1
                continue
            if limit is not None and counts['attempted'] >= limit:
                log.info('Backfill limit reached: %s', dict(counts))
                break
            counts['attempted'] += 1
            post = HoyolabPost(post_id=row['post_id'], gids=row['gids'] or 0,
                title=row['title'] or 'HoYoLAB raffle', snippet='', url=row['url'] or f"{config.HOYOLAB_SITE}/article/{row['post_id']}",
                author='HoYoLAB', official=True, created_at=None, cover_url=None,
                end_at=None, news_type=0)
            try:
                detailed = await client.get_post_full(post, strict=True)
                if not detailed.snippet:
                    raise ValueError('Original content unavailable')
                if await db.enqueue_raffle(detailed, historical=True):
                    counts['created'] += 1
                    log.info('Historical raffle queued: %s', post.post_id)
                else:
                    counts['skipped'] += 1
            except Exception as exc:
                counts['failed'] += 1
                log.warning('Historical post recovery failed: %s (%s)', post.post_id, type(exc).__name__)
            await asyncio.sleep(0.2)
        if limit is not None and counts['attempted'] >= limit:
            break
    # Shared queue; may also finish pending live raffles. No Discord sends in CLI.
    while True:
        result = await worker.analyze_one()
        if result is None:
            break
        counts[result] += 1
        if result == 'deferred':
            break
    log.info('Backfill complete: %s; due reminders will be sent by the running bot', dict(counts))
    return counts


async def run(limit):
    db = Database(config.SQLITE_PATH)
    await db.init()
    client = HoyolabClient(config.HOYOLAB_LANG)
    analyzer = RaffleAnalyzer()
    try:
        return await backfill(db, client, ReminderWorker(db, analyzer), limit=limit)
    finally:
        await client.aclose()
        await analyzer.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, help='Maximum historical posts to fetch in this run')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    counts = asyncio.run(run(args.limit))
    if counts['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
