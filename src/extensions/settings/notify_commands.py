"""Notification follow commands for Twitch streams and AniList anime."""

from __future__ import annotations

import asyncio
import datetime
import re
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from cachetools import TTLCache
from discord import app_commands
from discord.ext import commands

from core import Cog

from .notify import (
    ANILIST_AUTOCOMPLETE_QUERY,
    ANILIST_NOTIFY_BY_ID_QUERY,
    ANILIST_NOTIFY_QUERY,
    NotifyListView,
    NotifyView,
    anilist_datetime,
    anilist_media_id,
    anilist_media_titles,
    anilist_notification_schedule,
    anilist_search_variants,
    anilist_successors,
    anime_list_details,
    media_external_link,
    media_title,
    normalize_anilist_title,
    normalize_twitch_channel,
    select_anilist_media,
    twitch_list_details,
)

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


# Autocomplete can be invoked on every keystroke.  Keep a short shared cache
# for the remote Twitch/AniList searches and cap requests made by one user so
# a client cannot turn an autocomplete textbox into an API fan-out.  The
# entries are intentionally tiny and expire quickly; followed-item/database
# autocomplete does not use this cache.
_AUTOCOMPLETE_CACHE: TTLCache[str, tuple[app_commands.Choice[str], ...]] = TTLCache(
    maxsize=512, ttl=5.0
)
_AUTOCOMPLETE_CALLS: TTLCache[tuple[int, int | None], deque[float]] = TTLCache(
    maxsize=2_000, ttl=10.0
)
_AUTOCOMPLETE_WINDOW = 10.0
_AUTOCOMPLETE_MAX_CALLS = 20


def _autocomplete_allowed(interaction: discord.Interaction) -> bool:
    """Apply a small per-user autocomplete request budget."""

    user_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
    guild_id_value = getattr(getattr(interaction, "guild", None), "id", None)
    guild_id = int(guild_id_value) if guild_id_value is not None else None
    key = (user_id, guild_id)
    now = time.monotonic()
    calls = _AUTOCOMPLETE_CALLS.get(key)
    if calls is None:
        calls = deque()
    while calls and now - calls[0] >= _AUTOCOMPLETE_WINDOW:
        calls.popleft()
    if len(calls) >= _AUTOCOMPLETE_MAX_CALLS:
        _AUTOCOMPLETE_CALLS[key] = calls
        return False
    calls.append(now)
    _AUTOCOMPLETE_CALLS[key] = calls
    return True


def _scope(ctx: Context) -> tuple[int | None, int | None]:
    """Return the guild scope or DM-user scope for a notification command."""

    if ctx.guild is not None:
        return int(ctx.guild.id), None
    return None, int(ctx.author.id)


def _scope_predicate(guild_id: int | None, user_id: int | None) -> tuple[str, int]:
    if guild_id is not None:
        return "guild_id = $1", guild_id
    assert user_id is not None
    return "user_id = $1", user_id


def _notify_prefix(ctx: Context) -> str:
    """Return the prefix/command marker appropriate for this invocation."""

    # Application commands do not have a text prefix.  Check this first so a
    # context implementation that happens to retain the message prefix does
    # not render ``fish notify`` in slash-command instructions.
    if getattr(ctx, "interaction", None) is not None:
        return "/"
    prefix = getattr(ctx, "get_prefix", None)
    if isinstance(prefix, str) and prefix:
        return prefix
    return "fish "


def _as_utc_datetime(value: object) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)


def _anilist_date(value: object) -> datetime.datetime | None:
    """Convert AniList's ``{year, month, day}`` object to UTC."""

    # AniList frequently omits month/day for an announced season.  The shared
    # parser intentionally treats those omissions as January 1 rather than
    # dropping a useful future date altogether.
    return anilist_datetime(value)


def _mention_target(
    ctx: Context, value: str | None
) -> tuple[int | None, bool, str | None]:
    """Resolve a role, @everyone, or a request to clear mentions."""

    if value is None or not value.strip():
        return None, False, None
    raw = value.strip()
    if raw.casefold() in {"@everyone", "everyone"}:
        if ctx.guild is None:
            raise commands.BadArgument(
                "Mentions can only be used for server notifications."
            )
        return None, True, "@everyone"
    if ctx.guild is None:
        raise commands.BadArgument("Roles can only be used for server notifications.")
    match = re.fullmatch(r"<@&(?P<id>\d{15,25})>", raw)
    role: discord.Role | None = None
    if match:
        role = ctx.guild.get_role(int(match.group("id")))
    elif raw.isdigit():
        role = ctx.guild.get_role(int(raw))
    else:
        role = next(
            (
                candidate
                for candidate in ctx.guild.roles
                if candidate.name.casefold() == raw.casefold()
            ),
            None,
        )
    if role is None:
        raise commands.BadArgument("I could not find that role in this server.")
    if role.is_default():
        raise commands.BadArgument("The @everyone role cannot be selected directly.")
    return role.id, False, role.mention


def _prefix_command_tail(ctx: Context, *path: str) -> str | None:
    """Return all text after a prefix-command path, when available.

    Hybrid command parameters can only preserve spaces in one keyword-only
    argument.  Reading the raw prefix message lets anime titles such as
    ``Frieren: Beyond Journey's End`` retain their full name while slash
    interactions continue to use their structured options.
    """

    message = getattr(ctx, "message", None)
    content = str(getattr(message, "content", "") or "").strip()
    prefix = str(getattr(ctx, "prefix", "") or "")
    if prefix and content.casefold().startswith(prefix.casefold()):
        content = content[len(prefix) :].lstrip()
    tokens = content.split(maxsplit=len(path))
    if len(tokens) != len(path) + 1:
        return None
    if tuple(token.casefold() for token in tokens[: len(path)]) != tuple(
        item.casefold() for item in path
    ):
        return None
    return tokens[-1].strip() or None


def _split_anime_mention(
    ctx: Context, value: str, mention: str | None
) -> tuple[str, str | None]:
    """Recover a trailing role mention from a prefix anime command."""

    if getattr(ctx, "interaction", None) is not None:
        return value, mention
    tail = _prefix_command_tail(ctx, "notify", "mention", "anime")
    if not tail:
        return value, mention
    candidate: str | None = None
    match = re.search(
        r"\s+(<@&\d{15,25}>|\d{15,25}|@everyone|everyone)$", tail, re.IGNORECASE
    )
    if match:
        candidate = match.group(1)
        tail = tail[: match.start()].rstrip()
    else:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            return tail or value, candidate if candidate is not None else mention
        for role in sorted(guild.roles, key=lambda item: len(item.name), reverse=True):
            suffix = f" {role.name}".casefold()
            if tail.casefold().endswith(suffix):
                candidate = role.name
                tail = tail[: -len(suffix)].rstrip()
                break
    return tail or value, candidate if candidate is not None else mention


