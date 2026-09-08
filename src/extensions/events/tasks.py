from __future__ import annotations

import asyncio
import base64
import datetime
import os
import shutil
import time
from datetime import timedelta
from typing import Any, Dict, cast

import aiohttp
import discord
from discord.ext import commands, tasks

from core import Cog, is_operational_guild
from core.handoff import is_legacy_instance
from utils.anilist import (
    anilist_notification_schedule,
    anilist_temporarily_unavailable,
)
from utils.downloads import is_discord_media_url
from utils.paths import DOWNLOADS_ROOT


def hourly_post_media_kind(filename: object) -> str:
    """Return the library media kind used by the hourly-post filter.

    Approved image uploads use the ``post_uploads`` table and are split into
    images and GIFs by their normalized filename.  Video uploads are kept in
    their own table and are never treated as GIFs, even when an old filename
    has an unexpected extension.
    """

    value = str(filename or "").casefold()
    return "gif" if value.rsplit("?", 1)[0].endswith(".gif") else "image"


def hourly_post_kind_enabled(
    kind: str, *, images: bool, gifs: bool, videos: bool
) -> bool:
    """Check whether a configured media kind is enabled for a guild."""

    return {
        "image": images,
        "gif": gifs,
        "video": videos,
    }.get(kind, False)


TWITCH_EVENTSUB_CALLBACK = "https://api.crygup.com/fishie/twitch/eventsub"


