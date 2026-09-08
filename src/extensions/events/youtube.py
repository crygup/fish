from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlsplit

import discord

from utils.google import google_json

YOUTUBE_WEBSUB_CALLBACK = "https://api.crygup.com/fishie/youtube/websub"
YOUTUBE_HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
YOUTUBE_EVENT_TYPES = frozenset({"video", "live", "short", "community"})
YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
YOUTUBE_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
_INITIAL_DATA_MARKERS = ("var ytInitialData = ", 'window["ytInitialData"] = ')


def youtube_websub_verify_token(secret: str, channel_id: str) -> str:
    """Create a stable, unguessable verification token for one subscription."""
    return hmac.new(
        secret.encode(),
        f"youtube-websub:{channel_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def normalize_youtube_events(values: Any) -> tuple[str, ...]:
    """Return unique, supported YouTube event names in display order."""
    if isinstance(values, str):
        values = re.split(r"[\s,]+", values.strip().lower())
    if not isinstance(values, (list, tuple, set)):
        return ()
    selected = {str(value).strip().lower() for value in values}
    return tuple(
        event for event in ("video", "live", "short", "community") if event in selected
    )


def _text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    return "".join(
        str(run.get("text", "")) for run in runs if isinstance(run, dict)
    ).strip()


def _extract_balanced_json(document: str, marker: str) -> dict[str, Any] | None:
    start = document.find(marker)
    if start < 0:
        return None
    start = document.find("{", start + len(marker))
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(document)):
        char = document[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(document[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _walk_renderers(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"backstagePostRenderer", "postRenderer"} and isinstance(
                child, dict
            ):
                yield child
            yield from _walk_renderers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_renderers(child)


def parse_youtube_community_posts(document: str) -> list[dict[str, Any]]:
    """Parse public post cards from a YouTube community tab."""
    initial_data = None
    for marker in _INITIAL_DATA_MARKERS:
        initial_data = _extract_balanced_json(document, marker)
        if initial_data is not None:
            break
    if initial_data is None:
        return []

    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for renderer in _walk_renderers(initial_data):
        post_id = str(renderer.get("postId") or "")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        author = _text(renderer.get("authorText"))
        content = _text(renderer.get("contentText"))
        published = _text(renderer.get("publishedTimeText"))
        images: list[str] = []
        for attachment in renderer.get(
            "backstageAttachment",
        ), renderer.get("attachment"):
            if not isinstance(attachment, dict):
                continue
            for item in _walk_values_for_key(attachment, "thumbnails"):
                if not isinstance(item, list) or not item:
                    continue
                thumbnail = item[-1]
                if isinstance(thumbnail, dict) and thumbnail.get("url"):
                    url = str(thumbnail["url"])
                    if url not in images:
                        images.append(url)
        video_id = ""
        for item in _walk_values_for_key(renderer, "videoId"):
            if isinstance(item, str) and item:
                video_id = item
                break
        posts.append(
            {
                "id": post_id,
                "author": author,
                "text": content,
                "published": published,
                "images": images[:4],
                "video_id": video_id,
            }
        )
    return posts


def _walk_values_for_key(value: Any, wanted: str):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == wanted:
                yield child
            yield from _walk_values_for_key(child, wanted)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values_for_key(child, wanted)


def _iso_duration_seconds(value: str) -> int | None:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value,
    )
    if not match:
        return None
    parts = {key: int(item or 0) for key, item in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


class YouTubeNotifications:
    bot: Any

    def _youtube_api_keys(self) -> list[str]:
        values = self.bot.config.get("keys", {}).get("google")
        if isinstance(values, str):
            return [values] if values else []
        if isinstance(values, (list, tuple)):
            return [str(value) for value in values if value]
        return []

    def _youtube_websub_secret(self) -> str | None:
        keys = self.bot.config.get("keys", {})
        value = keys.get("youtube_websub_secret")
        return str(value) if value else None

    @staticmethod
    def youtube_topic(channel_id: str) -> str:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    async def resolve_youtube_channel(self, value: str) -> dict[str, Any] | None:
        raw = value.strip()
        if not raw:
            return None
        channel_id = raw if YOUTUBE_CHANNEL_ID_RE.fullmatch(raw) else None
        handle: str | None = None
        if raw.startswith(("http://", "https://")):
            parsed = urlsplit(raw)
            if parsed.hostname not in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
            }:
                return None
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] == "channel":
                channel_id = parts[1]
            elif parts and parts[0].startswith("@"):
                handle = parts[0]
        elif raw.startswith("@"):
            handle = raw
        else:
            handle = f"@{raw}"

        keys = self._youtube_api_keys()
        if not keys:
            self.bot.logger.warning("YouTube notifications require a Google API key")
            return None
        params: dict[str, str] = {"part": "snippet"}
        if channel_id and YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id):
            params["id"] = channel_id
        elif handle and YOUTUBE_HANDLE_RE.fullmatch(handle):
            params["forHandle"] = handle
        else:
            return None
        try:
            data = await google_json(
                self.bot.session,
                "https://www.googleapis.com/youtube/v3/channels",
                keys=keys,
                params=params,
            )
        except Exception as error:
            self.bot.logger.warning("Could not resolve YouTube channel: %s", error)
            return None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            self.bot.logger.warning("YouTube channel lookup returned no results")
            return None
        item = items[0]
        if not isinstance(item, dict) or not item.get("id"):
            return None
        snippet: dict[str, Any] = (
            cast(dict[str, Any], item["snippet"])
            if isinstance(item.get("snippet"), dict)
            else {}
        )
        custom_url = str(snippet.get("customUrl") or "")
        return {
            "id": str(item["id"]),
            "name": str(snippet.get("title") or item["id"]),
            "handle": custom_url if custom_url.startswith("@") else handle,
            "thumbnail": _best_thumbnail(snippet.get("thumbnails")),
        }

    async def get_youtube_video(self, video_id: str) -> dict[str, Any] | None:
        keys = self._youtube_api_keys()
        if not keys:
            return None
        try:
            data = await google_json(
                self.bot.session,
                "https://www.googleapis.com/youtube/v3/videos",
                keys=keys,
                params={
                    "part": "snippet,contentDetails,liveStreamingDetails",
                    "id": video_id,
                },
            )
        except Exception as error:
            self.bot.logger.warning(
                "Could not fetch YouTube video %s: %s", video_id, error
            )
            return None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        return item if isinstance(item, dict) else None

    async def _youtube_video_type(self, item: dict[str, Any]) -> str:
        snippet: dict[str, Any] = (
            cast(dict[str, Any], item["snippet"])
            if isinstance(item.get("snippet"), dict)
            else {}
        )
        live_details = item.get("liveStreamingDetails")
        if isinstance(live_details, dict) or snippet.get("liveBroadcastContent") in {
            "live",
            "upcoming",
        }:
            return "live"
        details: dict[str, Any] = (
            cast(dict[str, Any], item["contentDetails"])
            if isinstance(item.get("contentDetails"), dict)
            else {}
        )
        duration = _iso_duration_seconds(str(details.get("duration") or ""))
        if duration is not None and duration <= 180:
            video_id = str(item.get("id") or "")
            try:
                async with self.bot.session.get(
                    f"https://www.youtube.com/shorts/{video_id}",
                    allow_redirects=True,
                ) as response:
                    if response.status == 200 and response.url.path.startswith(
                        "/shorts/"
                    ):
                        return "short"
            except Exception:
                pass
        return "video"

    async def ensure_youtube_subscription(self, channel_id: str) -> bool:
        secret = self._youtube_websub_secret()
        if not secret:
            self.bot.logger.warning(
                "YouTube WebSub is disabled: no callback secret is configured"
            )
            return False
        claimed = await self.bot.pool.fetchval(
            """
            INSERT INTO youtube_websub_subscriptions
                (youtube_channel_id, status, updated_at)
            VALUES ($1, 'creating', now())
            ON CONFLICT (youtube_channel_id) DO UPDATE
            SET status = 'creating', updated_at = now(), last_error = NULL
            WHERE youtube_websub_subscriptions.status NOT IN
                    ('enabled', 'creating', 'pending')
               OR youtube_websub_subscriptions.updated_at < now() - interval '15 minutes'
               OR youtube_websub_subscriptions.lease_expires_at < now() + interval '1 day'
            RETURNING youtube_channel_id
            """,
            channel_id,
        )
        if claimed is None:
            return True
        try:
            verify_token = youtube_websub_verify_token(secret, channel_id)
            async with self.bot.session.post(
                YOUTUBE_HUB_URL,
                data={
                    "hub.callback": YOUTUBE_WEBSUB_CALLBACK,
                    "hub.topic": self.youtube_topic(channel_id),
                    "hub.verify": "async",
                    "hub.mode": "subscribe",
                    "hub.secret": secret,
                    "hub.verify_token": verify_token,
                    "hub.lease_seconds": "864000",
                },
            ) as response:
                ok = response.status in {202, 204}
                response_text = await response.text()
        except Exception as error:
            ok = False
            response_text = str(error)
        await self.bot.pool.execute(
            "UPDATE youtube_websub_subscriptions SET status = $2, "
            "updated_at = now(), last_error = $3 WHERE youtube_channel_id = $1",
            channel_id,
            "pending" if ok else "failed",
            None if ok else response_text[:1000],
        )
        if not ok:
            self.bot.logger.warning(
                "YouTube WebSub subscription failed for %s: %s",
                channel_id,
                response_text[:500],
            )
        return ok

    async def remove_youtube_subscription(self, channel_id: str) -> None:
        if await self.bot.pool.fetchval(
            "SELECT 1 FROM youtube_follows WHERE youtube_channel_id = $1 LIMIT 1",
            channel_id,
        ):
            return
        secret = self._youtube_websub_secret()
        if not secret:
            return
        try:
            verify_token = youtube_websub_verify_token(secret, channel_id)
            async with self.bot.session.post(
                YOUTUBE_HUB_URL,
                data={
                    "hub.callback": YOUTUBE_WEBSUB_CALLBACK,
                    "hub.topic": self.youtube_topic(channel_id),
                    "hub.verify": "async",
                    "hub.mode": "unsubscribe",
                    "hub.secret": secret,
                    "hub.verify_token": verify_token,
                },
            ) as response:
                if response.status not in {202, 204}:
                    return
        except Exception:
            return
        await self.bot.pool.execute(
            "UPDATE youtube_websub_subscriptions SET status = 'unsubscribing', "
            "updated_at = now() WHERE youtube_channel_id = $1",
            channel_id,
        )

    async def process_youtube_event(self, event_id: str | None = None) -> bool:
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT event_id, video_id, payload, attempts
                    FROM youtube_events
                    WHERE ($1::text IS NULL OR event_id = $1)
                      AND next_attempt_at <= now()
                      AND (status = 'pending' OR
                           (status = 'processing' AND updated_at < now() - interval '5 minutes'))
                    ORDER BY received_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    event_id,
                )
                if row is None:
                    return False
                await connection.execute(
                    "UPDATE youtube_events SET status = 'processing', "
                    "attempts = attempts + 1, updated_at = now() WHERE event_id = $1",
                    row["event_id"],
                )
        try:
            item = await self.get_youtube_video(str(row["video_id"]))
            if item is None:
                raise RuntimeError("YouTube video details are unavailable")
            event_type = await self._youtube_video_type(item)
            payload = dict(row["payload"])
            payload["video"] = item
            await self._dispatch_youtube_item(
                str(payload["channel_id"]),
                str(row["video_id"]),
                event_type,
                payload,
            )
        except Exception as error:
            attempts = int(row["attempts"])
            await self.bot.pool.execute(
                "UPDATE youtube_events SET status = $2, "
                "next_attempt_at = now() + ($3 * interval '1 second'), "
                "updated_at = now(), last_error = $4 WHERE event_id = $1",
                row["event_id"],
                "dead" if attempts >= 9 else "pending",
                min(300, 2 ** min(attempts, 8)),
                str(error)[:1000],
            )
            self.bot.logger.exception("YouTube event %s failed", row["event_id"])
            return False
        await self.bot.pool.execute(
            "UPDATE youtube_events SET status = 'done', updated_at = now(), "
            "last_error = NULL WHERE event_id = $1",
            row["event_id"],
        )
        return True

    async def _dispatch_youtube_item(
        self,
        channel_id: str,
        item_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT guild_id, youtube_channel_id, channel_name, channel_handle, "
            "announce_channel_id, message_template, event_types "
            "FROM youtube_follows WHERE youtube_channel_id = $1 "
            "AND $2 = ANY(event_types)",
            channel_id,
            event_type,
        )
        failed = 0
        for row in rows:
            if not await self._announce_youtube_once(
                row,
                item_id,
                event_type,
                payload,
            ):
                failed += 1
        if failed:
            raise RuntimeError(
                f"YouTube delivery failed for {failed} announcement destination(s)"
            )

    async def _announce_youtube_once(
        self, row: Any, item_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        claimed = await self.bot.pool.fetchval(
            """
            INSERT INTO youtube_announcement_deliveries
                (guild_id, youtube_channel_id, item_id, event_type, payload,
                 status, attempts)
            VALUES ($1, $2, $3, $4, $5::jsonb, 'processing', 1)
            ON CONFLICT (guild_id, youtube_channel_id, item_id, event_type) DO UPDATE
            SET status = 'processing',
                attempts = youtube_announcement_deliveries.attempts + 1,
                payload = EXCLUDED.payload, updated_at = now()
            WHERE (youtube_announcement_deliveries.status = 'pending'
                   AND youtube_announcement_deliveries.attempts < 10)
               OR (youtube_announcement_deliveries.status = 'processing'
                   AND youtube_announcement_deliveries.updated_at < now() - interval '5 minutes'
                   AND youtube_announcement_deliveries.attempts < 10)
            RETURNING item_id
            """,
            row["guild_id"],
            row["youtube_channel_id"],
            item_id,
            event_type,
            payload,
        )
        if claimed is None:
            status = await self.bot.pool.fetchval(
                "SELECT status FROM youtube_announcement_deliveries "
                "WHERE guild_id = $1 AND youtube_channel_id = $2 "
                "AND item_id = $3 AND event_type = $4",
                row["guild_id"],
                row["youtube_channel_id"],
                item_id,
                event_type,
            )
            return status == "done"
        announced = await self._announce_youtube(row, item_id, event_type, payload)
        await self.bot.pool.execute(
            "UPDATE youtube_announcement_deliveries SET status = $5, "
            "updated_at = now(), last_error = $6 "
            "WHERE guild_id = $1 AND youtube_channel_id = $2 "
            "AND item_id = $3 AND event_type = $4",
            row["guild_id"],
            row["youtube_channel_id"],
            item_id,
            event_type,
            (
                "done"
                if announced
                else (
                    "dead"
                    if await self.bot.pool.fetchval(
                        "SELECT attempts >= 10 FROM youtube_announcement_deliveries "
                        "WHERE guild_id = $1 AND youtube_channel_id = $2 "
                        "AND item_id = $3 AND event_type = $4",
                        row["guild_id"],
                        row["youtube_channel_id"],
                        item_id,
                        event_type,
                    )
                    else "pending"
                )
            ),
            None if announced else "Discord delivery failed",
        )
        return announced

    async def _announce_youtube(
        self, row: Any, item_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        channel = self.bot.get_channel(row["announce_channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(row["announce_channel_id"])
            except Exception as error:
                self.bot.logger.warning(
                    "Could not find YouTube announcement channel %s: %s",
                    row["announce_channel_id"],
                    error,
                )
                return False
        label = {
            "video": "New video",
            "live": "Live now",
            "short": "New Short",
            "community": "New community post",
        }[event_type]
        children: list[discord.ui.Item] = []
        if row["message_template"]:
            children.append(discord.ui.TextDisplay(str(row["message_template"])))
        images: list[str] = []
        if event_type == "community":
            post: dict[str, Any] = (
                cast(dict[str, Any], payload["post"])
                if isinstance(payload.get("post"), dict)
                else {}
            )
            url = (
                f"https://www.youtube.com/post/{item_id}"
                if item_id.startswith("Ug")
                else f"https://www.youtube.com/channel/{row['youtube_channel_id']}/community"
            )
            body = str(post.get("text") or "Open the post on YouTube.")
            title = f"{label} from {row['channel_name']}"
            images = [str(value) for value in post.get("images", []) if value]
        else:
            video: dict[str, Any] = (
                cast(dict[str, Any], payload["video"])
                if isinstance(payload.get("video"), dict)
                else {}
            )
            snippet: dict[str, Any] = (
                cast(dict[str, Any], video["snippet"])
                if isinstance(video.get("snippet"), dict)
                else {}
            )
            title = str(snippet.get("title") or payload.get("title") or label)
            body = f"**{label} from {row['channel_name']}**"
            url = (
                f"https://www.youtube.com/shorts/{item_id}"
                if event_type == "short"
                else f"https://www.youtube.com/watch?v={item_id}"
            )
            thumbnail = _best_thumbnail(snippet.get("thumbnails"))
            if thumbnail:
                images.append(thumbnail)
        safe_title = discord.utils.escape_markdown(title)
        children.append(
            discord.ui.TextDisplay(
                f"### [{safe_title}]({url})\n{discord.utils.escape_markdown(body)}"
            )
        )
        if images:
            children.append(
                discord.ui.MediaGallery(
                    *(discord.MediaGalleryItem(image) for image in images[:4])
                )
            )
        children.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Open on YouTube",
                    style=discord.ButtonStyle.link,
                    url=url,
                )
            )
        )
        view = type("YouTubeAnnouncementView", (discord.ui.LayoutView,), {})(
            timeout=None
        )
        view.add_item(discord.ui.Container(*children, accent_color=self.bot.embedcolor))
        try:
            await cast(Any, channel).send(
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    everyone=True, users=True, roles=True, replied_user=False
                ),
            )
        except Exception as error:
            self.bot.logger.warning(
                "Could not announce YouTube %s %s in guild %s: %s",
                event_type,
                item_id,
                row["guild_id"],
                error,
            )
            return False
        return True

    async def _fetch_community_posts(self, channel_id: str) -> list[dict[str, Any]]:
        try:
            async with self.bot.session.get(
                f"https://www.youtube.com/channel/{channel_id}/community",
                headers={"Accept-Language": "en-US,en;q=0.8"},
            ) as response:
                if response.status != 200:
                    return []
                document = await response.text()
        except Exception as error:
            self.bot.logger.warning(
                "Could not check YouTube community posts for %s: %s",
                channel_id,
                error,
            )
            return []
        return parse_youtube_community_posts(document)

    async def check_youtube_community_posts(self) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT DISTINCT youtube_channel_id FROM youtube_follows "
            "WHERE 'community' = ANY(event_types)"
        )
        for row in rows:
            channel_id = str(row["youtube_channel_id"])
            posts = await self._fetch_community_posts(channel_id)
            if not posts:
                await self.bot.pool.execute(
                    "INSERT INTO youtube_community_state "
                    "(youtube_channel_id, checked_at) VALUES ($1, now()) "
                    "ON CONFLICT (youtube_channel_id) DO UPDATE SET checked_at = now()",
                    channel_id,
                )
                continue
            previous = await self.bot.pool.fetchval(
                "SELECT latest_post_id FROM youtube_community_state "
                "WHERE youtube_channel_id = $1",
                channel_id,
            )
            newest = str(posts[0]["id"])
            if previous is None:
                await self._store_youtube_community_cursor(channel_id, newest)
                continue
            pending: list[dict[str, Any]] = []
            for post in posts[:10]:
                if str(post["id"]) == str(previous):
                    break
                pending.append(post)
            for post in reversed(pending):
                await self._dispatch_youtube_item(
                    channel_id,
                    str(post["id"]),
                    "community",
                    {"channel_id": channel_id, "post": post},
                )
            await self._store_youtube_community_cursor(channel_id, newest)
            await asyncio.sleep(0.25)

    async def _store_youtube_community_cursor(
        self,
        channel_id: str,
        post_id: str,
    ) -> None:
        await self.bot.pool.execute(
            "INSERT INTO youtube_community_state "
            "(youtube_channel_id, latest_post_id, checked_at) "
            "VALUES ($1, $2, now()) ON CONFLICT (youtube_channel_id) DO UPDATE "
            "SET latest_post_id = EXCLUDED.latest_post_id, checked_at = now()",
            channel_id,
            post_id,
        )

    async def sync_youtube_subscriptions(self) -> None:
        await self.bot.pool.execute(
            "DELETE FROM youtube_events "
            "WHERE (status = 'done' AND received_at < now() - interval '1 day') "
            "OR (status = 'dead' AND received_at < now() - interval '7 days')"
        )
        await self.bot.pool.execute(
            "DELETE FROM youtube_announcement_deliveries "
            "WHERE (status = 'done' AND updated_at < now() - interval '30 days') "
            "OR (status = 'dead' AND updated_at < now() - interval '30 days')"
        )
        channel_ids = await self.bot.pool.fetch(
            "SELECT DISTINCT youtube_channel_id FROM youtube_follows"
        )
        for row in channel_ids:
            await self.ensure_youtube_subscription(str(row["youtube_channel_id"]))
        orphaned = await self.bot.pool.fetch(
            "SELECT youtube_channel_id FROM youtube_websub_subscriptions "
            "WHERE NOT EXISTS (SELECT 1 FROM youtube_follows "
            "WHERE youtube_follows.youtube_channel_id = "
            "youtube_websub_subscriptions.youtube_channel_id)"
        )
        for row in orphaned:
            await self.remove_youtube_subscription(str(row["youtube_channel_id"]))


def _best_thumbnail(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("maxres", "standard", "high", "medium", "default"):
        item = value.get(key)
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
    return ""