class Notify(Cog):
    """Follow Twitch channels and anime release dates."""

    emoji = discord.PartialEmoji(name="🔔")

    # AniList marks an anime that is currently airing as ``RELEASING``.  Some
    # API clients expose that value as ``AIRING``, so accept both spellings.
    # Completed, cancelled, and hiatus entries must never be added to a
    # notification follow; a future successor is offered instead.
    _ACTIVE_ANIME_STATUSES = frozenset({"RELEASING", "AIRING", "NOT_YET_RELEASED"})

    def _events(self) -> Any | None:
        events = self.bot.get_cog("Events")
        return events

    def _require_server_admin(self, ctx: Context) -> None:
        if ctx.guild is not None and not ctx.author.guild_permissions.manage_guild:
            raise commands.MissingPermissions(["manage_guild"])

    async def _send(self, ctx: Context, *args: Any, **kwargs: Any) -> discord.Message:
        """Send a command response, keeping application replies ephemeral.

        Prefix invocations remain visible in the channel.  Hybrid command
        interactions, including nested callbacks and follow-up responses after
        an interaction defer, are private to the invoking user.
        """

        if getattr(ctx, "interaction", None) is not None:
            kwargs.setdefault("ephemeral", True)
        return await ctx.send(*args, **kwargs)

    async def _send_info(self, ctx: Context) -> None:
        """Render the notify landing panel.

        A command object is not itself awaitable (``self.notify`` is a
        :class:`HybridGroup`), so nested command fallbacks must call a helper
        rather than attempting ``await self.notify(ctx)``.  Keeping the
        renderer here also ensures text and slash fallbacks use identical
        Components V2 output.
        """

        text = (
            "## Notifications\n"
            "Follow Twitch channels or anime releases and receive notifications "
            "in this server or by DM.\n\n"
            "Use `notify add twitch <channel>` or `notify add anime <title>`."
        )
        await self._send(
            ctx,
            view=NotifyView(text, "Use `notify list` to view active follows."),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    async def _twitch_user(self, name: str) -> dict[str, Any] | None:
        events = self._events()
        if events is None or not hasattr(events, "_get_twitch_user"):
            return None
        return await events._get_twitch_user(name)

    async def _find_upcoming_successor(
        self, media: dict[str, Any], now: datetime.datetime
    ) -> dict[str, Any] | None:
        """Find a future sequel/part when the selected anime is complete.

        AniList relation nodes only include one level of relations in the
        notification query.  Walk a bounded successor chain and hydrate each
        node by ID as needed, allowing a search such as ``rezero`` (which can
        initially select the finished first season) to discover a currently
        airing fourth season without silently subscribing to it.
        """

        queue: list[tuple[dict[str, Any], int]] = [
            (candidate, 1) for candidate in anilist_successors(media)
        ]
        seen: set[int] = set()
        scheduled: list[dict[str, Any]] = []
        while queue and len(seen) < 24:
            candidate, depth = queue.pop(0)
            try:
                candidate_id = int(candidate.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                candidate_id = 0
            if not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            current = candidate
            # Relation nodes have the basic schedule fields but not always a
            # status (older cached API responses omitted it).  Hydrate those
            # nodes so the active/finished check is authoritative.
            if not current.get("status") or "relations" not in current:
                hydrated = await self._anilist_lookup(
                    f"https://anilist.co/anime/{candidate_id}"
                )
                if isinstance(hydrated, dict):
                    current = hydrated
            schedule = anilist_notification_schedule(current, now)
            if schedule[0] is not None or schedule[2] is not None:
                scheduled.append(current)
            if depth >= 6:
                continue
            for successor in anilist_successors(current):
                queue.append((successor, depth + 1))
        if not scheduled:
            return None

        def _sort_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
            airing, _episode, release = anilist_notification_schedule(candidate, now)
            date = airing or release
            try:
                candidate_id = int(candidate.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                candidate_id = 0
            return (
                1 if airing is not None else 0,
                int(date.timestamp()) if date is not None else 0,
                candidate_id,
            )

        return max(scheduled, key=_sort_key)

    async def _anilist_lookup(self, value: str) -> dict[str, Any] | None:
        raw_value = " ".join(str(value or "").strip().split())
        media_id = anilist_media_id(raw_value)
        # Successor suggestions and autocomplete can pass an AniList ID
        # directly.  Treat an all-numeric value as an ID while keeping normal
        # title searches unchanged.
        if media_id is None and raw_value.isdecimal():
            media_id = int(raw_value)
        if media_id is not None:
            query = ANILIST_NOTIFY_BY_ID_QUERY
            variables: dict[str, Any] = {"id": media_id}
        else:
            query = ANILIST_NOTIFY_QUERY
            variables = {"search": raw_value, "type": "ANIME"}
        if not variables.get("id") and not variables.get("search"):
            return None
        candidates = (
            (raw_value,) if media_id is not None else anilist_search_variants(raw_value)
        )
        fallback_media: dict[str, Any] | None = None
        wanted_title = normalize_anilist_title(raw_value)
        for candidate in candidates:
            request_variables = (
                variables
                if media_id is not None
                else {
                    "search": candidate,
                    "type": "ANIME",
                }
            )
            try:
                async with self.bot.session.post(
                    "https://graphql.anilist.co",
                    json={"query": query, "variables": request_variables},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return None
            if response.status != 200 or not isinstance(payload, dict):
                return None
            data = payload.get("data")
            if media_id is not None:
                media = data.get("Media") if isinstance(data, dict) else None
                return media if isinstance(media, dict) else None
            page = data.get("Page") if isinstance(data, dict) else None
            entries = page.get("media") if isinstance(page, dict) else None
            media = (
                select_anilist_media(
                    entries,
                    raw_value,
                )
                if isinstance(entries, list)
                else None
            )
            if isinstance(media, dict):
                # ``SEARCH_MATCH`` can return a plausible but different title
                # for a punctuation-heavy query.  Keep that as a fallback,
                # but continue through the punctuation-light variant so an
                # exact title is preferred whenever AniList exposes it.
                fallback_media = fallback_media or media
                if wanted_title and wanted_title in anilist_media_titles(media):
                    return media
        return fallback_media

    @staticmethod
    def _normalize_anime_query(value: str) -> str:
        """Normalize the optional ``@`` shorthand used for anime searches."""

        value = value.strip()
        return value[1:].lstrip() if value.startswith("@") else value

    @staticmethod
    def _autocomplete_needle(value: str) -> str:
        """Normalize the text Discord sends while an option is being typed."""

        return str(value or "").strip().casefold().lstrip("@#")

    async def _followed_twitch_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return Twitch follows visible in the current server/DM scope."""

        try:
            if interaction.guild is None:
                rows = await self.bot.pool.fetch(
                    "SELECT DISTINCT ON (lower(btrim(channel_name))) channel_name "
                    "FROM notify_twitch_follows WHERE user_id = $1 "
                    "ORDER BY lower(btrim(channel_name)), channel_name LIMIT 100",
                    interaction.user.id,
                )
            else:
                rows = await self.bot.pool.fetch(
                    """
                    SELECT DISTINCT ON (lower(btrim(channel_name))) channel_name
                    FROM (
                        SELECT channel_name FROM notify_twitch_follows
                        WHERE guild_id = $1
                        UNION ALL
                        SELECT channel_name FROM twitch_follows
                        WHERE guild_id = $1
                    ) follows
                    ORDER BY lower(btrim(channel_name)), channel_name LIMIT 100
                    """,
                    interaction.guild.id,
                )
        except Exception:
            self.bot.logger.exception("Notify Twitch autocomplete lookup failed")
            return []
        needle = self._autocomplete_needle(current)
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            name = str(row["channel_name"]).strip()
            if not name or (needle and needle not in name.casefold()):
                continue
            choices.append(app_commands.Choice(name=f"{name}"[:100], value=name))
        return choices[:25]

    async def _followed_anime_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return followed anime titles visible in the current scope."""

        try:
            if interaction.guild is None:
                rows = await self.bot.pool.fetch(
                    "SELECT DISTINCT ON (anilist_id) title, anilist_id "
                    "FROM notify_anime_follows WHERE user_id = $1 "
                    "ORDER BY anilist_id, updated_at DESC NULLS LAST, id DESC LIMIT 100",
                    interaction.user.id,
                )
            else:
                rows = await self.bot.pool.fetch(
                    "SELECT DISTINCT ON (anilist_id) title, anilist_id "
                    "FROM notify_anime_follows WHERE guild_id = $1 "
                    "ORDER BY anilist_id, updated_at DESC NULLS LAST, id DESC LIMIT 100",
                    interaction.guild.id,
                )
        except Exception:
            self.bot.logger.exception("Notify anime autocomplete lookup failed")
            return []
        needle = self._autocomplete_needle(current)
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            title = str(row["title"]).strip()
            if not title or (needle and needle not in title.casefold()):
                continue
            value = title if len(title) <= 100 else str(row["anilist_id"])
            choices.append(app_commands.Choice(name=title[:100], value=value))
        return choices[:25]

    @staticmethod
    def _guild_channel_choices(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return text channels in position order for channel options."""

        guild = interaction.guild
        if guild is None:
            return []
        needle = Notify._autocomplete_needle(current)
        channels = sorted(
            guild.text_channels,
            key=lambda channel: (getattr(channel, "position", 0), channel.id),
        )
        choices: list[app_commands.Choice[str]] = []
        for channel in channels:
            name = str(channel.name)
            if (
                needle
                and needle not in name.casefold()
                and needle not in str(channel.id)
            ):
                continue
            choices.append(
                app_commands.Choice(name=f"#{name}"[:100], value=str(channel.id))
            )
        return choices[:25]

    @staticmethod
    def _guild_role_choices(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return roles plus the @everyone option for mention settings."""

        guild = interaction.guild
        if guild is None:
            return []
        needle = Notify._autocomplete_needle(current)
        choices: list[app_commands.Choice[str]] = []
        if not needle or "everyone" in needle:
            choices.append(app_commands.Choice(name="@everyone", value="@everyone"))
        for role in sorted(guild.roles, key=lambda item: item.position, reverse=True):
            if role.is_default():
                continue
            name = str(role.name).strip()
            if not name:
                continue
            if needle and needle not in name.casefold() and needle not in str(role.id):
                continue
            choices.append(
                app_commands.Choice(name=f"@{name}"[:100], value=str(role.id))
            )
        return choices[:25]

    async def _twitch_search_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Search Twitch accounts for add/lookup command suggestions."""

        query = str(current or "").strip()
        if not query:
            return []
        if not _autocomplete_allowed(interaction):
            return []
        cache_key = f"twitch:{query.casefold()[:100]}"
        cached = _AUTOCOMPLETE_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)
        try:
            normalized = normalize_twitch_channel(query)
        except ValueError:
            normalized = ""
        # A complete channel URL/handle is cheaper to resolve through the
        # existing exact lookup than through the search endpoint.
        if normalized:
            user = await self._twitch_user(normalized)
            if user:
                login = str(user.get("login") or normalized)
                display = str(user.get("display_name") or login)
                choices = [
                    app_commands.Choice(
                        name=f"{display} (@{login})"[:100], value=login[:100]
                    )
                ]
                _AUTOCOMPLETE_CACHE[cache_key] = tuple(choices)
                return choices

        events = self._events()
        token_getter = getattr(events, "_get_twitch_access_token", None)
        if events is None or token_getter is None:
            return []
        try:
            token = await token_getter()
            client_id = self.bot.config["keys"]["twitch_id"]
            if not token or not client_id:
                return []
            async with self.bot.session.get(
                "https://api.twitch.tv/helix/search/channels",
                headers={
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {token}",
                },
                params={"query": query.lstrip("@"), "first": 25},
                timeout=aiohttp.ClientTimeout(total=2.5),
            ) as response:
                payload = await response.json(content_type=None)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return []
        if response.status != 200 or not isinstance(payload, dict):
            return []
        entries = payload.get("data")
        if not isinstance(entries, list):
            return []
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            login = str(entry.get("broadcaster_login") or "").strip()
            if not login or login.casefold() in seen:
                continue
            seen.add(login.casefold())
            display = str(entry.get("display_name") or login).strip()
            choices.append(
                app_commands.Choice(
                    name=f"{display} (@{login})"[:100], value=login[:100]
                )
            )
        result = choices[:25]
        _AUTOCOMPLETE_CACHE[cache_key] = tuple(result)
        return result

    async def _anime_search_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Search AniList titles for add/lookup command suggestions."""

        query = self._normalize_anime_query(str(current or "")).strip()
        if not query:
            return []
        if not _autocomplete_allowed(interaction):
            return []
        cache_key = f"anime:{query.casefold()[:100]}"
        cached = _AUTOCOMPLETE_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)
        try:
            async with self.bot.session.post(
                "https://graphql.anilist.co",
                json={
                    "query": ANILIST_AUTOCOMPLETE_QUERY,
                    "variables": {"search": query},
                },
                timeout=aiohttp.ClientTimeout(total=2.5),
            ) as response:
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []
        if response.status != 200 or not isinstance(payload, dict):
            return []
        data = payload.get("data")
        page = data.get("Page") if isinstance(data, dict) else None
        entries = page.get("media") if isinstance(page, dict) else None
        if not isinstance(entries, list):
            return []
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = media_title(entry)
            media_id = entry.get("id")
            if not title or title.casefold() in seen or media_id is None:
                continue
            seen.add(title.casefold())
            value = title if len(title) <= 100 else str(media_id)
            choices.append(app_commands.Choice(name=title[:100], value=value))
        result = choices[:25]
        _AUTOCOMPLETE_CACHE[cache_key] = tuple(result)
        return result

    @commands.hybrid_group(name="notify", fallback="info", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify(self, ctx: Context) -> None:
        """Follow Twitch channels and anime releases."""
        await self._send_info(ctx)

    @notify.group(
        name="add", aliases=("follow",), fallback="info", invoke_without_command=True
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_add(self, ctx: Context) -> None:
        """Add a Twitch or anime notification follow."""
        await self._send_info(ctx)

    async def _add_twitch(self, ctx: Context, channel: str) -> None:
        """Persist one Twitch follow and schedule its EventSub subscription."""

        self._require_server_admin(ctx)
        try:
            name = normalize_twitch_channel(channel)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error
        twitch_user = await self._twitch_user(name)
        if not twitch_user or not twitch_user.get("id"):
            raise commands.BadArgument(
                f"Could not find a Twitch channel named **{name}**."
            )
        guild_id, user_id = _scope(ctx)
        predicate, scope_id = _scope_predicate(guild_id, user_id)
        announce_channel_id = int(ctx.channel.id) if guild_id is not None else None
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"notify:twitch:{scope_id}",
                )
                existing = await connection.fetchval(
                    f"SELECT id FROM notify_twitch_follows WHERE {predicate} "
                    "AND lower(btrim(channel_name)) = lower(btrim($2))",
                    scope_id,
                    name,
                )
                if existing is None and guild_id is not None:
                    # The legacy ``twitch`` command shares server follows
                    # with ``notify``.  Treat a legacy row as an existing
                    # follow even if an old installation has not mirrored it
                    # yet; this prevents a second subscription for the same
                    # Twitch channel.
                    existing = await connection.fetchval(
                        "SELECT 1 FROM twitch_follows "
                        "WHERE guild_id = $1 AND lower(btrim(channel_name)) = lower(btrim($2))",
                        guild_id,
                        name,
                    )
                if existing is not None:
                    raise commands.BadArgument(
                        "That Twitch channel is already followed here."
                    )
                if guild_id is not None:
                    count = await connection.fetchval(
                        """
                        SELECT COUNT(DISTINCT lower(channel_name))
                        FROM (
                            SELECT channel_name FROM notify_twitch_follows
                            WHERE guild_id = $1
                            UNION ALL
                            SELECT channel_name FROM twitch_follows
                            WHERE guild_id = $1
                        ) follows
                        """,
                        guild_id,
                    )
                else:
                    count = await connection.fetchval(
                        "SELECT COUNT(DISTINCT lower(channel_name)) "
                        "FROM notify_twitch_follows WHERE user_id = $1",
                        user_id,
                    )
                if int(count or 0) >= 10:
                    raise commands.BadArgument(
                        "You can follow up to 10 Twitch channels per server or DM."
                    )
                # Mention columns are intentionally omitted here so the
                # database defaults (disabled) always apply to new follows.
                inserted = await connection.execute(
                    "INSERT INTO notify_twitch_follows "
                    "(guild_id, user_id, channel_name, broadcaster_id, announce_channel_id) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    guild_id,
                    user_id,
                    name,
                    str(twitch_user["id"]),
                    announce_channel_id,
                )
                # asyncpg returns ``INSERT 0 0`` when the unique scope key
                # rejected the row (the ``ON CONFLICT`` path).
                if inserted.endswith(" 0"):
                    raise commands.BadArgument(
                        "That Twitch channel is already followed here."
                    )
        events = self._events()
        if events is not None and hasattr(
            events, "ensure_twitch_eventsub_subscription"
        ):
            await events.ensure_twitch_eventsub_subscription(str(twitch_user["id"]))
        await self._send(
            ctx,
            view=NotifyView("## Twitch notifications", f"Following **{name}**."),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_add.command(name="twitch")
    @app_commands.describe(channel="Twitch channel name or Twitch URL.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_add_twitch(self, ctx: Context, *, channel: str) -> None:
        """Follow a Twitch channel in this server or by DM."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._add_twitch(ctx, channel)

    @notify_add_twitch.autocomplete("channel")
    async def notify_add_twitch_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._twitch_search_choices(interaction, current)

    @notify.command(name="twitch")
    @app_commands.describe(channel="Twitch channel name or Twitch URL.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_twitch(self, ctx: Context, *, channel: str) -> None:
        """Follow a Twitch channel (shorthand for ``notify add twitch``)."""

        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._add_twitch(ctx, channel)

    @notify_twitch.autocomplete("channel")
    async def notify_twitch_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._twitch_search_choices(interaction, current)

    async def _add_anime(self, ctx: Context, anime: str) -> None:
        """Resolve and persist one AniList anime follow."""

        self._require_server_admin(ctx)
        anime = self._normalize_anime_query(anime)
        media = await self._anilist_lookup(anime)
        if not media or str(media.get("type", "ANIME")).upper() != "ANIME":
            raise commands.BadArgument(f"Could not find an anime for **{anime}**.")
        media_id = int(media.get("id") or 0)
        title = media_title(media)
        if not media_id or not title:
            raise commands.BadArgument("AniList did not return a usable anime.")

        now = datetime.datetime.now(datetime.timezone.utc)
        next_airing, episode, release = anilist_notification_schedule(media, now)

        # Search results often represent a completed season whose successor is
        # the entry that actually has an upcoming airing date.  Never switch a
        # follow silently: offer the successor and retain the originally
        # requested title when the user declines or the prompt times out.
        if next_airing is None and release is None:
            successor = await self._find_upcoming_successor(media, now)
            successor_id = successor.get("id") if successor else None
            successor_title = media_title(successor) if successor else ""
            if successor:
                successor_airing, successor_episode, successor_release = (
                    anilist_notification_schedule(successor, now)
                )
            else:
                successor_airing = successor_release = None
                successor_episode = None
            if (
                successor_id
                and successor_title
                and (successor_airing or successor_release)
            ):
                if successor_airing is not None:
                    schedule_text = (
                        f"with the episode {successor_episode or 'next'} airing in "
                        f"{discord.utils.format_dt(successor_airing, 'R')}"
                    )
                else:
                    schedule_text = (
                        "with the release date of "
                        f"{discord.utils.format_dt(successor_release)}"
                    )
                prompt = (
                    f"Did you mean **{discord.utils.escape_markdown(successor_title)}** "
                    f"{schedule_text}?"
                )
                confirmed = await ctx.prompt(
                    prompt,
                    confirm_label="Yes",
                    cancel_label="No",
                    ephemeral=ctx.interaction is not None,
                )
                if confirmed:
                    # ``_anilist_lookup`` accepts AniList URLs for ID lookups;
                    # use one here instead of issuing a title search for the
                    # numeric relation ID.
                    successor_media = await self._anilist_lookup(
                        f"https://anilist.co/anime/{successor_id}"
                    )
                    if successor_media:
                        media = successor_media
                    else:
                        # The relation node has enough data for a valid follow
                        # even if AniList temporarily omits the ID lookup.
                        media = successor
                    media_id = int(media.get("id") or successor_id)
                    title = media_title(media) or successor_title
                    next_airing, episode, release = anilist_notification_schedule(
                        media, now
                    )

        # Do not create a follow that can never produce a notification.  This
        # also protects against declining the successor prompt for a finished
        # season: users must explicitly choose an upcoming entry instead.
        if next_airing is None and release is None:
            raise commands.BadArgument(
                "That anime has finished and has no upcoming release or episode."
            )
        guild_id, user_id = _scope(ctx)
        predicate, scope_id = _scope_predicate(guild_id, user_id)
        destination = int(ctx.channel.id) if guild_id is not None else None
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"notify:anime:{scope_id}",
                )
                existing = await connection.fetchval(
                    f"SELECT id FROM notify_anime_follows WHERE {predicate} "
                    "AND anilist_id = $2",
                    scope_id,
                    media_id,
                )
                if existing is not None:
                    raise commands.BadArgument("That anime is already followed here.")
                count = await connection.fetchval(
                    f"SELECT COUNT(DISTINCT anilist_id) FROM notify_anime_follows WHERE {predicate}",
                    scope_id,
                )
                if int(count or 0) >= 20:
                    raise commands.BadArgument(
                        "You can follow up to 20 anime per server or DM."
                    )
                # Mention columns are intentionally omitted so new follows
                # start with notifications disabled by default.
                inserted = await connection.execute(
                    "INSERT INTO notify_anime_follows "
                    "(guild_id, user_id, anilist_id, title, site_url, banner_url, official_site_url, "
                    "crunchyroll_url, announce_channel_id, release_at, next_airing_at, next_episode, last_checked_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,now()) "
                    "ON CONFLICT DO NOTHING",
                    guild_id,
                    user_id,
                    media_id,
                    title,
                    media.get("siteUrl"),
                    media.get("bannerImage")
                    or (media.get("coverImage") or {}).get("extraLarge"),
                    media_external_link(media, "Official Site"),
                    next(
                        (
                            str(link.get("url"))
                            for link in (media.get("externalLinks") or [])
                            if isinstance(link, dict)
                            and str(link.get("site") or "")
                            .casefold()
                            .startswith("crunchyroll")
                            and str(link.get("url") or "").startswith(
                                ("http://", "https://")
                            )
                        ),
                        None,
                    ),
                    destination,
                    release,
                    next_airing,
                    episode,
                )
                # asyncpg returns ``INSERT 0 0`` when the unique scope key
                # rejected the row (the ``ON CONFLICT`` path).
                if inserted.endswith(" 0"):
                    raise commands.BadArgument("That anime is already followed here.")
        await self._send(
            ctx,
            view=NotifyView(
                "## Anime notifications",
                f"Following **{discord.utils.escape_markdown(title)}**.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_add.command(
        name="anime", extras={"required_permissions": ("manage_guild",)}
    )
    @app_commands.describe(anime="Anime title or AniList anime URL.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_add_anime(self, ctx: Context, *, anime: str) -> None:
        """Follow an anime's next episode or release date."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._add_anime(ctx, anime)

    @notify_add_anime.autocomplete("anime")
    async def notify_add_anime_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._anime_search_choices(interaction, current)

    @notify.command(name="anime", extras={"required_permissions": ("manage_guild",)})
    @app_commands.describe(anime="Anime title, @title, or AniList anime URL.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_anime(self, ctx: Context, *, anime: str) -> None:
        """Follow an anime (shorthand for ``notify add anime``)."""

        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._add_anime(ctx, anime)

    @notify_anime.autocomplete("anime")
    async def notify_anime_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._anime_search_choices(interaction, current)

    @notify.group(
        name="remove",
        aliases=("unfollow",),
        fallback="info",
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_remove(self, ctx: Context) -> None:
        """Remove a Twitch or anime follow."""
        await self._send_info(ctx)

    async def _remove_twitch(self, ctx: Context, channel: str) -> None:
        """Stop following a Twitch channel in this scope."""
        self._require_server_admin(ctx)
        try:
            name = normalize_twitch_channel(channel)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error
        guild_id, user_id = _scope(ctx)
        predicate, scope_id = _scope_predicate(guild_id, user_id)
        broadcaster_id = await self.bot.pool.fetchval(
            f"SELECT broadcaster_id FROM notify_twitch_follows WHERE {predicate} "
            "AND lower(channel_name) = lower($2) LIMIT 1",
            scope_id,
            name,
        )
        if broadcaster_id is None and guild_id is not None:
            broadcaster_id = await self.bot.pool.fetchval(
                "SELECT broadcaster_id FROM twitch_follows "
                "WHERE guild_id = $1 AND lower(channel_name) = lower($2) LIMIT 1",
                guild_id,
                name,
            )
        result = await self.bot.pool.execute(
            f"DELETE FROM notify_twitch_follows WHERE {predicate} AND lower(channel_name) = lower($2)",
            scope_id,
            name,
        )
        # The legacy text command and ``notify`` intentionally share server
        # follows.  Removing a follow through either surface must remove the
        # corresponding legacy row as well, otherwise it would continue to
        # generate notifications after the user unfollowed it.
        legacy_result = "DELETE 0"
        if guild_id is not None:
            legacy_result = await self.bot.pool.execute(
                "DELETE FROM twitch_follows WHERE guild_id = $1 "
                "AND lower(channel_name) = lower($2)",
                guild_id,
                name,
            )
        try:
            removed = int(str(result).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            removed = 0
        try:
            removed += int(str(legacy_result).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            pass
        if not removed:
            raise commands.BadArgument(f"You are not following **{name}**.")
        if broadcaster_id:
            events = self._events()
            if events is not None and hasattr(
                events, "remove_twitch_eventsub_subscription"
            ):
                try:
                    await events.remove_twitch_eventsub_subscription(
                        str(broadcaster_id)
                    )
                except Exception as error:
                    self.bot.logger.warning(
                        "Could not remove Twitch notification subscription for %s: %s",
                        name,
                        error,
                    )
        await self._send(
            ctx,
            view=NotifyView(
                "## Twitch notifications", f"No longer following **{name}**."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_remove.command(name="twitch")
    @app_commands.describe(channel="Twitch channel name or URL to unfollow.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_remove_twitch(self, ctx: Context, *, channel: str) -> None:
        """Stop following a Twitch channel in this scope."""

        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._remove_twitch(ctx, channel)

    @notify_remove_twitch.autocomplete("channel")
    async def notify_remove_twitch_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest followed Twitch channels for the current server or DM."""
        return await self._followed_twitch_choices(interaction, current)

    async def _remove_anime(self, ctx: Context, anime: str) -> None:
        """Stop following an AniList anime in this scope."""
        self._require_server_admin(ctx)
        anime = self._normalize_anime_query(anime)
        media_id = anilist_media_id(anime)
        if media_id is None and anime.isdigit():
            # Long titles cannot fit in an app-command Choice value.  The
            # autocomplete below uses the stable AniList ID in that case.
            media_id = int(anime)
        guild_id, user_id = _scope(ctx)
        predicate, scope_id = _scope_predicate(guild_id, user_id)
        if media_id is None:
            media_id = await self.bot.pool.fetchval(
                f"SELECT anilist_id FROM notify_anime_follows WHERE {predicate} AND lower(title) = lower($2) LIMIT 1",
                scope_id,
                anime.strip(),
            )
        if not media_id:
            raise commands.BadArgument(f"You are not following **{anime}**.")
        result = await self.bot.pool.execute(
            f"DELETE FROM notify_anime_follows WHERE {predicate} AND anilist_id = $2",
            scope_id,
            int(media_id),
        )
        try:
            removed = int(str(result).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            removed = 0
        if not removed:
            raise commands.BadArgument(f"You are not following **{anime}**.")
        await self._send(
            ctx,
            view=NotifyView(
                "## Anime notifications",
                f"No longer following **{discord.utils.escape_markdown(anime)}**.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_remove.command(name="anime")
    @app_commands.describe(anime="Anime title or AniList anime URL to unfollow.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_remove_anime(self, ctx: Context, *, anime: str) -> None:
        """Stop following an AniList anime in this scope."""

        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._remove_anime(ctx, anime)

    @notify_remove_anime.autocomplete("anime")
    async def notify_remove_anime_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest followed anime titles for the current server or DM."""
        return await self._followed_anime_choices(interaction, current)

    @notify.group(name="mention", fallback="info", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_mention(self, ctx: Context) -> None:
        """Set or clear mentions for a followed notification."""
        await self._send_info(ctx)

    async def _set_mention(
        self, ctx: Context, kind: str, entity: str, target: str | None
    ) -> None:
        self._require_server_admin(ctx)
        role_id, everyone, label = _mention_target(ctx, target)
        guild_id, user_id = _scope(ctx)
        predicate, scope_id = _scope_predicate(guild_id, user_id)
        if kind == "twitch":
            try:
                name = normalize_twitch_channel(entity)
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            table = "notify_twitch_follows"
            where = f"{predicate} AND lower(channel_name) = lower($2)"
            params = (scope_id, name)
        else:
            table = "notify_anime_follows"
            media_id = anilist_media_id(entity)
            if media_id is None:
                media_id = await self.bot.pool.fetchval(
                    f"SELECT anilist_id FROM {table} WHERE {predicate} AND lower(title) = lower($2) LIMIT 1",
                    scope_id,
                    entity.strip(),
                )
            if not media_id:
                raise commands.BadArgument(
                    f"I could not find a followed anime named **{entity}**."
                )
            where = f"{predicate} AND anilist_id = $2"
            params = (scope_id, int(media_id))
        result = await self.bot.pool.execute(
            f"UPDATE {table} SET mention_role_id = $3, mention_everyone = $4, updated_at = now() WHERE {where}",
            *params,
            role_id,
            everyone,
        )
        if result.endswith(" 0"):
            raise commands.BadArgument("That notification follow was not found.")
        await self._send(
            ctx,
            view=NotifyView(
                f"## {kind.title()} notifications",
                (
                    f"Mention set to {label}."
                    if label
                    else "Notification mentions cleared."
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_mention.command(name="twitch")
    @app_commands.describe(
        channel="Followed Twitch channel.",
        mention="Role, @everyone, or omit to clear mentions.",
    )
    async def notify_mention_twitch(
        self, ctx: Context, channel: str, mention: str | None = None
    ) -> None:
        """Configure the role or @everyone mention for a Twitch follow."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._set_mention(ctx, "twitch", channel, mention)

    @notify_mention_twitch.autocomplete("channel")
    async def notify_mention_twitch_channel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._followed_twitch_choices(interaction, current)

    @notify_mention_twitch.autocomplete("mention")
    async def notify_mention_twitch_role_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._guild_role_choices(interaction, current)

    @notify_mention.command(name="anime")
    @app_commands.describe(
        anime="Followed anime title or AniList URL.",
        mention="Role, @everyone, or omit to clear mentions.",
    )
    async def notify_mention_anime(
        self, ctx: Context, *, anime: str, mention: str | None = None
    ) -> None:
        """Configure the role or @everyone mention for an anime follow."""
        anime, mention = _split_anime_mention(ctx, anime, mention)
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._set_mention(ctx, "anime", anime, mention)

    @notify_mention_anime.autocomplete("mention")
    async def notify_mention_anime_role_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._guild_role_choices(interaction, current)

    @notify_mention_anime.autocomplete("anime")
    async def notify_mention_anime_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest the anime notifications available in the current scope."""
        return await self._followed_anime_choices(interaction, current)

    @notify.group(name="list", fallback="info", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notify_list(self, ctx: Context) -> None:
        """List followed Twitch channels and anime."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._list_all(ctx)

    async def _fetch_twitch_follows(self, ctx: Context) -> list[Any]:
        """Return Twitch follows for the current server or DM scope."""

        guild_id, user_id = _scope(ctx)
        if guild_id is not None:
            rows = await self.bot.pool.fetch(
                """
                SELECT DISTINCT ON (lower(btrim(channel_name)))
                    channel_name, announce_channel_id, last_offline_at, last_live_at
                FROM (
                    SELECT channel_name, announce_channel_id, last_offline_at,
                           last_live_at, 1 AS source
                    FROM notify_twitch_follows WHERE guild_id = $1
                    UNION ALL
                    SELECT channel_name, announce_channel_id, NULL AS last_offline_at,
                           NULL AS last_live_at, 2 AS source
                    FROM twitch_follows WHERE guild_id = $1
                ) follows
                ORDER BY lower(btrim(channel_name)), source,
                         last_live_at DESC NULLS LAST,
                         last_offline_at DESC NULLS LAST
                """,
                guild_id,
            )
        else:
            rows = await self.bot.pool.fetch(
                """
                SELECT channel_name, announce_channel_id, last_offline_at, last_live_at
                FROM (
                    SELECT DISTINCT ON (lower(btrim(channel_name)))
                        channel_name, announce_channel_id, last_offline_at,
                        last_live_at, updated_at, id
                    FROM notify_twitch_follows
                    WHERE user_id = $1
                    ORDER BY lower(btrim(channel_name)),
                             updated_at DESC NULLS LAST, id DESC
                ) follows
                ORDER BY channel_name
                """,
                user_id,
            )
        return list(rows)

    async def _fetch_anime_follows(self, ctx: Context) -> list[Any]:
        """Return anime follows for the current server or DM scope."""

        guild_id, user_id = _scope(ctx)
        predicate, scope_id = _scope_predicate(guild_id, user_id)
        rows = await self.bot.pool.fetch(
            f"""
            SELECT title, announce_channel_id, release_at, next_airing_at,
                   next_episode
            FROM (
                SELECT DISTINCT ON (anilist_id)
                    title, announce_channel_id, release_at, next_airing_at,
                    next_episode, updated_at, id
                FROM notify_anime_follows
                WHERE {predicate}
                ORDER BY anilist_id, updated_at DESC NULLS LAST, id DESC
            ) follows
            ORDER BY title
            """,
            scope_id,
        )
        return list(rows)

    async def _list_all(self, ctx: Context) -> None:
        """Render both follow types in one panel for ``notify list``."""

        twitch_rows, anime_rows = await asyncio.gather(
            self._fetch_twitch_follows(ctx),
            self._fetch_anime_follows(ctx),
        )
        twitch = twitch_list_details(twitch_rows) or "No Twitch channels followed."
        anime = anime_list_details(anime_rows) or "No anime followed."
        scope_title = (
            f"Notifications for {ctx.guild.name}"
            if ctx.guild is not None
            else "Your Notifications"
        )
        await self._send(
            ctx,
            view=NotifyListView(
                f"## {discord.utils.escape_markdown(scope_title)}",
                (("Twitch", twitch), ("Anime", anime)),
                mention=(
                    f"Remove a notification with `{_notify_prefix(ctx)}notify "
                    "remove twitch/anime <name>`."
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_list.command(name="twitch")
    async def notify_list_twitch(self, ctx: Context) -> None:
        """List Twitch channels followed in this server or DM scope."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            rows = await self._fetch_twitch_follows(ctx)
            await self._send(
                ctx,
                view=NotifyView(
                    "## Followed Twitch channels",
                    twitch_list_details(rows) or "No Twitch channels followed.",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=ctx.interaction is not None,
            )

    @notify_list.command(name="anime")
    async def notify_list_anime(self, ctx: Context) -> None:
        """List anime followed in this server or DM scope."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            rows = await self._fetch_anime_follows(ctx)
            await self._send(
                ctx,
                view=NotifyView(
                    "## Followed anime",
                    anime_list_details(rows) or "No anime followed.",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=ctx.interaction is not None,
            )

    @notify.group(name="channel", fallback="info", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def notify_channel(self, ctx: GuildContext) -> None:
        """Choose channels for followed Twitch channels and anime."""
        await self._send_info(ctx)

    async def _resolve_channel(
        self, ctx: GuildContext, value: str
    ) -> discord.TextChannel:
        raw = value.strip()
        match = re.fullmatch(r"<#(\d{15,25})>|(\d{15,25})", raw)
        channel: discord.abc.GuildChannel | None = None
        if match:
            channel = ctx.guild.get_channel(int(match.group(1) or match.group(2)))
        else:
            channel = next(
                (
                    item
                    for item in ctx.guild.text_channels
                    if item.name.casefold() == raw.removeprefix("#").casefold()
                ),
                None,
            )
        if not isinstance(channel, discord.TextChannel):
            raise commands.BadArgument(
                "That text channel could not be found in this server."
            )
        return channel

    async def _channel_for(
        self, ctx: GuildContext, kind: str, first: str | None, second: str | None
    ) -> None:
        values = [value for value in (first, second) if value and value.strip()]
        # Prefix commands pass the first token to ``channel`` and the rest to
        # the keyword-only entity option.  Rebuild the complete tail so a
        # channel and a multi-word anime can be supplied in either order.
        if ctx.interaction is None:
            tail = _prefix_command_tail(ctx, "notify", "channel", kind)
            if tail:
                tokens = tail.split()
                if len(tokens) == 1:
                    values = [tail]
                else:
                    try:
                        await self._resolve_channel(ctx, tokens[0])
                    except commands.BadArgument:
                        try:
                            await self._resolve_channel(ctx, tokens[-1])
                        except commands.BadArgument:
                            values = [tail]
                        else:
                            values = [" ".join(tokens[:-1]), tokens[-1]]
                    else:
                        values = [tokens[0], " ".join(tokens[1:])]
        if not values:
            await self._send(
                ctx,
                view=NotifyView(
                    f"## {kind.title()} notification channels",
                    "Provide a channel and a followed name.",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=ctx.interaction is not None,
            )
            return
        guild_id = int(ctx.guild.id)
        if len(values) == 1:
            candidate_channel: discord.TextChannel | None = None
            try:
                candidate_channel = await self._resolve_channel(ctx, values[0])
            except commands.BadArgument:
                pass
            if candidate_channel is not None:
                rows = await self.bot.pool.fetch(
                    (
                        "SELECT channel_name, announce_channel_id, last_live_at, "
                        "last_offline_at FROM ("
                        "SELECT DISTINCT ON (lower(btrim(channel_name))) "
                        "channel_name, announce_channel_id, last_live_at, "
                        "last_offline_at, updated_at, id "
                        "FROM notify_twitch_follows "
                        "WHERE guild_id = $1 AND announce_channel_id = $2 "
                        "ORDER BY lower(btrim(channel_name)), "
                        "updated_at DESC NULLS LAST, id DESC"
                        ") follows ORDER BY channel_name"
                        if kind == "twitch"
                        else "SELECT title, announce_channel_id, release_at, next_airing_at, "
                        "next_episode FROM (SELECT DISTINCT ON (anilist_id) "
                        "title, announce_channel_id, release_at, next_airing_at, "
                        "next_episode, updated_at, id FROM notify_anime_follows "
                        "WHERE guild_id = $1 AND announce_channel_id = $2 "
                        "ORDER BY anilist_id, updated_at DESC NULLS LAST, id DESC) follows "
                        "ORDER BY title"
                    ),
                    guild_id,
                    candidate_channel.id,
                )
                details = (
                    twitch_list_details(rows)
                    if kind == "twitch"
                    else anime_list_details(rows)
                )
                await self._send(
                    ctx,
                    view=NotifyView(
                        f"## {kind.title()} notifications in {candidate_channel.mention}",
                        details or "No follows are configured in that channel.",
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=ctx.interaction is not None,
                )
                return
            entity = values[0]
            destination = ctx.channel
        else:
            try:
                destination = await self._resolve_channel(ctx, values[0])
                entity = " ".join(values[1:])
            except commands.BadArgument:
                try:
                    destination = await self._resolve_channel(ctx, values[-1])
                    entity = " ".join(values[:-1])
                except commands.BadArgument:
                    # Anime titles commonly contain spaces.  When neither
                    # edge token is a channel, treat the complete input as
                    # the followed title and use the current channel.
                    if kind != "anime":
                        raise
                    destination = ctx.channel
                    entity = " ".join(values)
        if kind == "twitch":
            try:
                normalized = normalize_twitch_channel(entity)
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            row = await self.bot.pool.fetchrow(
                "SELECT channel_name, broadcaster_id, mention_role_id, mention_everyone FROM notify_twitch_follows WHERE guild_id = $1 AND lower(channel_name) = lower($2) ORDER BY id LIMIT 1",
                guild_id,
                normalized,
            )
            key = normalized
        else:
            media_id = anilist_media_id(entity)
            if media_id is None:
                row = await self.bot.pool.fetchrow(
                    "SELECT anilist_id, title, site_url, banner_url, official_site_url, crunchyroll_url, mention_role_id, mention_everyone, release_at, next_airing_at, next_episode FROM notify_anime_follows WHERE guild_id = $1 AND lower(title) = lower($2) ORDER BY id LIMIT 1",
                    guild_id,
                    entity.strip(),
                )
            else:
                row = await self.bot.pool.fetchrow(
                    "SELECT anilist_id, title, site_url, banner_url, official_site_url, crunchyroll_url, mention_role_id, mention_everyone, release_at, next_airing_at, next_episode FROM notify_anime_follows WHERE guild_id = $1 AND anilist_id = $2 ORDER BY id LIMIT 1",
                    guild_id,
                    media_id,
                )
            key = row["anilist_id"] if row else None
        if row is None:
            raise commands.BadArgument("That followed notification could not be found.")
        if not await ctx.prompt(
            f"Send {kind} notifications for **{entity}** to {destination.mention}?",
            ephemeral=ctx.interaction is not None,
        ):
            return
        if kind == "twitch":
            result = await self.bot.pool.execute(
                "UPDATE notify_twitch_follows SET announce_channel_id = $3, "
                "broadcaster_id = COALESCE($4, broadcaster_id), updated_at = now() "
                "WHERE guild_id = $1 AND lower(btrim(channel_name)) = lower(btrim($2))",
                guild_id,
                key,
                destination.id,
                row["broadcaster_id"],
            )
            # Legacy ``twitch`` follows share the same subscription.  Keep
            # their destination in sync so moving a notify follow cannot
            # result in an alert in both the old and new channel.
            await self.bot.pool.execute(
                "UPDATE twitch_follows SET announce_channel_id = $3, "
                "broadcaster_id = COALESCE($4, broadcaster_id) "
                "WHERE guild_id = $1 AND lower(btrim(channel_name)) = lower(btrim($2))",
                guild_id,
                key,
                destination.id,
                row["broadcaster_id"],
            )
        else:
            result = await self.bot.pool.execute(
                "UPDATE notify_anime_follows SET announce_channel_id = $3, "
                "updated_at = now() WHERE guild_id = $1 AND anilist_id = $2",
                guild_id,
                key,
                destination.id,
            )
        # During the migration window an old installation may still have
        # duplicate rows.  Updating more than one row is still a successful
        # route change; only an update count of zero means the follow vanished.
        if result.endswith(" 0"):
            raise commands.BadArgument("That followed notification no longer exists.")
        await self._send(
            ctx,
            view=NotifyView(
                f"## {kind.title()} notifications",
                f"{entity} will now notify in {destination.mention}.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @notify_channel.command(name="twitch")
    @app_commands.describe(
        channel="Channel name, ID, or mention; can be omitted when using the current channel.",
        twitch="Followed Twitch channel name.",
    )
    async def notify_channel_twitch(
        self,
        ctx: GuildContext,
        channel: str | None = None,
        *,
        twitch: str | None = None,
    ) -> None:
        """Route a followed Twitch channel to a server text channel."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._channel_for(ctx, "twitch", channel, twitch)

    @notify_channel_twitch.autocomplete("channel")
    async def notify_channel_twitch_channel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._guild_channel_choices(interaction, current)

    @notify_channel_twitch.autocomplete("twitch")
    async def notify_channel_twitch_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._followed_twitch_choices(interaction, current)

    @notify_channel.command(name="anime")
    @app_commands.describe(
        channel="Channel name, ID, or mention; can be omitted when using the current channel.",
        anime="Followed anime title.",
    )
    async def notify_channel_anime(
        self, ctx: GuildContext, channel: str | None = None, *, anime: str | None = None
    ) -> None:
        """Route a followed anime to a server text channel."""
        async with ctx.typing(ephemeral=ctx.interaction is not None):
            await self._channel_for(ctx, "anime", channel, anime)

    @notify_channel_anime.autocomplete("channel")
    async def notify_channel_anime_channel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._guild_channel_choices(interaction, current)

    @notify_channel_anime.autocomplete("anime")
    async def notify_channel_anime_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._followed_anime_choices(interaction, current)
