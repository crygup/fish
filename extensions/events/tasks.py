from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any, Dict, cast

import discord
from discord.ext import commands, tasks

from core import Cog
from utils import run

TWITCH_EVENTSUB_CALLBACK = "https://api.crygup.com/fishie/twitch/eventsub"


class Tasks(Cog):
    def _twitch_eventsub_secret(self) -> str | None:
        keys = self.bot.config["keys"]
        secret = keys.get("twitch_eventsub_secret") or keys.get("twitch_secret")
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

        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"fishie:twitch:eventsub:{broadcaster_id}",
                )
                existing = await connection.fetchrow(
                    "SELECT subscription_id, status "
                    "FROM twitch_eventsub_subscriptions "
                    "WHERE broadcaster_id = $1",
                    broadcaster_id,
                )
                if existing and existing["status"] == "enabled":
                    return True

                if (
                    existing
                    and existing["status"] == "webhook_callback_verification_pending"
                ):
                    try:
                        async with self.bot.session.get(
                            "https://api.twitch.tv/helix/eventsub/subscriptions",
                            headers={
                                "Client-ID": self.bot.config["keys"]["twitch_id"],
                                "Authorization": f"Bearer {token}",
                            },
                            params={"id": existing["subscription_id"]},
                        ) as response:
                            remote_data = await response.json(content_type=None)
                    except Exception as error:
                        self.bot.logger.warning(
                            "Could not verify Twitch EventSub subscription for %s: %s",
                            broadcaster_id,
                            error,
                        )
                        return False

                    remote_subscriptions = (
                        remote_data.get("data")
                        if isinstance(remote_data, dict)
                        else None
                    )
                    remote = (
                        remote_subscriptions[0]
                        if isinstance(remote_subscriptions, list)
                        and remote_subscriptions
                        and isinstance(remote_subscriptions[0], dict)
                        else None
                    )
                    remote_status = remote.get("status") if remote else None
                    if remote_status in {
                        "enabled",
                        "webhook_callback_verification_pending",
                    }:
                        if remote_status != existing["status"]:
                            await connection.execute(
                                "UPDATE twitch_eventsub_subscriptions "
                                "SET status = $2, updated_at = now() "
                                "WHERE broadcaster_id = $1",
                                broadcaster_id,
                                remote_status,
                            )
                        return True

                    # Twitch rejected or revoked the old subscription. Remove the
                    # stale local row so the new subscription can be created.
                    await connection.execute(
                        "DELETE FROM twitch_eventsub_subscriptions "
                        "WHERE broadcaster_id = $1",
                        broadcaster_id,
                    )

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
                    self.bot.logger.warning(
                        "Could not create Twitch EventSub subscription for %s: %s",
                        broadcaster_id,
                        error,
                    )
                    return False

                subscriptions = data.get("data") if isinstance(data, dict) else None
                if response.status not in (200, 202) or not subscriptions:
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
                await connection.execute(
                    """INSERT INTO twitch_eventsub_subscriptions
                       (broadcaster_id, subscription_id, status)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (broadcaster_id) DO UPDATE
                       SET subscription_id = EXCLUDED.subscription_id,
                           status = EXCLUDED.status,
                           updated_at = now()""",
                    broadcaster_id,
                    subscription["id"],
                    subscription.get("status", "enabled"),
                )
        return True

    async def sync_twitch_eventsub_subscriptions(self) -> None:
        await self.bot.pool.execute(
            "DELETE FROM twitch_eventsub_events "
            "WHERE received_at < now() - interval '1 day'"
        )
        rows = await self.bot.pool.fetch(
            "SELECT DISTINCT channel_name, broadcaster_id FROM twitch_follows"
        )
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
            ")"
        )
        for row in orphaned:
            await self.remove_twitch_eventsub_subscription(str(row["broadcaster_id"]))

    async def remove_twitch_eventsub_subscription(self, broadcaster_id: str) -> None:
        remaining = await self.bot.pool.fetchval(
            "SELECT 1 FROM twitch_follows WHERE broadcaster_id = $1 LIMIT 1",
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
                params={"id": subscription["subscription_id"]},
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
            return
        if event_type != "stream.online":
            return

        rows = await self.bot.pool.fetch(
            "SELECT guild_id, channel_name, announce_channel_id, message_template, "
            "last_stream_id FROM twitch_follows WHERE broadcaster_id = $1",
            broadcaster_id,
        )
        if not rows:
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

    async def _announce_twitch_stream_once(
        self, row: Any, stream: dict[str, Any]
    ) -> bool:
        """Claim a stream notification before sending it.

        EventSub and the reconciliation loop can process the same stream at
        the same time. The transaction lock serializes the check, send, and
        update, so only one path announces a given stream for a guild/channel
        pair while failed sends remain retryable.
        """
        stream_id = str(stream.get("id") or "")
        if not stream_id:
            self.bot.logger.warning(
                "Twitch stream event for %s did not include a stream ID",
                row["channel_name"],
            )
            return False

        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"fishie:twitch:announce:{row['guild_id']}:{row['channel_name']}",
                )
                previous_id = await connection.fetchval(
                    "SELECT last_stream_id FROM twitch_follows "
                    "WHERE guild_id = $1 AND channel_name = $2",
                    row["guild_id"],
                    row["channel_name"],
                )
                if previous_id == stream_id:
                    return False

                announced = await self._announce_twitch_stream(row, stream)
                if announced:
                    await connection.execute(
                        "UPDATE twitch_follows SET last_stream_id = $3 "
                        "WHERE guild_id = $1 AND channel_name = $2",
                        row["guild_id"],
                        row["channel_name"],
                        stream_id,
                    )
                return announced

    async def _announce_twitch_stream(self, row, stream: dict[str, Any]) -> bool:
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
        title = str(stream.get("title") or "")
        game = str(stream.get("game_name") or "")
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

        safe_title = discord.utils.escape_markdown(
            title or f"{display_name} is live on Twitch!"
        )
        stream_text = f"### [{safe_title}](https://www.twitch.tv/{login})"
        if game:
            stream_text += f"\n**Playing:** {discord.utils.escape_markdown(game)}"

        children: list[discord.ui.Item] = []
        if row["message_template"]:
            children.append(discord.ui.TextDisplay(str(row["message_template"])))
        children.append(discord.ui.TextDisplay(stream_text))
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

        container = discord.ui.Container(*children, accent_color=self.bot.embedcolor)
        view_type = type("TwitchAnnouncementView", (discord.ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        send_kwargs: dict[str, Any] = {"view": view}
        if row["message_template"]:
            send_kwargs["allowed_mentions"] = discord.AllowedMentions.all()

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
        valid_formats = (
            "mp4",
            "webm",
            "mov",
            "mp3",
            "ogg",
            "wav",
            "part",
            "ytdl",
        )
        for file in os.listdir(r"files/downloads"):
            if file.endswith(valid_formats):
                os.remove(os.path.join("files/downloads", file))

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

    async def cog_unload(self):
        self.set_key_task.cancel()
        self.delete_videos_task.cancel()
        self.twitch_eventsub_sync_task.cancel()
        self.twitch_reconciliation_task.cancel()

    async def cog_load(self) -> None:
        self._twitch_access_token: str | None = None
        self._twitch_token_expires_at = 0.0
        self.set_key_task.start()
        self.delete_videos_task.start()
        self.twitch_eventsub_sync_task.start()
        self.twitch_reconciliation_task.start()

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
