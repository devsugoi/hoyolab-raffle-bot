"""Async HoYoLAB public news client."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx

from config import HOYOLAB_HOST, HOYOLAB_SITE, USER_AGENT

log = logging.getLogger(__name__)

NEWS_LIST_PATH = "/community/post/wapi/getNewsList"
POST_FULL_PATH = "/community/post/wapi/getPostFull"

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (1.0, 2.0, 4.0)


class HoyolabAPIError(RuntimeError):
    """Raised when HoYoLAB returns a non-success payload or HTTP error."""


@dataclass
class HoyolabPost:
    post_id: str
    gids: int
    title: str
    snippet: str
    url: str
    author: str
    official: bool
    created_at: datetime | None
    cover_url: str | None
    end_at: datetime | None
    news_type: int
    period_label: str | None = None


def _html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unix_to_dt(value: Any) -> datetime | None:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def _cover_from_row(row: dict[str, Any], post: dict[str, Any]) -> str | None:
    for item in _as_list(row.get("cover_list")):
        url = _as_dict(item).get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    images = _as_list(post.get("images"))
    if images and isinstance(images[0], str) and images[0].startswith("http"):
        return images[0]
    cover = post.get("cover")
    if isinstance(cover, str) and cover.startswith("http"):
        return cover
    return None


def _is_official(row: dict[str, Any], post: dict[str, Any], user: dict[str, Any]) -> bool:
    if row.get("is_official_master") is True:
        return True
    status = _as_dict(post.get("post_status")) or _as_dict(row.get("post_status"))
    if status.get("is_official") is True:
        return True
    cert = _as_dict(user.get("certification"))
    if cert.get("type") in (1, 2) or str(cert.get("label", "")).lower() == "official":
        return True
    if "official" in str(user.get("nickname") or "").lower():
        return True
    forum = _as_dict(row.get("forum"))
    if str(forum.get("name", "")).lower() in {"official", "official notice"}:
        return True
    # getNewsList is HoYoLAB's official announcement feed.
    return True


def _parse_row(row: Any, gids: int, news_type: int) -> HoyolabPost | None:
    try:
        data = _as_dict(row)
        post = _as_dict(data.get("post"))
        user = _as_dict(data.get("user"))
        post_id = str(post.get("post_id") or "").strip()
        title = _html_to_text(str(post.get("subject") or "")).strip()
        if not post_id or not title:
            return None
        snippet = _html_to_text(str(post.get("content") or post.get("desc") or ""))
        game_id = post.get("game_id", gids)
        try:
            parsed_gids = int(game_id)
        except (TypeError, ValueError):
            parsed_gids = gids
        return HoyolabPost(
            post_id=post_id,
            gids=parsed_gids,
            title=title,
            snippet=snippet,
            url=f"{HOYOLAB_SITE}/article/{post_id}",
            author=str(user.get("nickname") or "HoYoLAB").strip() or "HoYoLAB",
            official=_is_official(data, post, user),
            created_at=_unix_to_dt(post.get("created_at")),
            cover_url=_cover_from_row(data, post),
            end_at=_unix_to_dt(post.get("event_end_date")),
            news_type=news_type,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        log.warning("Skipping malformed HoYoLAB news row: %s", exc)
        return None


class HoyolabClient:
    def __init__(self, language: str, timeout: float = 20.0) -> None:
        self.language = language
        self._client = httpx.AsyncClient(
            base_url=HOYOLAB_HOST,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"{HOYOLAB_SITE}/",
                "Origin": HOYOLAB_SITE,
                "X-Rpc-Language": language,
                "x-rpc-client-type": "4",
                "Accept": "application/json, text/plain, */*",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for delay in (*_RETRY_DELAYS, None):
            try:
                response = await self._client.get(path, params=params)
                if response.status_code in _RETRY_STATUSES and delay is not None:
                    log.warning(
                        "HoYoLAB %s returned %s; retrying in %.0fs",
                        path,
                        response.status_code,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise HoyolabAPIError(f"Unexpected JSON type from {path}")
                retcode = payload.get("retcode", 0)
                if retcode not in (0, None):
                    raise HoyolabAPIError(
                        f"HoYoLAB retcode {retcode} on {path}: {payload.get('message')}"
                    )
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise HoyolabAPIError(f"HoYoLAB payload missing data object for {path}")
                return data
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if status not in _RETRY_STATUSES or delay is None:
                    raise HoyolabAPIError(f"HoYoLAB HTTP {status} on {path}") from exc
                log.warning("HoYoLAB %s returned %s; retrying in %.0fs", path, status, delay)
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if delay is None:
                    break
                log.warning("HoYoLAB request failed (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
            except (ValueError, KeyError, TypeError) as exc:
                raise HoyolabAPIError(f"HoYoLAB schema error on {path}: {exc}") from exc
        raise HoyolabAPIError(f"HoYoLAB request failed for {path}: {last_error}")

    async def get_news_list(self, gids: int, news_type: int, page_size: int = 20) -> list[HoyolabPost]:
        try:
            data = await self._get(
                NEWS_LIST_PATH,
                {
                    "gids": gids,
                    "type": news_type,
                    "page_size": page_size,
                    "client_type": 4,
                },
            )
        except HoyolabAPIError as exc:
            log.error("Failed to fetch news list gids=%s type=%s: %s", gids, news_type, exc)
            return []

        posts: list[HoyolabPost] = []
        for row in _as_list(data.get("list")):
            parsed = _parse_row(row, gids, news_type)
            if parsed is not None:
                posts.append(parsed)
        return posts

    async def get_post_full(self, post: HoyolabPost) -> HoyolabPost:
        try:
            data = await self._get(POST_FULL_PATH, {"post_id": post.post_id})
        except HoyolabAPIError as exc:
            log.warning("getPostFull failed for %s: %s", post.post_id, exc)
            return post

        try:
            wrapper = _as_dict(data.get("post"))
            inner = _as_dict(wrapper.get("post"))
            user = _as_dict(wrapper.get("user"))
            content = _html_to_text(str(inner.get("content") or post.snippet))
            title = _html_to_text(str(inner.get("subject") or post.title)) or post.title
            cover = _cover_from_row(wrapper, inner) or post.cover_url
            author = str(user.get("nickname") or post.author).strip() or post.author
            official = post.official or _is_official(wrapper, inner, user)
            created_at = _unix_to_dt(inner.get("created_at")) or post.created_at
            end_at = _unix_to_dt(inner.get("event_end_date")) or post.end_at
            return replace(
                post,
                title=title,
                snippet=content,
                cover_url=cover,
                author=author,
                official=official,
                created_at=created_at,
                end_at=end_at,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            log.warning("Unexpected getPostFull schema for %s: %s", post.post_id, exc)
            return post
