"""Persistent extraction and delivery workers used by live processing and backfill."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from uuid import uuid4
import discord
import config
from utils.api import HoyolabPost
from utils.raffle_ai import AnalysisUnavailable

log = logging.getLogger(__name__)


def recover_post(row):
    data = json.loads(row['post_json'])
    for key in ('created_at', 'end_at'):
        data[key] = datetime.fromisoformat(data[key]) if data[key] else None
    return HoyolabPost(**data)


def build_reminder_embed(row):
    game = config.GAMES.get(row['gids'])
    winner = row['winner_at'] is not None
    description = ('The winners should now be available.' if winner else
                   'This reminder is based on the raffle ending; a winner announcement date was not available.')
    if row['historical']:
        description += '\nThis reminder was recovered from an older raffle.'
    embed = discord.Embed(title='🎉 Raffle Results Reminder', url=row['url'],
                          description=description, color=game.color if game else 0x59A6D4)
    embed.add_field(name='Raffle', value=row['title'][:1024], inline=False)
    for key, label in (('winner_at', '🏆 Winners expected'), ('end_at', '📅 Raffle ended')):
        if row[key] is not None:
            embed.add_field(name=label, value=f"<t:{int(row[key])}:F>", inline=False)
    embed.add_field(name='Check whether you won!', value=f"[Open HoYoLAB post]({row['url']})", inline=False)
    if game:
        embed.set_thumbnail(url=game.thumbnail)
    embed.set_footer(text=f"Raffle result reminder · {row['post_id']}")
    return embed


class ReminderWorker:
    def __init__(self, db, analyzer, sender=None):
        self.db, self.analyzer, self.sender = db, analyzer, sender

    async def analyze_one(self, now=None):
        now = now or datetime.now(timezone.utc)
        token = uuid4().hex
        row = await self.db.claim_reminder('analysis', now.timestamp(), token)
        if not row:
            return None
        try:
            post = recover_post(row)
            dates = await asyncio.wait_for(self.analyzer.analyze(post), timeout=240)
            due = dates.reminder_at
            status = 'pending' if due else 'no_date'
            if due and row['historical'] and due.timestamp() < now.timestamp() - config.HISTORICAL_CATCHUP_DAYS * 86400:
                status = 'expired'
            await self.db.finish_reminder(row['post_id'], token, status=status,
                end_at=dates.end_at.timestamp() if dates.end_at else None,
                winner_at=dates.winner_at.timestamp() if dates.winner_at else None,
                reminder_at=due.timestamp() if due else None, reason=dates.reason)
            log.info('Raffle %s: %s; end=%s winner=%s reminder=%s', row['post_id'], status, dates.end_at, dates.winner_at, due)
            return status
        except (AnalysisUnavailable, asyncio.TimeoutError) as exc:
            await self.db.finish_reminder(row['post_id'], token, next_attempt_at=now.timestamp() + getattr(exc, 'delay', 60))
            log.warning('Raffle date analysis deferred: %s', row['post_id'])
            return 'deferred'
        except ValueError:
            attempts = row['attempts'] + 1
            await self.db.finish_reminder(row['post_id'], token, attempts=attempts,
                status='invalid' if attempts >= 3 else 'analysis_pending', next_attempt_at=now.timestamp() + 3600)
            log.warning('Invalid raffle date analysis: %s (attempt %s)', row['post_id'], attempts)
            return 'invalid'

    async def send_one(self, now=None):
        now = now or datetime.now(timezone.utc)
        token = uuid4().hex
        row = await self.db.claim_reminder('delivery', now.timestamp(), token)
        if not row:
            return None
        log.info('Reminder due: %s', row['post_id'])
        try:
            sent = await asyncio.wait_for(self.sender(row), timeout=60)
        except Exception as exc:
            # Exception text from HTTP clients may contain webhook credentials.
            log.warning('Reminder send failed: %s (%s)', row['post_id'], type(exc).__name__)
            sent = False
        if sent:
            await self.db.finish_reminder(row['post_id'], token, status='sent', sent_at=now.timestamp())
            log.info('Reminder sent: %s', row['post_id'])
            return 'sent'
        await self.db.finish_reminder(row['post_id'], token, next_attempt_at=now.timestamp() + 300)
        return 'failed'
