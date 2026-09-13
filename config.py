"""Environment and game configuration for the HoYoLAB raffle watcher."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

BASE_DIR = Path(__file__).resolve().parent


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return int(raw)


@dataclass(frozen=True)
class GameInfo:
    gids: int
    name: str
    color: int
    thumbnail: str


# Public HoYoLAB / HoYoverse game icons used as embed thumbnails/author icons.
GAMES: dict[int, GameInfo] = {
    1: GameInfo(
        gids=1,
        name="Honkai Impact 3rd",
        color=0xFF6B9D,
        thumbnail="https://fastcdn.hoyoverse.com/static-resource-v2/2023/11/07/bbb4fb1d8f34a9a0ed3f9f40988b56aa_7317933270117164295.png",
    ),
    2: GameInfo(
        gids=2,
        name="Genshin Impact",
        color=0x59A6D4,
        thumbnail="https://fastcdn.hoyoverse.com/static-resource-v2/2023/11/08/9ebfb23326e9c4ca998f97b1f8f67773_3931639537141793944.png",
    ),
    6: GameInfo(
        gids=6,
        name="Honkai: Star Rail",
        color=0xC9A227,
        thumbnail="https://fastcdn.hoyoverse.com/static-resource-v2/2023/11/08/bb3a1c8bf65b3db60d4a5482aa43c20f_6780736678187736972.png",
    ),
    8: GameInfo(
        gids=8,
        name="Zenless Zone Zero",
        color=0xFF6A00,
        thumbnail="https://fastcdn.hoyoverse.com/static-resource-v2/2024/07/08/1d5a1089297c61647d237a1e29e37e58_2500095810673716054.png",
    ),
    9: GameInfo(
        gids=9,
        name="Honkai: Nexus Anima",
        color=0x5EC8C0,
        thumbnail="https://fastcdn.hoyoverse.com/static-resource-v2/2026/06/22/5095f59b1457997c7166a22a89a60a7d_4719883413651054051.png",
    ),
}

NEWS_TYPES: tuple[int, ...] = (1, 2, 3)  # notices, events, info

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_CHANNEL_ID = _optional_int("DISCORD_CHANNEL_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
DISCORD_GUILD_ID = _optional_int("DISCORD_GUILD_ID")

CHECK_INTERVAL_SECONDS = max(60, _int_env("CHECK_INTERVAL_SECONDS", 420))
CHECK_JITTER_SECONDS = max(0, _int_env("CHECK_JITTER_SECONDS", 90))
HOYOLAB_LANG = os.getenv("HOYOLAB_LANG", "en-us").strip() or "en-us"
SQLITE_PATH = Path(os.getenv("SQLITE_PATH", str(BASE_DIR / "data" / "seen_posts.db")))

RAFFLE_KEYWORDS = tuple(
    keyword.strip().lower()
    for keyword in os.getenv("RAFFLE_KEYWORDS", "").split(",")
    if keyword.strip()
)

HOYOLAB_HOST = "https://bbs-api-os.hoyolab.com"
HOYOLAB_SITE = "https://www.hoyolab.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Raffle result reminders: date-only facts use this local evening time.
REMINDER_TIMEZONE = os.getenv("REMINDER_TIMEZONE", "Asia/Singapore")
ZoneInfo(REMINDER_TIMEZONE)  # Fail early for an invalid IANA timezone.
REMINDER_DEFAULT_HOUR = _int_env("REMINDER_DEFAULT_HOUR", 18)
if not 0 <= REMINDER_DEFAULT_HOUR <= 23:
    raise ValueError("REMINDER_DEFAULT_HOUR must be 0..23")
REMINDER_POLL_SECONDS = max(10, _int_env("REMINDER_POLL_SECONDS", 60))
HISTORICAL_CATCHUP_DAYS = max(0, _int_env("HISTORICAL_CATCHUP_DAYS", 30))


def _numbered_env(prefix: str) -> tuple[str, ...]:
    """Primary plus _2 through _20, in order; skip blanks, gaps and duplicates."""
    values = []
    for name in (prefix, *(f"{prefix}_{index}" for index in range(2, 21))):
        value = os.getenv(name, "").strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEYS = _numbered_env("GEMINI_API_KEY")
GEMINI_FALLBACK_MODELS = _numbered_env("GEMINI_FALLBACK_MODEL")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
GEMINI_MIN_INTERVAL_SECONDS = max(1, _int_env("GEMINI_MIN_INTERVAL_SECONDS", 15))
