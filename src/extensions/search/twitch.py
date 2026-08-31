from __future__ import annotations

import asyncio
import datetime
import re
from typing import TYPE_CHECKING, Any, Optional, cast
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from extensions.context import Context, GuildContext

if TYPE_CHECKING:
    from core import Fishie
    from extensions.events import Events


TWITCH_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
TWITCH_URL_RE = re.compile(
    r"https?://(?:www\.)?twitch\.tv/(?P<name>[A-Za-z0-9_]{1,25})(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _twitch_text(value: object, limit: int = 1_800) -> str:
    text = discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ""))
    ).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _twitch_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _twitch_timestamp(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def _twitch_duration(value: object) -> int:
    """Parse Twitch's ISO 8601 video duration into seconds."""
    match = re.fullmatch(
        r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?",
        str(value or "").upper(),
    )
    if not match:
        return 0
    return (
        int(match.group("h") or 0) * 3600
        + int(match.group("m") or 0) * 60
        + int(match.group("s") or 0)
    )


def _resolve_twitch_mention(
    ctx: GuildContext, value: str | None
) -> tuple[int | None, bool]:
    """Resolve the optional role/@everyone mention used by text follows."""

    if value is None or not value.strip():
        return None, False
    raw = value.strip()
    if raw.casefold() in {"everyone", "@everyone"}:
        return None, True
    match = re.fullmatch(r"<@&(?P<id>\d{15,25})>", raw)
    role = ctx.guild.get_role(int(match.group("id"))) if match else None
    if role is None and raw.isdigit():
        role = ctx.guild.get_role(int(raw))
    if role is None and not match and not raw.isdigit():
        role = next(
            (
                candidate
                for candidate in ctx.guild.roles
                if candidate.name.casefold() == raw.casefold()
            ),
            None,
        )
    if role is None or role.is_default():
        raise commands.BadArgument("I could not find that role in this server.")
    return role.id, False


class TwitchAccountView(discord.ui.LayoutView):
    """Components V2 view for a public Twitch channel."""

    def __init__(
        self,
        ctx: Context,
        user: dict[str, Any],
        stream: dict[str, Any] | None,
        followers: int | None,
        subscribers: int | None,
        latest_video: dict[str, Any] | None,
        archived_seconds: int,
    ) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        login = str(user.get("login") or "").strip()
        display_name = _twitch_text(user.get("display_name") or login, 120)
        profile_url = _twitch_url(user.get("profile_image_url"))
        channel_url = f"https://www.twitch.tv/{login}" if login else "https://twitch.tv"

        title = discord.ui.TextDisplay(f"## [{display_name}]({channel_url})")
        description = _twitch_text(user.get("description"), 1_500)
        if not description:
            description = "No channel description provided."
        children: list[discord.ui.Item[Any]] = []
        if profile_url:
            children.append(
                discord.ui.Section(
                    title,
                    discord.ui.TextDisplay(description),
                    accessory=discord.ui.Thumbnail(profile_url),
                )
            )
        else:
            children.extend((title, discord.ui.TextDisplay(description)))
        children.append(discord.ui.Separator())

        details: list[str] = []
        if stream:
            viewers = stream.get("viewer_count")
            try:
                viewer_text = f"{int(viewers):,}" if viewers is not None else "Unknown"
            except (TypeError, ValueError):
                viewer_text = "Unknown"
            details.append(f"**Live:** Yes · {viewer_text} viewers")
        else:
            details.append("**Live:** No")

        if followers is not None:
            details.append(f"**Followers:** {followers:,}")
        if subscribers is not None:
            details.append(f"**Subscribers:** {subscribers:,}")

        if not stream and latest_video:
            last_stream = _twitch_timestamp(
                latest_video.get("created_at") or latest_video.get("published_at")
            )
            if last_stream is not None:
                details.append(f"**Last streamed:** <t:{last_stream}:R>")

        # Twitch exposes archived video durations, but it has no lifetime
        # streamed-hours or global follower-rank endpoint. Show the hours that
        # are available from the public archive instead of inventing totals.
        if archived_seconds > 0:
            hours = archived_seconds / 3600
            details.append(f"**Archived stream hours:** {hours:,.1f}")

        broadcaster_type = _twitch_text(user.get("broadcaster_type"), 60)
        if broadcaster_type:
            details.append(f"**Account type:** {broadcaster_type.title()}")

        children.append(
            discord.ui.TextDisplay(
                "\n".join(details) or "No additional channel information was provided."
            )
        )

        banner = _twitch_url(user.get("offline_image_url"))
        if banner:
            children.append(discord.ui.Separator())
            children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        banner, description=f"{display_name} banner"
                    )
                )
            )

        footer = f"Twitch ID: {user.get('id', 'Unknown')} · Data from Twitch"
        children.append(discord.ui.TextDisplay(f"-# {_twitch_text(footer, 300)}"))
        container = discord.ui.Container(*children, accent_color=ctx.bot.embedcolor)
        self.add_item(container)
        self.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Open Twitch channel",
                    style=discord.ButtonStyle.link,
                    url=channel_url,
                )
            )
        )