class Tasks(Cog):
    async def process_twitch_event_message(self, message_id: str | None = None) -> bool:
        """Claim and process one durable EventSub inbox row."""
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT message_id, payload, attempts
                    FROM twitch_eventsub_events
                    WHERE ($1::text IS NULL OR message_id = $1)
                      AND next_attempt_at <= now()
                      AND (status = 'pending' OR
                           (status = 'processing' AND updated_at < now() - interval '5 minutes'))
                    ORDER BY received_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    message_id,
                )
                if row is None:
                    return False
                await connection.execute(
                    "UPDATE twitch_eventsub_events SET status = 'processing', "
                    "attempts = attempts + 1, updated_at = now() WHERE message_id = $1",
                    row["message_id"],
                )

        try:
            await self.handle_twitch_event(dict(row["payload"]))
        except Exception as error:
            delay = min(300, 2 ** min(int(row["attempts"]), 8))
            status = "dead" if int(row["attempts"]) >= 9 else "pending"
            await self.bot.pool.execute(
                "UPDATE twitch_eventsub_events SET status = $2, "
                "next_attempt_at = now() + ($3 * interval '1 second'), "
                "updated_at = now(), last_error = $4 WHERE message_id = $1",
                row["message_id"],
                status,
                delay,
                str(error)[:1000],
            )
            self.bot.logger.exception(
                "Twitch EventSub event %s failed", row["message_id"]
            )
            return False

        await self.bot.pool.execute(
            "UPDATE twitch_eventsub_events SET status = 'done', updated_at = now(), "
            "last_error = NULL WHERE message_id = $1",
            row["message_id"],
        )
        return True

    def _twitch_eventsub_secret(self) -> str | None:
        keys = self.bot.config["keys"]
        secret = keys.get("twitch_eventsub_secret")
        if not secret:
            self.bot.logger.warning(
                "Twitch EventSub is disabled: twitch_eventsub_secret is not configured"
            )
            return None
        return secret

    async def _get_twitch_access_token(self) -> str | None:
        try:
            client_id = self.bot.config["keys"]["twitch_id"]
            client_secret = self.bot.config["keys"]["twitch_secret"]
        except KeyError:
            return None

        if (
            self._twitch_access_token
            and time.monotonic() < self._twitch_token_expires_at
        ):
            return self._twitch_access_token

        try:
            async with self.bot.session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
            ) as response:
                data = await response.json(content_type=None)
        except Exception as error:
            self.bot.logger.warning("Could not obtain Twitch access token: %s", error)
            return None

        token = data.get("access_token") if isinstance(data, dict) else None
        if response.status != 200 or not token:
            self.bot.logger.warning(
                "Twitch token request failed with status %s", response.status
            )
            return None

        expires_in = int(data.get("expires_in", 3600))
        self._twitch_access_token = token
        self._twitch_token_expires_at = time.monotonic() + max(expires_in - 60, 60)
        return token

    async def _get_twitch_user(self, channel_name: str) -> dict[str, Any] | None:
        token = await self._get_twitch_access_token()
        if not token:
            return None

        try:
            async with self.bot.session.get(
                "https://api.twitch.tv/helix/users",
                headers={
                    "Client-ID": self.bot.config["keys"]["twitch_id"],
                    "Authorization": f"Bearer {token}",
                },
                params={"login": channel_name},
            ) as response:
                data = await response.json(content_type=None)
        except Exception as error:
            self.bot.logger.warning(
                "Could not resolve Twitch channel %s: %s", channel_name, error
            )
            return None

        if response.status != 200 or not isinstance(data, dict):
            self.bot.logger.warning(
                "Twitch user lookup failed for %s with status %s",
                channel_name,
                response.status,
            )
            return None

        users = data.get("data")
        if not isinstance(users, list) or not users:
            return None
        user = users[0]
        return user if isinstance(user, dict) else None

    async def _get_twitch_stream(self, broadcaster_id: str) -> dict[str, Any] | None:
        token = await self._get_twitch_access_token()
        if not token:
            return None

        try:
            async with self.bot.session.get(
                "https://api.twitch.tv/helix/streams",
                headers={
                    "Client-ID": self.bot.config["keys"]["twitch_id"],
                    "Authorization": f"Bearer {token}",
                },
                params={"user_id": broadcaster_id},
            ) as response:
                data = await response.json(content_type=None)
        except Exception as error:
            self.bot.logger.warning(
                "Could not fetch Twitch stream details for %s: %s",
                broadcaster_id,
                error,
            )
            return None

        if response.status != 200 or not isinstance(data, dict):
            self.bot.logger.warning(
                "Twitch stream lookup failed for %s with status %s",
                broadcaster_id,
                response.status,
            )
            return None

        streams = data.get("data")
        if not isinstance(streams, list) or not streams:
            return None
        stream = streams[0]
        return stream if isinstance(stream, dict) else None

    async def ensure_twitch_eventsub_subscription(self, broadcaster_id: str) -> bool:
        secret = self._twitch_eventsub_secret()
        if not secret:
            return False

        token = await self._get_twitch_access_token()
        if not token:
            return False

        claimed = await self.bot.pool.fetchval(
            """
            INSERT INTO twitch_eventsub_subscriptions
                (broadcaster_id, subscription_id, status, updated_at)
            VALUES ($1, '', 'creating', now())
            ON CONFLICT (broadcaster_id) DO UPDATE
            SET subscription_id = '', status = 'creating', updated_at = now()
            WHERE twitch_eventsub_subscriptions.status NOT IN
                    ('enabled', 'webhook_callback_verification_pending', 'creating')
               OR twitch_eventsub_subscriptions.updated_at < now() - interval '15 minutes'
            RETURNING broadcaster_id
            """,
            broadcaster_id,
        )
        if claimed is None:
            return True

        # The database can lose the remote ID during a restart or hand-off.
        # Reconcile first so an already-enabled Twitch subscription is adopted
        # instead of being submitted a second time and reported as a 409.
        existing = await self._find_twitch_eventsub_subscription(token, broadcaster_id)
        if existing is not None:
            await self.bot.pool.execute(
                "UPDATE twitch_eventsub_subscriptions "
                "SET subscription_id = $2, status = $3, updated_at = now() "
                "WHERE broadcaster_id = $1",
                broadcaster_id,
                existing["id"],
                existing.get("status", "enabled"),
            )
            return True

        payload = {
            "type": "stream.online",
            "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id},
            "transport": {
                "method": "webhook",
                "callback": TWITCH_EVENTSUB_CALLBACK,
                "secret": secret,
            },
        }
        try:
            async with self.bot.session.post(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                headers={
                    "Client-ID": self.bot.config["keys"]["twitch_id"],
                    "Authorization": f"Bearer {token}",
                },
                json=payload,
            ) as response:
                data = await response.json(content_type=None)
        except Exception as error:
            await self.bot.pool.execute(
                "UPDATE twitch_eventsub_subscriptions SET status = 'failed', "
                "updated_at = now() WHERE broadcaster_id = $1",
                broadcaster_id,
            )
            self.bot.logger.warning(
                "Could not create Twitch EventSub subscription for %s: %s",
                broadcaster_id,
                error,
            )
            return False

        subscriptions = data.get("data") if isinstance(data, dict) else None
        if response.status not in (200, 202) or not subscriptions:
            if response.status == 409:
                existing = await self._find_twitch_eventsub_subscription(
                    token, broadcaster_id
                )
                if existing is not None:
                    await self.bot.pool.execute(
                        "UPDATE twitch_eventsub_subscriptions "
                        "SET subscription_id = $2, status = $3, updated_at = now() "
                        "WHERE broadcaster_id = $1",
                        broadcaster_id,
                        existing["id"],
                        existing.get("status", "enabled"),
                    )
                    return True
            await self.bot.pool.execute(
                "UPDATE twitch_eventsub_subscriptions SET status = 'failed', "
                "updated_at = now() WHERE broadcaster_id = $1",
                broadcaster_id,
            )
            self.bot.logger.warning(
                "Twitch EventSub subscription failed for %s with status %s: %s",
                broadcaster_id,
                response.status,
                data,
            )
            return False

        subscription = subscriptions[0]
        if not isinstance(subscription, dict):
            return False
        await self.bot.pool.execute(
            "UPDATE twitch_eventsub_subscriptions SET subscription_id = $2, "
            "status = $3, updated_at = now() WHERE broadcaster_id = $1",
            broadcaster_id,
            subscription["id"],
            subscription.get("status", "enabled"),
        )
        return True

    async def _find_twitch_eventsub_subscription(
        self, token: str, broadcaster_id: str
    ) -> dict[str, Any] | None:
        """Find an existing stream.online webhook subscription for a channel."""

        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"type": "stream.online", "first": 100}
            if cursor:
                params["after"] = cursor
            try:
                async with self.bot.session.get(
                    "https://api.twitch.tv/helix/eventsub/subscriptions",
                    headers={
                        "Client-ID": self.bot.config["keys"]["twitch_id"],
                        "Authorization": f"Bearer {token}",
                    },
                    params=params,
                ) as response:
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return None
            if response.status != 200 or not isinstance(data, dict):
                return None
            for subscription in data.get("data", []):
                if not isinstance(subscription, dict) or not subscription.get("id"):
                    continue
                transport = subscription.get("transport")
                condition = subscription.get("condition")
                if not isinstance(transport, dict) or not isinstance(condition, dict):
                    continue
                if (
                    subscription.get("type") == "stream.online"
                    and subscription.get("version") == "1"
                    and condition.get("broadcaster_user_id") == broadcaster_id
                    and transport.get("method") == "webhook"
                    and transport.get("callback") == TWITCH_EVENTSUB_CALLBACK
                ):
                    if subscription.get("status") in {
                        "enabled", "webhook_callback_verification_pending"
                    }:
                        return subscription
                    # A revoked/failed subscription cannot deliver events and
                    # can conflict with its replacement until it is removed.
                    try:
                        async with self.bot.session.delete(
                            "https://api.twitch.tv/helix/eventsub/subscriptions",
                            headers={
                                "Client-ID": self.bot.config["keys"]["twitch_id"],
                                "Authorization": f"Bearer {token}",
                            },
                            params={"id": subscription["id"]},
                        ) as deletion:
                            if deletion.status not in (204, 404):
                                return None
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        return None
            pagination = data.get("pagination")
            cursor = (
                str(pagination.get("cursor"))
                if isinstance(pagination, dict) and pagination.get("cursor")
                else None
            )
            if not cursor:
                break
        return None

    async def sync_twitch_eventsub_subscriptions(self) -> None:
        await self.bot.pool.execute(
            "DELETE FROM twitch_eventsub_events "
            "WHERE (status = 'done' AND received_at < now() - interval '1 day') "
            "OR (status = 'dead' AND received_at < now() - interval '7 days')"
        )
        await self.bot.pool.execute(
            "DELETE FROM twitch_announcement_deliveries "
            "WHERE status = 'dead' AND updated_at < now() - interval '30 days'"
        )
        rows = await self.bot.pool.fetch("""
            SELECT DISTINCT channel_name, broadcaster_id FROM twitch_follows
            UNION
            SELECT DISTINCT channel_name, broadcaster_id
            FROM notify_twitch_follows
            WHERE broadcaster_id IS NOT NULL
            """)
        known_ids: set[str] = set()
        for row in rows:
            broadcaster_id = row["broadcaster_id"]
            if not broadcaster_id:
                user = await self._get_twitch_user(row["channel_name"])
                if not user or not user.get("id"):
                    self.bot.logger.warning(
                        "Could not resolve followed Twitch channel %s",
                        row["channel_name"],
                    )
                    continue
                broadcaster_id = str(user["id"])
                await self.bot.pool.execute(
                    "UPDATE twitch_follows SET broadcaster_id = $2 "
                    "WHERE channel_name = $1 AND broadcaster_id IS NULL",
                    row["channel_name"],
                    broadcaster_id,
                )
                # Notify follows are stored separately from the legacy
                # Twitch table.  Update those rows too, otherwise an
                # unresolved ``notify`` follow would be looked up on every
                # sync and never receive an EventSub subscription.
                await self.bot.pool.execute(
                    "UPDATE notify_twitch_follows SET broadcaster_id = $2, "
                    "updated_at = now() "
                    "WHERE lower(channel_name) = lower($1) "
                    "AND broadcaster_id IS NULL",
                    row["channel_name"],
                    broadcaster_id,
                )
            broadcaster_id = str(broadcaster_id)
            if broadcaster_id in known_ids:
                continue
            known_ids.add(broadcaster_id)
            await self.ensure_twitch_eventsub_subscription(broadcaster_id)

        # Retry deleting subscriptions whose guild follows were removed while
        # Twitch authentication was unavailable.
        orphaned = await self.bot.pool.fetch(
            "SELECT broadcaster_id FROM twitch_eventsub_subscriptions "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM twitch_follows "
            "WHERE twitch_follows.broadcaster_id = "
            "twitch_eventsub_subscriptions.broadcaster_id"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM notify_twitch_follows "
            "WHERE notify_twitch_follows.broadcaster_id = "
            "twitch_eventsub_subscriptions.broadcaster_id"
            ")"
        )
        for row in orphaned:
            await self.remove_twitch_eventsub_subscription(str(row["broadcaster_id"]))

    async def remove_twitch_eventsub_subscription(self, broadcaster_id: str) -> None:
        remaining = await self.bot.pool.fetchval(
            "SELECT 1 FROM twitch_follows WHERE broadcaster_id = $1 "
            "UNION ALL SELECT 1 FROM notify_twitch_follows "
            "WHERE broadcaster_id = $1 LIMIT 1",
            broadcaster_id,
        )
        if remaining:
            return

        subscription = await self.bot.pool.fetchrow(
            "SELECT subscription_id FROM twitch_eventsub_subscriptions "
            "WHERE broadcaster_id = $1",
            broadcaster_id,
        )
        if not subscription:
            return

        # A failed creation can leave a local row without a Twitch ID.  There
        # is nothing to delete remotely in that state; remove the stale row so
        # reconciliation can stop retrying an invalid DELETE request.
        subscription_id = subscription["subscription_id"]
        if not subscription_id:
            await self.bot.pool.execute(
                "DELETE FROM twitch_eventsub_subscriptions WHERE broadcaster_id = $1",
                broadcaster_id,
            )
            return

        token = await self._get_twitch_access_token()
        if not token:
            self.bot.logger.warning(
                "Keeping Twitch EventSub subscription %s for retry because "
                "Twitch authentication is unavailable",
                broadcaster_id,
            )
            return

        try:
            async with self.bot.session.delete(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                headers={
                    "Client-ID": self.bot.config["keys"]["twitch_id"],
                    "Authorization": f"Bearer {token}",
                },
                params={"id": subscription_id},
            ) as response:
                if response.status not in (204, 404):
                    self.bot.logger.warning(
                        "Could not remove Twitch EventSub subscription for %s: status %s",
                        broadcaster_id,
                        response.status,
                    )
                    return
        except Exception as error:
            self.bot.logger.warning(
                "Could not remove Twitch EventSub subscription for %s: %s",
                broadcaster_id,
                error,
            )
            return

        await self.bot.pool.execute(
            "DELETE FROM twitch_eventsub_subscriptions WHERE broadcaster_id = $1",
            broadcaster_id,
        )

    async def handle_twitch_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        broadcaster_id = str(event.get("broadcaster_user_id", ""))
        self.bot.logger.info(
            "Processing Twitch EventSub event type=%s broadcaster=%s",
            event_type or "unknown",
            broadcaster_id or "unknown",
        )
        if not broadcaster_id:
            return

        if event_type == "stream.offline":
            await self.bot.pool.execute(
                "UPDATE twitch_follows SET last_stream_id = NULL "
                "WHERE broadcaster_id = $1",
                broadcaster_id,
            )
            await self.bot.pool.execute(
                "UPDATE notify_twitch_follows SET last_stream_id = NULL, "
                "last_offline_at = now(), updated_at = now() "
                "WHERE broadcaster_id = $1",
                broadcaster_id,
            )
            return
        if event_type != "stream.online":
            return

        rows = await self.bot.pool.fetch(
            "SELECT guild_id, channel_name, announce_channel_id, message_template, "
            "last_stream_id FROM twitch_follows WHERE broadcaster_id = $1",
            broadcaster_id,
        )
        rows = [row for row in rows if not is_operational_guild(row.get("guild_id"))]
        notify_rows = await self.bot.pool.fetch(
            """
            SELECT n.*
            FROM notify_twitch_follows AS n
            WHERE n.broadcaster_id = $1
              AND NOT EXISTS (
                  SELECT 1 FROM twitch_follows AS legacy
                  WHERE n.guild_id IS NOT NULL
                    AND legacy.guild_id = n.guild_id
                    AND lower(legacy.channel_name) = lower(n.channel_name)
                    AND COALESCE(legacy.announce_channel_id, 0)
                        = COALESCE(n.announce_channel_id, 0)
              )
            """,
            broadcaster_id,
        )
        notify_rows = [
            row for row in notify_rows if not is_operational_guild(row.get("guild_id"))
        ]
        if not rows and not notify_rows:
            self.bot.logger.warning(
                "No Twitch follow matched broadcaster %s", broadcaster_id
            )
            return

        stream = await self._get_twitch_stream(broadcaster_id) or {
            "id": event.get("id"),
            "user_id": broadcaster_id,
            "user_login": event.get("broadcaster_user_login"),
            "user_name": event.get("broadcaster_user_name"),
            "type": event.get("type"),
            "started_at": event.get("started_at"),
        }
        stream["id"] = str(event.get("id") or stream.get("id"))
        for row in rows:
            await self._announce_twitch_stream_once(row, stream)
        for row in notify_rows:
            stream_id = str(stream.get("id") or "")
            if not stream_id or not await self._claim_notify_twitch(row, stream_id):
                continue
            if await self._announce_notify_twitch(row, stream):
                await self._finish_notify_twitch(row, stream_id)
            else:
                await self._release_notify_twitch(row, stream_id)

    async def _announce_twitch_stream_once(
        self, row: Any, stream: dict[str, Any]
    ) -> bool:
        """Durably claim a stream notification without holding a DB lock on I/O."""
        stream_id = str(stream.get("id") or "")
        if not stream_id:
            self.bot.logger.warning(
                "Twitch stream event for %s did not include a stream ID",
                row["channel_name"],
            )
            return False

        claimed = await self.bot.pool.fetchval(
            """
            INSERT INTO twitch_announcement_deliveries
                (guild_id, channel_name, stream_id, stream_payload, status, attempts)
            VALUES ($1, $2, $3, $4::jsonb, 'processing', 1)
            ON CONFLICT (guild_id, channel_name, stream_id) DO UPDATE
            SET status = 'processing', attempts = twitch_announcement_deliveries.attempts + 1,
                stream_payload = EXCLUDED.stream_payload, updated_at = now()
            WHERE (twitch_announcement_deliveries.status = 'pending'
                   AND twitch_announcement_deliveries.attempts < 10)
               OR (twitch_announcement_deliveries.status = 'processing'
                   AND twitch_announcement_deliveries.updated_at < now() - interval '5 minutes'
                   AND twitch_announcement_deliveries.attempts < 10)
            RETURNING stream_id
            """,
            row["guild_id"],
            row["channel_name"],
            stream_id,
            stream,
        )
        if claimed is None:
            return False

        announced = await self._announce_twitch_stream(row, stream)
        if announced:
            async with self.bot.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE twitch_announcement_deliveries SET status = 'done', "
                        "updated_at = now(), last_error = NULL "
                        "WHERE guild_id = $1 AND channel_name = $2 AND stream_id = $3",
                        row["guild_id"],
                        row["channel_name"],
                        stream_id,
                    )
                    await connection.execute(
                        "UPDATE twitch_follows SET last_stream_id = $3 "
                        "WHERE guild_id = $1 AND channel_name = $2",
                        row["guild_id"],
                        row["channel_name"],
                        stream_id,
                    )
                    await connection.execute(
                        "UPDATE notify_twitch_follows SET last_stream_id = $3, "
                        "last_live_at = now(), updated_at = now() "
                        "WHERE guild_id = $1 AND lower(channel_name) = lower($2) "
                        "AND COALESCE(announce_channel_id, 0::bigint) = "
                        "COALESCE($4::bigint, 0::bigint)",
                        row["guild_id"],
                        row["channel_name"],
                        stream_id,
                        row["announce_channel_id"],
                    )
            return True

        await self.bot.pool.execute(
            "UPDATE twitch_announcement_deliveries "
            "SET status = CASE WHEN attempts >= 10 THEN 'dead' ELSE 'pending' END, "
            "updated_at = now(), last_error = 'Discord delivery failed' "
            "WHERE guild_id = $1 AND channel_name = $2 AND stream_id = $3",
            row["guild_id"],
            row["channel_name"],
            stream_id,
        )
        return False

    async def _announce_twitch_stream(self, row, stream: dict[str, Any]) -> bool:
        from extensions.settings.notify import mention_text, notify_allowed_mentions

        channel = self.bot.get_channel(row["announce_channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(row["announce_channel_id"])
            except Exception as error:
                self.bot.logger.warning(
                    "Could not find Twitch announcement channel %s: %s",
                    row["announce_channel_id"],
                    error,
                )
                return False

        login = str(stream.get("user_login", row["channel_name"]))
        display_name = str(stream.get("user_name", login))
        title = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(stream.get("title") or ""))
        )
        game = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(stream.get("game_name") or ""))
        )
        url = f"https://www.twitch.tv/{login}"
        channel_info = await self._get_twitch_user(login) or {}
        media_urls: list[str] = []
        for media_url in (
            channel_info.get("profile_image_url"),
            stream.get("thumbnail_url"),
            channel_info.get("offline_image_url"),
        ):
            if not media_url:
                continue
            media_url = (
                str(media_url).replace("{width}", "1280").replace("{height}", "720")
            )
            if media_url not in media_urls:
                media_urls.append(media_url)
                break

        safe_display_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(display_name)
        )
        safe_title = title or f"{safe_display_name} is live on Twitch!"
        stream_text = f"### [{safe_title}](https://www.twitch.tv/{login})"
        if game:
            stream_text += f"\n**Playing:** {game}"

        children: list[discord.ui.Item] = [discord.ui.TextDisplay(stream_text)]
        if media_urls:
            children.append(
                discord.ui.MediaGallery(
                    *(discord.MediaGalleryItem(url) for url in media_urls)
                )
            )
        children.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Watch on Twitch",
                    style=discord.ButtonStyle.link,
                    url=url,
                )
            )
        )

        # ``notify mention`` stores the mention separately from the legacy
        # Twitch follow row.  Read it at delivery time so changes take effect
        # without restarting the worker.
        mention_role_id: object = None
        mention_everyone = False
        try:
            mention_row = await self.bot.pool.fetchrow(
                "SELECT mention_role_id, mention_everyone FROM notify_twitch_follows "
                "WHERE guild_id = $1 AND lower(channel_name) = lower($2) "
                "AND COALESCE(announce_channel_id, 0::bigint) = "
                "COALESCE($3::bigint, 0::bigint) "
                "ORDER BY id LIMIT 1",
                row["guild_id"],
                row["channel_name"],
                row["announce_channel_id"],
            )
        except Exception:
            mention_row = None
        if mention_row:
            mention_role_id = mention_row.get("mention_role_id")
            mention_everyone = bool(mention_row.get("mention_everyone"))
        mention = mention_text(mention_role_id, mention_everyone)
        if mention:
            children.extend(
                (discord.ui.Separator(), discord.ui.TextDisplay(f"-# {mention}"))
            )

        container = discord.ui.Container(*children, accent_color=self.bot.embedcolor)
        view_type = type("TwitchAnnouncementView", (discord.ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        send_kwargs: dict[str, Any] = {
            "view": view,
            "allowed_mentions": notify_allowed_mentions(
                mention_role_id, mention_everyone
            ),
        }

        try:
            await cast(Any, channel).send(**send_kwargs)
        except Exception as error:
            self.bot.logger.warning(
                "Could not announce Twitch stream %s in guild %s: %s",
                login,
                row["guild_id"],
                error,
            )
            return False
        self.bot.logger.info(
            "Announced Twitch stream %s in guild %s", login, row["guild_id"]
        )
        return True

    async def check_twitch_streams(self) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT guild_id, channel_name, announce_channel_id, message_template, "
            "last_stream_id "
            "FROM twitch_follows"
        )
        rows = [row for row in rows if not is_operational_guild(row.get("guild_id"))]
        if not rows:
            return

        token = await self._get_twitch_access_token()
        if not token:
            return

        headers = {
            "Client-ID": self.bot.config["keys"]["twitch_id"],
            "Authorization": f"Bearer {token}",
        }
        live_streams: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(rows), 100):
            batch = rows[offset : offset + 100]
            params = [("user_login", row["channel_name"]) for row in batch]
            try:
                async with self.bot.session.get(
                    "https://api.twitch.tv/helix/streams",
                    headers=headers,
                    params=params,
                ) as response:
                    if response.status == 401:
                        self._twitch_access_token = None
                        self._twitch_token_expires_at = 0
                        return
                    data = await response.json(content_type=None)
            except Exception as error:
                self.bot.logger.warning("Could not check Twitch streams: %s", error)
                return

            if response.status != 200 or not isinstance(data, dict):
                self.bot.logger.warning(
                    "Twitch streams request failed with status %s", response.status
                )
                return

            streams = data.get("data")
            if not isinstance(streams, list):
                self.bot.logger.warning(
                    "Twitch streams response did not contain a data list"
                )
                return

            live_streams.update(
                {
                    str(stream.get("user_login", "")).lower(): stream
                    for stream in streams
                    if isinstance(stream, dict) and stream.get("user_login")
                }
            )
        for row in rows:
            stream = live_streams.get(row["channel_name"])
            if stream is None:
                if row["last_stream_id"] is not None:
                    await self.bot.pool.execute(
                        "UPDATE twitch_follows SET last_stream_id = NULL "
                        "WHERE guild_id = $1 AND channel_name = $2",
                        row["guild_id"],
                        row["channel_name"],
                    )
                continue

            await self._announce_twitch_stream_once(row, stream)

    async def _notify_destination(self, row: Any) -> Any | None:
        """Resolve a notify row's channel or DM destination."""

        channel_id = row.get("announce_channel_id")
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except Exception as error:
                    self.bot.logger.warning(
                        "Could not resolve notify channel %s: %s", channel_id, error
                    )
            return channel

        user_id = row.get("user_id")
        if not user_id:
            return None
        user = self.bot.get_user(int(user_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(user_id))
            except Exception as error:
                self.bot.logger.warning(
                    "Could not resolve notify DM user %s: %s", user_id, error
                )
        return user

    async def _announce_notify_twitch(self, row: Any, stream: dict[str, Any]) -> bool:
        """Deliver one Twitch notification created by ``notify``."""

        from extensions.settings.notify import (
            NotifyView,
            mention_text,
            notify_allowed_mentions,
        )

        destination = await self._notify_destination(row)
        if destination is None:
            return False
        login = str(stream.get("user_login") or row.get("channel_name") or "")
        display_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(stream.get("user_name") or login))
        )
        title = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(stream.get("title") or ""))
        )
        game = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(stream.get("game_name") or ""))
        )
        viewers = stream.get("viewer_count")
        try:
            viewer_text = (
                f"{int(viewers):,} viewers" if viewers is not None else "Live now"
            )
        except (TypeError, ValueError):
            viewer_text = "Live now"
        twitch_url = f"https://www.twitch.tv/{login}" if login else "https://twitch.tv"
        details = f"### [{display_name}]({twitch_url}) is live!"
        if title:
            details += f"\n**{title}**"
        if game:
            details += f"\nPlaying: {game}"
        details += f"\n👀 {viewer_text}"

        channel_info = await self._get_twitch_user(login) or {}
        image = stream.get("thumbnail_url") or channel_info.get("profile_image_url")
        if image:
            image = str(image).replace("{width}", "1280").replace("{height}", "720")
        mention = mention_text(row.get("mention_role_id"), row.get("mention_everyone"))
        view = NotifyView(
            "## Twitch live",
            details,
            image=image,
            links=[("Watch on Twitch", twitch_url)],
            mention=mention,
        )
        try:
            await destination.send(
                view=view,
                allowed_mentions=notify_allowed_mentions(
                    row.get("mention_role_id"), row.get("mention_everyone")
                ),
            )
        except Exception as error:
            self.bot.logger.warning(
                "Could not deliver Twitch notification for %s: %s", login, error
            )
            return False
        return True

    async def _claim_notify_twitch(self, row: Any, stream_id: str) -> bool:
        """Claim a stream notification before sending it.

        EventSub delivery and the polling fallback run concurrently.  An
        atomic claim prevents both workers from announcing the same stream
        when they observe it at the same time.
        """

        claimed = await self.bot.pool.fetchval(
            """
            UPDATE notify_twitch_follows
            SET last_stream_id = $2, updated_at = now()
            WHERE id = $1
              AND (last_stream_id IS NULL OR last_stream_id <> $2)
            RETURNING id
            """,
            row["id"],
            stream_id,
        )
        return claimed is not None

    async def _finish_notify_twitch(self, row: Any, stream_id: str) -> None:
        await self.bot.pool.execute(
            "UPDATE notify_twitch_follows SET last_live_at = now(), updated_at = now() "
            "WHERE id = $1 AND last_stream_id = $2",
            row["id"],
            stream_id,
        )

    async def _release_notify_twitch(self, row: Any, stream_id: str) -> None:
        """Release a failed claim so a later poll/event can retry delivery."""

        await self.bot.pool.execute(
            "UPDATE notify_twitch_follows SET last_stream_id = NULL, updated_at = now() "
            "WHERE id = $1 AND last_stream_id = $2",
            row["id"],
            stream_id,
        )

    async def check_notify_twitch(self) -> None:
        """Poll Twitch follows created by ``notify``.

        Legacy ``twitch follow`` rows are deliberately excluded because the
        existing EventSub/reconciliation worker already announces them.  The
        command layer mirrors legacy rows into the new table, so this avoids
        duplicate messages while allowing DM and multi-destination follows.
        """

        rows = await self.bot.pool.fetch("""
            SELECT n.*
            FROM notify_twitch_follows AS n
            WHERE NOT EXISTS (
                SELECT 1 FROM twitch_follows AS legacy
                WHERE n.guild_id IS NOT NULL
                  AND legacy.guild_id = n.guild_id
                  AND lower(legacy.channel_name) = lower(n.channel_name)
                  AND COALESCE(legacy.announce_channel_id, 0)
                      = COALESCE(n.announce_channel_id, 0)
            )
            """)
        rows = [row for row in rows if not is_operational_guild(row.get("guild_id"))]
        if not rows:
            return
        token = await self._get_twitch_access_token()
        if not token:
            return
        headers = {
            "Client-ID": self.bot.config["keys"]["twitch_id"],
            "Authorization": f"Bearer {token}",
        }
        ids_by_row: dict[int, str] = {}
        for index, row in enumerate(rows):
            broadcaster_id = row.get("broadcaster_id")
            if not broadcaster_id:
                user = await self._get_twitch_user(str(row["channel_name"]))
                if not user or not user.get("id"):
                    continue
                broadcaster_id = str(user["id"])
                await self.bot.pool.execute(
                    "UPDATE notify_twitch_follows SET broadcaster_id = $2, updated_at = now() WHERE id = $1",
                    row["id"],
                    broadcaster_id,
                )
            ids_by_row[index] = str(broadcaster_id)
        if not ids_by_row:
            return
        live_streams: dict[str, dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(ids_by_row.values()))
        for offset in range(0, len(unique_ids), 100):
            params = [("user_id", value) for value in unique_ids[offset : offset + 100]]
            try:
                async with self.bot.session.get(
                    "https://api.twitch.tv/helix/streams",
                    headers=headers,
                    params=params,
                ) as response:
                    payload = await response.json(content_type=None)
            except Exception as error:
                self.bot.logger.warning(
                    "Could not poll notify Twitch follows: %s", error
                )
                return
            if response.status == 401:
                self._twitch_access_token = None
                self._twitch_token_expires_at = 0
                return
            if response.status != 200 or not isinstance(payload, dict):
                self.bot.logger.warning(
                    "Notify Twitch streams request failed with status %s",
                    response.status,
                )
                return
            for stream in payload.get("data", []):
                if isinstance(stream, dict) and stream.get("user_id"):
                    live_streams[str(stream["user_id"])] = stream

        for index, row in enumerate(rows):
            broadcaster_id = ids_by_row.get(index)
            if not broadcaster_id:
                continue
            stream = live_streams.get(broadcaster_id)
            if stream is None:
                if row.get("last_stream_id") is not None:
                    await self.bot.pool.execute(
                        "UPDATE notify_twitch_follows SET last_stream_id = NULL, last_offline_at = now(), updated_at = now() WHERE id = $1",
                        row["id"],
                    )
                continue
            stream_id = str(stream.get("id") or "")
            if not stream_id or not await self._claim_notify_twitch(row, stream_id):
                continue
            if await self._announce_notify_twitch(row, stream):
                await self._finish_notify_twitch(row, stream_id)
            else:
                await self._release_notify_twitch(row, stream_id)

    async def _anilist_media_batch(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        from extensions.settings.notify import ANILIST_NOTIFY_BATCH_QUERY

        if not ids:
            return {}
        result: dict[int, dict[str, Any]] = {}
        # AniList limits a Page response to 50 entries.  Split larger sets so
        # every stale follow is refreshed once per day rather than leaving
        # rows beyond the first page to retry on every scheduler tick.
        for offset in range(0, len(ids), 50):
            batch = ids[offset : offset + 50]
            try:
                async with self.bot.session.post(
                    "https://graphql.anilist.co",
                    json={
                        "query": ANILIST_NOTIFY_BATCH_QUERY,
                        "variables": {"ids": batch},
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    payload = await response.json(content_type=None)
            except Exception as error:
                self.bot.logger.warning(
                    "Could not refresh notify anime data (batch %s-%s): %s",
                    offset + 1,
                    offset + len(batch),
                    error,
                )
                continue
            if anilist_temporarily_unavailable(response.status, payload):
                retry_at = getattr(self, "_anilist_outage_retry_at", 0.0)
                if time.monotonic() >= retry_at:
                    self.bot.logger.warning(
                        "AniList is temporarily unavailable; keeping cached schedules"
                    )
                    self._anilist_outage_retry_at = time.monotonic() + 600
                continue
            if response.status != 200 or not isinstance(payload, dict):
                self.bot.logger.warning(
                    "AniList notify refresh failed with status %s for batch %s-%s",
                    response.status,
                    offset + 1,
                    offset + len(batch),
                )
                continue
            data = payload.get("data")
            page = data.get("Page") if isinstance(data, dict) else None
            media = page.get("media") if isinstance(page, dict) else None
            result.update(
                {
                    int(item["id"]): item
                    for item in (media or [])
                    if isinstance(item, dict) and item.get("id")
                }
            )
        return result

    async def _announce_notify_anime(self, row: Any) -> bool:
        from extensions.settings.notify import (
            NotifyView,
            media_title,
            mention_text,
            notify_allowed_mentions,
        )

        destination = await self._notify_destination(row)
        if destination is None:
            return False
        title = media_title({"title": {"userPreferred": row.get("title")}})
        airing = row.get("next_airing_at")
        episode = row.get("next_episode")
        if airing is not None:
            timestamp = int(airing.timestamp()) if hasattr(airing, "timestamp") else 0
            timing = (
                f"Episode {episode} airs <t:{timestamp}:R>"
                if episode
                else f"A new episode airs <t:{timestamp}:R>"
            )
        else:
            release = row.get("release_at")
            timestamp = int(release.timestamp()) if hasattr(release, "timestamp") else 0
            timing = (
                f"Released <t:{timestamp}:R>"
                if timestamp
                else "A new release is available."
            )
        links = [("AniList", str(row.get("site_url") or ""))]
        for label, key in (
            ("Official site", "official_site_url"),
            ("Crunchyroll", "crunchyroll_url"),
        ):
            value = row.get(key)
            if value:
                links.append((label, str(value)))
        mention = mention_text(row.get("mention_role_id"), row.get("mention_everyone"))
        view = NotifyView(
            "## Anime notification",
            f"### {discord.utils.escape_mentions(discord.utils.escape_markdown(title))}\n{timing}",
            image=row.get("banner_url"),
            links=links,
            mention=mention,
        )
        try:
            await destination.send(
                view=view,
                allowed_mentions=notify_allowed_mentions(
                    row.get("mention_role_id"), row.get("mention_everyone")
                ),
            )
        except Exception as error:
            self.bot.logger.warning(
                "Could not deliver anime notification for %s: %s", title, error
            )
            return False
        return True

    async def check_notify_anime(self) -> None:
        """Refresh AniList follows daily and deliver due episodes/releases."""

        from extensions.settings.notify import media_external_link, media_title

        rows = await self.bot.pool.fetch("SELECT * FROM notify_anime_follows")
        rows = [row for row in rows if not is_operational_guild(row.get("guild_id"))]
        if not rows:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_ids = {
            int(row["anilist_id"])
            for row in rows
            if row.get("last_checked_at") is None
            or row["last_checked_at"] < now - datetime.timedelta(days=1)
        }
        media_map = await self._anilist_media_batch(sorted(stale_ids))
        for media_id, media in media_map.items():
            # Keep the same status-aware schedule rules used while adding a
            # follow: finished/cancelled entries are cleared instead of
            # generating stale release notifications.
            airing, episode, release = anilist_notification_schedule(media, now)
            links = media.get("externalLinks") or []
            crunchyroll = next(
                (
                    str(link.get("url"))
                    for link in links
                    if isinstance(link, dict)
                    and str(link.get("site") or "").casefold().startswith("crunchyroll")
                    and str(link.get("url") or "").startswith(("http://", "https://"))
                ),
                None,
            )
            await self.bot.pool.execute(
                """
                UPDATE notify_anime_follows
                SET title = $2, site_url = $3, banner_url = $4,
                    official_site_url = $5, crunchyroll_url = $6,
                    release_at = $7, next_airing_at = $8, next_episode = $9,
                    last_checked_at = now(), updated_at = now()
                WHERE anilist_id = $1
                """,
                media_id,
                media_title(media),
                media.get("siteUrl"),
                media.get("bannerImage")
                or (media.get("coverImage") or {}).get("extraLarge"),
                media_external_link(media, "Official Site"),
                crunchyroll,
                release,
                airing,
                episode,
            )

        rows = await self.bot.pool.fetch("SELECT * FROM notify_anime_follows")
        rows = [row for row in rows if not is_operational_guild(row.get("guild_id"))]
        for row in rows:
            due_airing = row.get("next_airing_at")
            due_release = row.get("release_at")
            if due_airing is None and due_release is not None:
                # A start date that predates the follow is informational, not
                # a release reminder.  Dates retained from before the follow
                # are therefore never emitted as a fresh notification.
                created_at = row.get("created_at")
                if created_at is not None and due_release < created_at:
                    due_release = None
            due_at = due_airing or due_release
            if due_at is None or due_at > now:
                continue
            last_notified = row.get("last_notified_at")
            if last_notified is not None and last_notified >= due_at:
                continue
            if await self._announce_notify_anime(row):
                await self.bot.pool.execute(
                    "UPDATE notify_anime_follows SET last_notified_at = now(), updated_at = now() WHERE id = $1",
                    row["id"],
                )

    async def set_spotify_key(self):
        url = "https://accounts.spotify.com/api/token"
        sid = self.bot.config["keys"]["spotify_id"]
        ss = self.bot.config["keys"]["spotify_secret"]

        encoded_key = base64.b64encode(f"{sid}:{ss}".encode("ascii")).decode("ascii")

        headers = {
            "Authorization": f"Basic {encoded_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "client_credentials",
        }

        async with self.bot.session.post(url, headers=headers, data=data) as r:
            results: Dict[Any, Any] = await r.json()
            if results.get("access_token"):
                self.bot.spotify_key = results["access_token"]
                return

            raise commands.BadArgument("Unable to set spotify key.")

    def delete_videos(self):
        # Normal jobs clean themselves in ``Downloader.download``. Remove only
        # crash-orphaned job directories old enough that no valid job can still
        # be running (the hard job timeout is ten minutes).
        root = DOWNLOADS_ROOT
        if not os.path.isdir(root):
            return
        cutoff = time.time() - 3600
        for entry in os.scandir(root):
            if not entry.name.startswith(".job-") or not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                continue

    async def _hourly_post_media(
        self, settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Select one approved library item for an hourly-post configuration.

        Selection happens in PostgreSQL so a guild with a large library does
        not require loading every URL into the event loop.  The uploader block
        list is applied in the query, which also means a blocked uploader can
        never leak through a retry or an empty-media fallback.
        """

        blocked = [int(value) for value in settings.get("blocked_users", ())]
        images = bool(settings.get("images", True))
        gifs = bool(settings.get("gifs", True))
        videos = bool(settings.get("videos", True))
        if not any((images, gifs, videos)):
            return None

        row = await self.bot.pool.fetchrow(
            """
            SELECT item.*
            FROM (
                SELECT id, library_id, source_url, filename,
                       review_message_id, uploader_id, 'post'::text AS media_kind
                FROM post_uploads
                WHERE status = 'approved'
                  AND library_id IS NOT NULL
                  AND uploader_id <> ALL($1::BIGINT[])
                  AND (
                        ($2::boolean AND lower(filename) NOT LIKE '%%.gif')
                     OR ($3::boolean AND lower(filename) LIKE '%%.gif')
                  )
                UNION ALL
                SELECT id, library_id, source_url, filename,
                       review_message_id, uploader_id, 'video'::text AS media_kind
                FROM video_uploads
                WHERE status = 'approved'
                  AND library_id IS NOT NULL
                  AND uploader_id <> ALL($1::BIGINT[])
                  AND $4::boolean
                  AND lower(filename) NOT LIKE '%%.gif'
            ) AS item
            ORDER BY random()
            LIMIT 1
            """,
            blocked,
            images,
            gifs,
            videos,
        )
        return dict(row) if row is not None else None

    async def _hourly_post_url(self, row: dict[str, Any]) -> str:
        """Resolve a fresh Discord CDN URL for a library row when possible."""

        source_url = str(row.get("source_url") or "")
        upload_id = int(row["id"])
        review_message_id = row.get("review_message_id")
        if review_message_id is None:
            return source_url if is_discord_media_url(source_url) else ""

        # The library helpers already know how to refresh an attachment URL
        # from the durable review message and persist the refreshed URL.  Keep
        # this scheduler independent from the Fun cog so it can still operate
        # while the cog is being reloaded.
        fun_cog = cast(Any, self.bot.get_cog("Fun"))
        if fun_cog is not None:
            try:
                if row.get("media_kind") == "video":
                    return await fun_cog._current_video_url(
                        upload_id=upload_id,
                        source_url=source_url,
                        review_message_id=int(review_message_id),
                    )
                return await fun_cog._current_post_url(
                    upload_id=upload_id,
                    source_url=source_url,
                    review_message_id=int(review_message_id),
                )
            except (discord.HTTPException, commands.BadArgument):
                self.bot.logger.warning(
                    "Could not refresh hourly-post media %s", upload_id, exc_info=True
                )
        return source_url if is_discord_media_url(source_url) else ""

    async def _send_hourly_post(
        self, settings: dict[str, Any], row: dict[str, Any]
    ) -> bool:
        """Send one random approved item to one configured guild channel."""

        channel_id = int(settings["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                self.bot.logger.warning(
                    "Hourly-post channel %s is unavailable for guild %s",
                    channel_id,
                    settings["guild_id"],
                    exc_info=True,
                )
                return False
        if not hasattr(channel, "send"):
            self.bot.logger.warning(
                "Hourly-post channel %s is not messageable", channel_id
            )
            return False
        messageable = cast(discord.abc.Messageable, channel)

        source_url = await self._hourly_post_url(row)
        if not source_url:
            self.bot.logger.warning(
                "Hourly-post library item %s has no usable Discord URL", row["id"]
            )
            return False

        uploader_id = int(row["uploader_id"])
        uploader = self.bot.get_user(uploader_id)
        uploader_name = discord.utils.escape_markdown(
            str(getattr(uploader, "name", None) or f"User {uploader_id}")
        )[:100]
        library_id = int(row.get("library_id") or row["id"])
        media_label = (
            "Video"
            if row.get("media_kind") == "video"
            else (
                "GIF"
                if hourly_post_media_kind(row.get("filename")) == "gif"
                else "Image"
            )
        )
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Hourly Fishie post"),
                discord.ui.Separator(),
                discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# Library ID `{library_id}` · {media_label} · "
                    f"Uploaded by {uploader_name} (`{uploader_id}`)"
                ),
                accent_color=self.bot.embedcolor,
            )
        )
        try:
            await messageable.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.HTTPException):
            self.bot.logger.warning(
                "Could not send hourly-post library item %s to channel %s",
                library_id,
                channel_id,
                exc_info=True,
            )
            return False
        self.bot.logger.info(
            "Sent hourly-post library item %s to guild %s channel %s",
            library_id,
            settings["guild_id"],
            channel_id,
        )
        return True

    async def publish_hourly_posts(self) -> int:
        """Publish one random item for each configured guild."""
        # Settings are loaded into db_cache during bot startup and updated by
        # the settings commands.  Reading the cache here avoids one settings
        # query per guild while still reflecting edits immediately.
        cache = self.bot.db_cache
        now = discord.utils.utcnow()
        rows = [
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "images": "images" in cache.hourly_post_media.get(guild_id, set()),
                "gifs": "gifs" in cache.hourly_post_media.get(guild_id, set()),
                "videos": "videos" in cache.hourly_post_media.get(guild_id, set()),
                "blocked_users": cache.hourly_post_blocks.get(guild_id, set()),
            }
            for guild_id, channel_id in cache.hourly_posts.items()
            if not is_operational_guild(guild_id)
            if cache.hourly_post_next_at.get(guild_id) is None
            or cache.hourly_post_next_at[guild_id] <= now
        ]
        sent = 0
        for record in rows:
            settings = record
            try:
                item = await self._hourly_post_media(settings)
                if item is not None and await self._send_hourly_post(
                    settings, dict(item)
                ):
                    interval = cache.hourly_post_intervals.get(settings["guild_id"], 60)
                    next_post_at = discord.utils.utcnow() + timedelta(minutes=interval)
                    await self.bot.pool.execute(
                        "UPDATE guild_hourly_posts SET next_post_at=$2 "
                        "WHERE guild_id=$1",
                        settings["guild_id"],
                        next_post_at,
                    )
                    cache.set_hourly_post_next_at(settings["guild_id"], next_post_at)
                    sent += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.bot.logger.exception(
                    "Hourly-post delivery failed for guild %s",
                    settings.get("guild_id"),
                )
        return sent

    @tasks.loop(minutes=30.0)
    async def set_key_task(self):
        try:
            await self.set_spotify_key()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.bot.logger.warning("Could not refresh Spotify client token: %s", error)

    @set_key_task.before_loop
    async def before_set_key_task(self):
        await self.bot.wait_until_ready()

    async def cleanup_status_history(self) -> int:
        result = await self.bot.pool.execute("""
            DELETE FROM user_status_history
            WHERE ended_at IS NOT NULL
              AND ended_at < now() - interval '32 days'
            """)
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (IndexError, ValueError):
            return 0

    @tasks.loop(hours=6)
    async def status_history_cleanup_task(self) -> None:
        try:
            deleted = await self.cleanup_status_history()
        except Exception:
            self.bot.logger.exception("Status history cleanup failed")
        else:
            if deleted:
                self.bot.logger.info(
                    "Removed %s expired status history rows",
                    deleted,
                )

    @status_history_cleanup_task.before_loop
    async def before_status_history_cleanup_task(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        self.set_key_task.cancel()
        self.delete_videos_task.cancel()
        self.status_history_cleanup_task.cancel()
        self.twitch_eventsub_sync_task.cancel()
        self.twitch_reconciliation_task.cancel()
        self.twitch_event_inbox_task.cancel()
        self.notify_twitch_task.cancel()
        self.notify_anime_task.cancel()
        self.youtube_websub_sync_task.cancel()
        self.youtube_event_inbox_task.cancel()
        self.youtube_community_task.cancel()
        self.hourly_posts_task.cancel()

    async def cog_load(self) -> None:
        # Both applications share the same database during the hand-off.  The
        # legacy process remains online only to redirect users, so it must not
        # run durable schedulers that could deliver duplicate notifications,
        # hourly posts, or cleanup work.  Testing and replacement instances
        # retain the historical scheduler behavior.
        if is_legacy_instance(self.bot):
            self.bot.logger.info(
                "Skipping shared event schedulers on legacy bot instance"
            )
            return
        self._twitch_access_token: str | None = None
        self._twitch_token_expires_at = 0.0
        self.set_key_task.start()
        self.delete_videos_task.start()
        self.status_history_cleanup_task.start()
        self.twitch_eventsub_sync_task.start()
        self.twitch_reconciliation_task.start()
        self.twitch_event_inbox_task.start()
        self.notify_twitch_task.start()
        self.notify_anime_task.start()
        self.youtube_websub_sync_task.start()
        self.youtube_event_inbox_task.start()
        self.youtube_community_task.start()
        self.hourly_posts_task.start()

    @tasks.loop(minutes=10.0)
    async def delete_videos_task(self):
        self.delete_videos()

    @tasks.loop(minutes=15.0)
    async def twitch_eventsub_sync_task(self):
        try:
            await self.sync_twitch_eventsub_subscriptions()
        except Exception as error:
            self.bot.logger.warning("Twitch EventSub sync failed: %s", error)

    @twitch_eventsub_sync_task.before_loop
    async def before_twitch_eventsub_sync_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30.0)
    async def twitch_reconciliation_task(self):
        try:
            await self.check_twitch_streams()
        except Exception as error:
            self.bot.logger.warning("Twitch stream reconciliation failed: %s", error)

    @twitch_reconciliation_task.before_loop
    async def before_twitch_reconciliation_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30.0)
    async def twitch_event_inbox_task(self):
        for _ in range(25):
            if not await self.process_twitch_event_message():
                break

    @twitch_event_inbox_task.before_loop
    async def before_twitch_event_inbox_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5.0)
    async def notify_twitch_task(self):
        try:
            await self.check_notify_twitch()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Notify Twitch scheduler failed")

    @notify_twitch_task.before_loop
    async def before_notify_twitch_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=10.0)
    async def notify_anime_task(self):
        try:
            await self.check_notify_anime()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Notify anime scheduler failed")

    @notify_anime_task.before_loop
    async def before_notify_anime_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6.0)
    async def youtube_websub_sync_task(self):
        try:
            await cast(Any, self).sync_youtube_subscriptions()
        except Exception:
            self.bot.logger.exception("YouTube WebSub sync failed")

    @youtube_websub_sync_task.before_loop
    async def before_youtube_websub_sync_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30.0)
    async def youtube_event_inbox_task(self):
        for _ in range(25):
            if not await cast(Any, self).process_youtube_event():
                break

    @youtube_event_inbox_task.before_loop
    async def before_youtube_event_inbox_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=2.0)
    async def youtube_community_task(self):
        try:
            await cast(Any, self).check_youtube_community_posts()
        except Exception:
            self.bot.logger.exception("YouTube community post check failed")

    @youtube_community_task.before_loop
    async def before_youtube_community_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1.0)
    async def hourly_posts_task(self):
        try:
            await self.publish_hourly_posts()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A missing migration or a transient database outage should not
            # stop the rest of the event scheduler.  The next pass will retry.
            self.bot.logger.exception("Hourly-post scheduler failed")

    @hourly_posts_task.before_loop
    async def before_hourly_posts_task(self):
        await self.bot.wait_until_ready()
