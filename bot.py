"""HoYoLAB raffle Discord bot entrypoint."""

from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hoyolab-raffle-bot")


class RaffleBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.hoyolab_watcher")
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s guild slash command(s) to %s", len(synced), config.DISCORD_GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global slash command(s)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")


def main() -> None:
    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN is missing. Copy .env.example to .env and set your bot token.")
        sys.exit(1)
    bot = RaffleBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