class Twitch(Cog):
    """Twitch account lookups and server live-announcement settings."""

    emoji = discord.PartialEmoji(name="📺")

    def __init__(self, bot: Fishie) -> None:
        super().__init__()
        self.bot = bot

    def _events(self) -> Events | None:
        events = self.bot.get_cog("Events")
        return cast("Events | None", events)

    @staticmethod
    def _normalise_channel(value: str) -> str:
        value = value.strip().lstrip("@")
        match = TWITCH_URL_RE.fullmatch(value)
        if match:
            value = match.group("name")
        return value.lower()

    async def _helix(
        self,
        endpoint: str,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        events = self._events()
        if events is None or not hasattr(events, "_get_twitch_access_token"):
            return None
        token = await events._get_twitch_access_token()
        if not token:
            return None
        try:
            async with self.bot.session.get(
                f"https://api.twitch.tv/helix/{endpoint}",
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
        return data

    async def _channel_details(
        self, user: dict[str, Any]
    ) -> tuple[
        dict[str, Any] | None, int | None, int | None, dict[str, Any] | None, int
    ]:
        broadcaster_id = str(user.get("id") or "")
        stream_data, follower_data, subscriber_data, video_data = await asyncio.gather(
            self._helix("streams", params={"user_id": broadcaster_id}),
            self._helix(
                "channels/followers",
                params={"broadcaster_id": broadcaster_id, "first": 1},
            ),
            self._helix(
                "subscriptions", params={"broadcaster_id": broadcaster_id, "first": 1}
            ),
            self._helix(
                "videos",
                params={"user_id": broadcaster_id, "type": "archive", "first": 100},
            ),
        )
        streams = stream_data.get("data") if stream_data else None
        stream = streams[0] if isinstance(streams, list) and streams else None
        followers = follower_data.get("total") if follower_data else None
        if not isinstance(followers, int):
            followers = None
        subscribers = subscriber_data.get("total") if subscriber_data else None
        if not isinstance(subscribers, int):
            subscribers = None
        videos = video_data.get("data") if video_data else None
        if not isinstance(videos, list):
            videos = []
        latest_video = videos[0] if videos and isinstance(videos[0], dict) else None
        archived_seconds = sum(
            _twitch_duration(video.get("duration"))
            for video in videos
            if isinstance(video, dict)
        )
        return stream, followers, subscribers, latest_video, archived_seconds

    @commands.hybrid_group(
        name="twitch",
        fallback="account",
        aliases=("tw",),
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(channel_name="Twitch channel name, @handle, or channel URL.")
    async def twitch(self, ctx: Context, channel_name: str):
        """Look up a public Twitch channel."""
        channel_name = self._normalise_channel(channel_name)
        if not TWITCH_CHANNEL_RE.fullmatch(channel_name):
            raise commands.BadArgument(
                "Enter a valid Twitch channel name, @handle, or channel URL."
            )
        events = self._events()
        if events is None or not hasattr(events, "_get_twitch_user"):
            raise commands.CommandError("Twitch lookups are not available right now.")
        async with ctx.typing():
            user = await events._get_twitch_user(channel_name)
            if not user:
                raise commands.BadArgument(
                    f"Could not find a Twitch channel named **{channel_name}**."
                )
            stream, followers, subscribers, latest_video, archived_seconds = (
                await self._channel_details(user)
            )
            view = TwitchAccountView(
                ctx,
                user,
                stream,
                followers,
                subscribers,
                latest_video,
                archived_seconds,
            )
            await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @twitch.command(
        name="follows", aliases=("followed", "list"), with_app_command=False
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_follows(self, ctx: GuildContext):
        """Show the Twitch channels followed by this server."""
        rows = await self.bot.pool.fetch(
            """
            SELECT DISTINCT ON (lower(btrim(channel_name)))
                channel_name, announce_channel_id
            FROM (
                SELECT channel_name, announce_channel_id, 1 AS source
                FROM notify_twitch_follows WHERE guild_id = $1
                UNION ALL
                SELECT channel_name, announce_channel_id, 2 AS source
                FROM twitch_follows WHERE guild_id = $1
            ) follows
            ORDER BY lower(btrim(channel_name)), source, channel_name
            """,
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("No Twitch channels are being followed.")
        lines = [
            f"**{row['channel_name']}** → <#{row['announce_channel_id']}>"
            for row in rows
        ]
        await ctx.send("Twitch live announcements:\n" + "\n".join(lines))

    @twitch.command(name="follow", with_app_command=False)
    @app_commands.describe(
        channel_name="Twitch channel name or handle to follow.",
        announcement_channel="Text channel for live announcements. Defaults to this channel.",
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_follow(
        self,
        ctx: GuildContext,
        channel_name: str,
        announcement_channel: Optional[discord.TextChannel] = None,
    ):
        """Follow a Twitch channel and announce live streams here or elsewhere."""
        channel_name = self._normalise_channel(channel_name)
        if not TWITCH_CHANNEL_RE.fullmatch(channel_name):
            raise commands.BadArgument(
                "Enter a valid Twitch channel name (letters, numbers, and underscores only)."
            )
        events = self._events()
        if events is None or not hasattr(events, "_get_twitch_user"):
            raise commands.BadArgument("Twitch monitoring is not available right now.")
        twitch_user = await events._get_twitch_user(channel_name)
        if not twitch_user or not twitch_user.get("id"):
            raise commands.BadArgument(
                f"Could not find a Twitch channel named **{channel_name}**."
            )
        broadcaster_id = str(twitch_user["id"])
        target = announcement_channel or ctx.channel
        if not hasattr(target, "send"):
            raise commands.BadArgument(
                "Choose a text channel for Twitch announcements."
            )

        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"fishie:twitch:{ctx.guild.id}",
                )
                existing = await connection.fetchval(
                    "SELECT 1 FROM twitch_follows "
                    "WHERE guild_id = $1 AND channel_name = $2",
                    ctx.guild.id,
                    channel_name,
                )
                if not existing:
                    count = await connection.fetchval(
                        """
                        SELECT COUNT(DISTINCT lower(channel_name))
                        FROM (
                            SELECT channel_name FROM twitch_follows WHERE guild_id = $1
                            UNION ALL
                            SELECT channel_name FROM notify_twitch_follows WHERE guild_id = $1
                        ) follows
                        """,
                        ctx.guild.id,
                    )
                    if count >= 10:
                        raise commands.BadArgument(
                            "You can follow up to 10 Twitch channels per server."
                        )
                await connection.execute(
                    """INSERT INTO twitch_follows
                       (guild_id, channel_name, announce_channel_id, broadcaster_id)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (guild_id, channel_name) DO UPDATE
                       SET announce_channel_id = EXCLUDED.announce_channel_id,
                           broadcaster_id = EXCLUDED.broadcaster_id""",
                    ctx.guild.id,
                    channel_name,
                    target.id,
                    broadcaster_id,
                )

                # Keep the new notification store in sync with the legacy
                # text command. There is one notify row per followed Twitch
                # channel and guild, so moving the legacy follow updates that
                # row's destination instead of creating another subscription.
                mirrored = await connection.execute(
                    """
                    UPDATE notify_twitch_follows
                    SET announce_channel_id = $3,
                        broadcaster_id = $4,
                        updated_at = now()
                    WHERE guild_id = $1 AND lower(btrim(channel_name)) = lower(btrim($2))
                    """,
                    ctx.guild.id,
                    channel_name,
                    target.id,
                    broadcaster_id,
                )
                if mirrored == "UPDATE 0":
                    await connection.execute(
                        """
                        INSERT INTO notify_twitch_follows
                            (guild_id, channel_name, broadcaster_id, announce_channel_id)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT DO NOTHING
                        """,
                        ctx.guild.id,
                        channel_name,
                        broadcaster_id,
                        target.id,
                    )

        try:
            await events.ensure_twitch_eventsub_subscription(broadcaster_id)
        except Exception as error:
            self.bot.logger.warning(
                "Could not enable Twitch EventSub for %s: %s", channel_name, error
            )

        await ctx.send(
            f"Now following **{channel_name}**; live announcements will be posted in {target.mention}. "
            "Use `twitch mention <channel> [role/@everyone]` to configure mentions.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @twitch.command(name="mention", with_app_command=False)
    @app_commands.describe(channel_name="Followed Twitch channel name or handle.")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_message(
        self, ctx: GuildContext, channel_name: str, mention: str | None = None
    ):
        """Set or clear the role/@everyone mention for Twitch alerts."""
        channel_name = self._normalise_channel(channel_name)
        exists = await self.bot.pool.fetchval(
            "SELECT 1 FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2 "
            "UNION ALL SELECT 1 FROM notify_twitch_follows "
            "WHERE guild_id = $1 AND lower(channel_name) = lower($2) LIMIT 1",
            ctx.guild.id,
            channel_name,
        )
        if not exists:
            raise commands.BadArgument(
                f"This server is not following **{channel_name}**."
            )
        role_id, everyone = _resolve_twitch_mention(ctx, mention)
        await self.bot.pool.execute(
            """
            UPDATE notify_twitch_follows
            SET mention_role_id = $3, mention_everyone = $4, updated_at = now()
            WHERE guild_id = $1 AND lower(channel_name) = lower($2)
            """,
            ctx.guild.id,
            channel_name,
            role_id,
            everyone,
        )
        await ctx.send(
            f"Twitch mentions for **{channel_name}** were "
            + (
                "set to @everyone."
                if everyone
                else f"set to <@&{role_id}>." if role_id else "cleared."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @twitch.command(
        name="unfollow", aliases=("remove", "delete"), with_app_command=False
    )
    @app_commands.describe(channel_name="Followed Twitch channel name or handle.")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_unfollow(self, ctx: GuildContext, channel_name: str):
        """Stop following a Twitch channel in this server."""
        channel_name = self._normalise_channel(channel_name)
        broadcaster_id = await self.bot.pool.fetchval(
            "SELECT broadcaster_id FROM twitch_follows "
            "WHERE guild_id = $1 AND channel_name = $2 "
            "UNION ALL SELECT broadcaster_id FROM notify_twitch_follows "
            "WHERE guild_id = $1 AND lower(channel_name) = lower($2) LIMIT 1",
            ctx.guild.id,
            channel_name,
        )
        result = await self.bot.pool.execute(
            "DELETE FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2",
            ctx.guild.id,
            channel_name,
        )
        notify_result = await self.bot.pool.execute(
            "DELETE FROM notify_twitch_follows WHERE guild_id = $1 AND lower(channel_name) = lower($2)",
            ctx.guild.id,
            channel_name,
        )
        if result == "DELETE 0" and notify_result == "DELETE 0":
            raise commands.BadArgument(
                f"This server is not following **{channel_name}**."
            )
        if broadcaster_id:
            events = self._events()
            if events is not None and hasattr(
                events, "remove_twitch_eventsub_subscription"
            ):
                await events.remove_twitch_eventsub_subscription(str(broadcaster_id))
        await ctx.send(f"Stopped following **{channel_name}**.")
