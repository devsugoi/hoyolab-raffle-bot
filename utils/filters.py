"""Keyword matching and expiration helpers for HoYoLAB posts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from utils.api import HoyolabPost

STRONG_KEYWORDS: tuple[str, ...] = (
    "raffle",
    "giveaway",
    "lucky draw",
    "merch",
    "comment to enter",
    "leave a comment",
    "prize",
    "winner",
    "winners",
    "sweepstakes",
)

REWARD_KEYWORDS: tuple[str, ...] = (
    "primogems",
    "primogem",
    "stellar jade",
    "polychrome",
    "crystals",
    "crystal",
    "aspect gems",
    "aspect gem",
)

ACTION_KEYWORDS: tuple[str, ...] = (
    "redeem",
    "win",
    "draw",
    "lottery",
    "codes",
    "code",
    "enter",
    "participate",
    "chance to",
)

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_TIME = r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
_ISO_DATE = rf"\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}{_TIME}"
_NAMED_DATE = rf"{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}{_TIME}"
_SEP = r"(?:-|–|—|~|to)"

# Conservative date windows found in official event copy.
_PERIOD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:event\s*period|event\s*duration|duration|event\s*time)\s*[:\-–]?\s*(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
    re.compile(rf"({_ISO_DATE})\s*{_SEP}\s*({_ISO_DATE})", re.IGNORECASE),
    re.compile(rf"until\s+({_ISO_DATE}|{_NAMED_DATE})", re.IGNORECASE),
    re.compile(rf"({_NAMED_DATE})\s*{_SEP}\s*({_NAMED_DATE})", re.IGNORECASE),
)

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%B %d, %Y %H:%M:%S",
    "%B %d, %Y %H:%M",
    "%B %d, %Y",
    "%b %d, %Y %H:%M",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def is_raffle_post(post: HoyolabPost, extra_keywords: Iterable[str] = ()) -> bool:
    """Return True when title/snippet look like a giveaway or raffle."""
    haystack = f"{post.title}\n{post.snippet}".lower()
    extras = tuple(keyword.lower() for keyword in extra_keywords if keyword)

    if _contains_any(haystack, STRONG_KEYWORDS) or _contains_any(haystack, extras):
        return True
    return _contains_any(haystack, REWARD_KEYWORDS) and _contains_any(
        haystack, ACTION_KEYWORDS
    )


def _parse_datetime(raw: str) -> datetime | None:
    cleaned = re.sub(r"\s*\(UTC[^)]*\)", "", raw, flags=re.IGNORECASE)
    cleaned = cleaned.strip().replace("T", " ").rstrip("Zz")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _match_range(text: str) -> tuple[datetime | None, datetime | None, str] | None:
    for pattern in (_PERIOD_PATTERNS[1], _PERIOD_PATTERNS[3]):
        match = pattern.search(text)
        if match:
            return (
                _parse_datetime(match.group(1)),
                _parse_datetime(match.group(2)),
                match.group(0)[:180],
            )
    until = _PERIOD_PATTERNS[2].search(text)
    if until:
        return None, _parse_datetime(until.group(1)), until.group(0)[:180]
    return None


def extract_event_period(text: str) -> tuple[datetime | None, datetime | None, str | None]:
    """Best-effort start/end timestamps plus the original period string."""
    if not text:
        return None, None, None

    labeled = _PERIOD_PATTERNS[0].search(text)
    if labeled:
        label = labeled.group(1).strip()
        nested = _match_range(label)
        if nested:
            start, end, span = nested
            return start, end, span
        return None, None, label[:180]

    ranged = _match_range(text)
    if ranged:
        start, end, span = ranged
        return start, end, span
    return None, None, None


def is_expired(post: HoyolabPost, now: datetime | None = None) -> bool:
    moment = now or datetime.now(timezone.utc)
    end_at = post.end_at
    if end_at is None:
        return False
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    return end_at < moment
