"""Background HoYoLAB watcher and slash commands."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import discord
import httpx
from discord import app_commands
from discord.ext import commands

import config
from utils.api import HoyolabClient, HoyolabPost
from utils.db import Database
from utils.filters import extract_event_period, is_expired, is_raffle_post

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 400


@dataclass
class ScanResult:
    fetched: int = 0
    seeded: int = 0
    skipped: int = 0
    matched: int = 0
    notified: int = 0
    expired: int = 0
    errors: list[str] | None = None
    first_run: bool = False
    seeded_games: list[str] | None = None

    def summary(self) -> str:
        if self.first_run:
            return (
                f"First run seeded **{self.seeded}** existing posts without sending alerts "
                f"(fetched {self.fetched}). Future raffles will notify this channel."
            )
        parts = [
            f"Fetched **{self.fetched}**",
            f"new raffles **{self.matched}**",
            f"notified **{self.notified}**",
            f"already tracked **{self.skipped}**",
            f"expired **{self.expired}**",
        ]
        text = " · ".join(parts)
        if self.seeded:
            names = ", ".join(self.seeded_games or [])
            suffix = f" ({names})" if names else ""
            text += f"\nSeeded **{self.seeded}** existing posts for newly added game(s){suffix}."
        if self.errors:
            text += f"\nWarnings: {'; '.join(self.errors[:3])}"
        return text


def _truncate(text: str, limit: int = SNIPPET_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _game(post: HoyolabPost) -> config.GameInfo | None:
    return config.GAMES.get(post.gids)


def build_embed(post: HoyolabPost, *, sample: bool = False) -> discord.Embed:
    game = _game(post)
    game_name = game.name if game else f"Game {post.gids}"
    color = game.color if game else 0x2B2D31
    thumbnail = game.thumbnail if game else None

    embed = discord.Embed(
        title=post.title[:256],
        url=post.url,
        description=_truncate(post.snippet) or "*No preview available.*",
        color=color,
        timestamp=post.created_at,
    )
    official_tag = "Official" if post.official else "HoYoLAB"
    embed.set_author(name=f"{game_name} · {official_tag}", icon_url=thumbnail)
    embed.set_thumbnail(url=thumbnail)
    if post.cover_url:
        embed.set_image(url=post.cover_url)

    embed.add_field(name="Author", value=post.author or "Unknown", inline=True)
    embed.add_field(name="Game", value=game_name, inline=True)
    period = post.period_label
    if not period and post.end_at:
        period = post.end_at.strftime("%Y-%m-%d %H:%M UTC")
    embed.add_field(
        name="Event Period",
        value=period or "Not listed",
        inline=False,
    )
    embed.add_field(name="HoYoLAB", value=f"[Open post]({post.url})", inline=False)
    footer = f"Post ID {post.post_id} · HoYoLAB raffle watcher"
    if sample:
        footer += " · sample preview"
    embed.set_footer(text=footer)
    return embed


class HoyolabWatcher(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = Database(config.SQLITE_PATH)
        self.client = HoyolabClient(language=config.HOYOLAB_LANG)
        self._scan_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.started_at = datetime.now(timezone.utc)
        self.last_check_at: datetime | None = None
        self.last_result: ScanResult | None = None
        self.last_error: str | None = None

    async def cog_load(self) -> None:
        await self.db.init()
        self._task = asyncio.create_task(self._watch_loop(), name="hoyolab-watch")

    async def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()

    def next_delay(self) -> float:
        jitter = random.uniform(-config.CHECK_JITTER_SECONDS, config.CHECK_JITTER_SECONDS)
        return max(60.0, config.CHECK_INTERVAL_SECONDS + jitter)

    async def _watch_loop(self) -> None:
        await self.bot.wait_until_ready()
        log.info("HoYoLAB watcher started")
        try:
            await self.run_scan(notify=True)
        except Exception:
            log.exception("Initial HoYoLAB scan failed")
        while not self.bot.is_closed():
            delay = self.next_delay()
            log.info("Next HoYoLAB check in %.0fs", delay)
            try:
                await asyncio.sleep(delay)
                await self.run_scan(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("HoYoLAB watch loop failed")

    async def get_alert_channel_id(self) -> int | None:
        stored = await self.db.get_setting("alert_channel_id")
        if stored:
            try:
                return int(stored)
            except ValueError:
                log.warning("Invalid stored alert_channel_id: %s", stored)
        return config.DISCORD_CHANNEL_ID

    async def _fetch_all_posts(self) -> tuple[list[HoyolabPost], list[str]]:
        posts: dict[str, HoyolabPost] = {}
        errors: list[str] = []
        for gids, game in config.GAMES.items():
            for news_type in config.NEWS_TYPES:
                try:
                    batch = await self.client.get_news_list(gids, news_type)
                except Exception as exc:  # noqa: BLE001 - isolate per-game failures
                    message = f"{game.name} type {news_type}: {exc}"
                    errors.append(message)
                    log.error("News fetch failed: %s", message)
                    continue
                for post in batch:
                    posts.setdefault(post.post_id, post)
                await asyncio.sleep(0.15)
        return list(posts.values()), errors

    async def _fetch_sample_post(self) -> HoyolabPost:
        """Pick a random recent official news post from any tracked game."""
        games = list(config.GAMES.items())
        random.shuffle(games)
        last_error: Exception | None = None
        for gids, game in games:
            types = list(config.NEWS_TYPES)
            random.shuffle(types)
            for news_type in types:
                try:
                    batch = await self.client.get_news_list(gids, news_type, page_size=15)
                except Exception as exc:  # noqa: BLE001 - try the next feed
                    last_error = exc
                    log.warning(
                        "Sample fetch failed for %s type %s: %s",
                        game.name,
                        news_type,
                        exc,
                    )
                    continue
                if not batch:
                    continue
                chosen = random.choice(batch)
                detailed = await self.client.get_post_full(chosen)
                return self._enrich_period(detailed)
        raise RuntimeError(
            f"HoYoLAB returned no recent posts{f': {last_error}' if last_error else '.'}"
        )

    def _enrich_period(self, post: HoyolabPost) -> HoyolabPost:
        _start, end, label = extract_event_period(f"{post.title}\n{post.snippet}")
        return replace(
            post,
            end_at=end or post.end_at,
            period_label=label or post.period_label,
        )

    async def run_scan(self, notify: bool) -> ScanResult:
        async with self._scan_lock:
            result = ScanResult()
            try:
                posts, errors = await self._fetch_all_posts()
                result.fetched = len(posts)
                result.errors = errors or None

                if not await self.db.has_any_seen():
                    await self.db.mark_many_seen(posts, notified=False)
                    result.first_run = True
                    result.seeded = len(posts)
                    self.last_check_at = datetime.now(timezone.utc)
                    self.last_result = result
                    self.last_error = None
                    log.info("Seeded %s existing HoYoLAB posts", result.seeded)
                    return result

                known_games = await self.db.seen_game_ids()
                new_game_ids = {
                    post.gids
                    for post in posts
                    if post.gids in config.GAMES and post.gids not in known_games
                }
                if new_game_ids:
                    to_seed = [post for post in posts if post.gids in new_game_ids]
                    await self.db.mark_many_seen(to_seed, notified=False)
                    result.seeded = len(to_seed)
                    result.seeded_games = [
                        config.GAMES[gids].name for gids in sorted(new_game_ids)
                    ]
                    log.info(
                        "Seeded %s posts for newly added games: %s",
                        result.seeded,
                        ", ".join(result.seeded_games),
                    )

                for post in posts:
                    if await self.db.is_seen(post.post_id):
                        result.skipped += 1
                        continue

                    detailed = post
                    if post.official or is_raffle_post(post, config.RAFFLE_KEYWORDS):
                        detailed = await self.client.get_post_full(post)
                    detailed = self._enrich_period(detailed)

                    if is_expired(detailed):
                        await self.db.mark_seen(detailed, notified=False)
                        result.expired += 1
                        continue

                    if not detailed.official:
                        await self.db.mark_seen(detailed, notified=False)
                        result.skipped += 1
                        continue

                    if not is_raffle_post(detailed, config.RAFFLE_KEYWORDS):
                        await self.db.mark_seen(detailed, notified=False)
                        result.skipped += 1
                        continue

                    result.matched += 1
                    if not notify:
                        continue
                    sent = await self._notify(detailed)
                    if sent:
                        await self.db.mark_seen(detailed, notified=True)
                        result.notified += 1
                    else:
                        log.warning(
                            "Leaving post %s untracked so it can retry next scan",
                            detailed.post_id,
                        )

                self.last_check_at = datetime.now(timezone.utc)
                self.last_result = result
                self.last_error = None
                log.info(
                    "Scan complete: fetched=%s matched=%s notified=%s skipped=%s",
                    result.fetched,
                    result.matched,
                    result.notified,
                    result.skipped,
                )
                return result
            except Exception as exc:
                self.last_check_at = datetime.now(timezone.utc)
                self.last_error = str(exc)
                log.exception("Scan failed")
                raise

    async def _notify(self, post: HoyolabPost, *, sample: bool = False) -> bool:
        embed = build_embed(post, sample=sample)
        channel_ok = await self._send_channel(embed)
        webhook_ok = await self._send_webhook(embed)
        if config.DISCORD_WEBHOOK_URL:
            return channel_ok or webhook_ok
        return channel_ok

    async def _send_channel(self, embed: discord.Embed) -> bool:
        channel_id = await self.get_alert_channel_id()
        if channel_id is None:
            log.warning("No alert channel configured; skipping Discord channel send")
            return False
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                log.error("Could not fetch alert channel %s: %s", channel_id, exc)
                return False
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.error("Alert destination %s is not a text channel", channel_id)
            return False
        try:
            await channel.send(embed=embed)
            return True
        except discord.HTTPException as exc:
            log.error("Failed to send channel alert: %s", exc)
            return False

    async def _send_webhook(self, embed: discord.Embed) -> bool:
        if not config.DISCORD_WEBHOOK_URL:
            return False
        payload: dict[str, Any] = {
            "username": "HoYoLAB Raffle Watcher",
            "embeds": [embed.to_dict()],
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(config.DISCORD_WEBHOOK_URL, json=payload)
                if response.status_code >= 400:
                    log.error(
                        "Webhook returned %s: %s",
                        response.status_code,
                        response.text[:300],
                    )
                    return False
            return True
        except httpx.HTTPError as exc:
            log.error("Webhook send failed: %s", exc)
            return False

    @app_commands.command(
        name="set_channel",
        description="Set the channel that receives HoYoLAB raffle alerts",
    )
    @app_commands.describe(channel="Text channel for raffle notifications")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await self.db.set_setting("alert_channel_id", str(channel.id))
        await interaction.response.send_message(
            f"Raffle alerts will be sent to {channel.mention}.",
            ephemeral=True,
        )

    @set_channel.error
    async def set_channel_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Channels** permission to set the alert channel."
        else:
            log.exception("set_channel failed")
            message = "Could not update the alert channel."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="check_now",
        description="Force an immediate check for new HoYoLAB raffle posts",
    )
    @app_commands.checks.cooldown(1, 30.0)
    async def check_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.run_scan(notify=True)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Check failed: `{exc}`",
                ephemeral=True,
            )
            return
        await interaction.followup.send(result.summary(), ephemeral=True)

    @check_now.error
    async def check_now_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Please wait {error.retry_after:.0f}s before checking again."
        else:
            log.exception("check_now failed")
            message = "The check could not be started."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="sample",
        description="Post a sample alert using a random recent HoYoLAB news post",
    )
    @app_commands.checks.cooldown(1, 30.0)
    async def sample(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            post = await self._fetch_sample_post()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Could not fetch a HoYoLAB post: `{exc}`",
                ephemeral=True,
            )
            return

        sent = await self._notify(post, sample=True)
        sent_here = False
        if not sent and isinstance(
            interaction.channel, (discord.TextChannel, discord.Thread)
        ):
            try:
                await interaction.channel.send(embed=build_embed(post, sample=True))
                sent_here = True
            except discord.HTTPException as exc:
                log.error("Sample fallback send failed: %s", exc)

        if sent or sent_here:
            game = config.GAMES.get(post.gids)
            game_name = game.name if game else f"game {post.gids}"
            where = "the configured alert channel/webhook" if sent else "this channel"
            await interaction.followup.send(
                f"Sample preview of [{post.title}]({post.url}) ({game_name}) sent to {where}. "
                "This uses a real recent post and is **not** recorded as a raffle alert.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Fetched a post, but nowhere to send it. Set `/set_channel`, "
            "`DISCORD_CHANNEL_ID`, or `DISCORD_WEBHOOK_URL`.",
            ephemeral=True,
        )

    @sample.error
    async def sample_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Please wait {error.retry_after:.0f}s before sampling again."
        else:
            log.exception("sample failed")
            message = "The sample post could not be started."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="status",
        description="Show bot status, tracked posts, and check interval",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        tracked = await self.db.tracked_count()
        channel_id = await self.get_alert_channel_id()
        channel_text = f"<#{channel_id}>" if channel_id else "Not set — use `/set_channel`"
        last_check = (
            discord.utils.format_dt(self.last_check_at, style="R")
            if self.last_check_at
            else "Never"
        )
        last_result = self.last_result.summary() if self.last_result else "No scan yet"
        if self.last_error:
            last_result = f"Error: `{self.last_error}`"

        embed = discord.Embed(
            title="HoYoLAB raffle watcher",
            color=0x59A6D4,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Status", value="Online" if self.bot.is_ready() else "Starting", inline=True)
        embed.add_field(
            name="Uptime",
            value=discord.utils.format_dt(self.started_at, style="R"),
            inline=True,
        )
        embed.add_field(name="Tracked posts", value=str(tracked), inline=True)
        embed.add_field(
            name="Check interval",
            value=f"{config.CHECK_INTERVAL_SECONDS}s ± {config.CHECK_JITTER_SECONDS}s",
            inline=True,
        )
        embed.add_field(name="Language", value=config.HOYOLAB_LANG, inline=True)
        embed.add_field(name="Alert channel", value=channel_text, inline=True)
        embed.add_field(name="Last check", value=last_check, inline=True)
        webhook = "Enabled" if config.DISCORD_WEBHOOK_URL else "Disabled"
        embed.add_field(name="Webhook", value=webhook, inline=True)
        games = ", ".join(game.name for game in config.GAMES.values())
        embed.add_field(name="Games", value=games, inline=False)
        embed.add_field(name="Last result", value=last_result[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HoyolabWatcher(bot))
