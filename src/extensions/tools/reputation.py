from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from core import Cog
from core.cache import REPUTATION_BONUS_GUILD_ID, REPUTATION_BONUS_USER_ID
from utils.formats import plural

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


TATSU_ID = 172002275412279296
TATSU_REPUTATION_ICON_ID = 745225325063176252
TATSU_REPUTATION_PATTERN = re.compile(
    rf"<:Reputation_Icon:{TATSU_REPUTATION_ICON_ID}>\s*\*\*"
    r"(?P<giver>.+?)\s+has given\s+<@!?(?P<receiver>\d+)>",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def user_period_start(now: datetime) -> date:
    """Return the UTC calendar day used for a user reputation limit."""

    return now.date()


def guild_period_start(now: datetime) -> date:
    """Return the Sunday-starting UTC week used for a guild limit."""

    day = now.date()
    return day - timedelta(days=(now.weekday() + 1) % 7)


def next_user_reset(now: datetime) -> datetime:
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)


def next_guild_reset(now: datetime) -> datetime:
    start = guild_period_start(now)
    return datetime.combine(
        start + timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc
    )


class Reputation(Cog):
    """Fishie reputation commands and Tatsu reputation imports."""

    @staticmethod
    def _same_name(member: discord.Member, value: str) -> bool:
        wanted = value.strip().casefold()
        # Tatsu's text response puts the giver's username before "has given".
        # Do not use display names or global names here because they are not
        # guaranteed to identify the same account.
        return member.name.casefold() == wanted

    async def _resolve_reputation_guild(
        self, ctx: Context, value: str
    ) -> discord.Guild | None:
        normalized = value.strip().casefold()
        if normalized in {"server", "guild", "this server", "this guild"}:
            return ctx.guild
        if normalized.isdigit():
            return self.bot.get_guild(int(normalized))
        matches = [
            guild
            for guild in self.bot.guilds
            if guild.name.casefold() == normalized
        ]
        return matches[0] if len(matches) == 1 else None

    async def _record_event(
        self,
        *,
        giver_id: int,
        receiver_id: int | None,
        guild_id: int | None,
        kind: Literal["user", "guild"],
        source: Literal["fishie", "tatsu"],
        period_start: date,
        source_message_id: int | None = None,
        comment: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[bool, int]:
        """Insert an event and update the legacy aggregate atomically."""

        created_at = created_at or utc_now()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                if source == "fishie":
                    row = await connection.fetchrow(
                        """
                        INSERT INTO reputation_events(
                            giver_id, receiver_id, guild_id, kind, source,
                            source_message_id, period_start, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (giver_id, kind, period_start)
                            WHERE source = 'fishie' DO NOTHING
                        RETURNING id
                        """,
                        giver_id,
                        receiver_id,
                        guild_id,
                        kind,
                        source,
                        source_message_id,
                        period_start,
                        created_at,
                    )
                else:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO reputation_events(
                            giver_id, receiver_id, guild_id, kind, source,
                            source_message_id, period_start, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (source_message_id)
                            WHERE source_message_id IS NOT NULL DO NOTHING
                        RETURNING id
                        """,
                        giver_id,
                        receiver_id,
                        guild_id,
                        kind,
                        source,
                        source_message_id,
                        period_start,
                        created_at,
                    )

                if row is None:
                    return False, 0

                if kind == "user":
                    assert receiver_id is not None
                    await connection.execute(
                        """
                        INSERT INTO user_rep(user_id, count)
                        VALUES ($1, 1)
                        ON CONFLICT (user_id) DO UPDATE
                        SET count = user_rep.count + 1
                        """,
                        receiver_id,
                    )
                    if source == "tatsu":
                        await connection.execute(
                            """
                            INSERT INTO user_rep_logs(
                                user_id, author_id, value, comment, guild_id,
                                source_message_id, created_at
                            ) VALUES ($1, $2, TRUE, $3, $4, $5, $6)
                            ON CONFLICT (source_message_id) DO NOTHING
                            """,
                            receiver_id,
                            giver_id,
                            comment,
                            guild_id,
                            source_message_id,
                            created_at,
                        )
                    count = await connection.fetchval(
                        "SELECT count FROM user_rep WHERE user_id = $1",
                        receiver_id,
                    )
                else:
                    assert guild_id is not None
                    await connection.execute(
                        """
                        INSERT INTO guild_rep(guild_id, count)
                        VALUES ($1, 1)
                        ON CONFLICT (guild_id) DO UPDATE
                        SET count = guild_rep.count + 1
                        """,
                        guild_id,
                    )
                    count = await connection.fetchval(
                        "SELECT count FROM guild_rep WHERE guild_id = $1",
                        guild_id,
                    )

        # Keep XP bonus eligibility in memory after the event is committed.
        # Fishie and Tatsu events use the same sets, so a giver can receive
        # both user and guild bonuses in the same period.
        if kind == "user" and receiver_id == REPUTATION_BONUS_USER_ID:
            self.bot.db_cache.add_reputation_user_bonus(giver_id, source)
        elif kind == "guild" and guild_id == REPUTATION_BONUS_GUILD_ID:
            self.bot.db_cache.add_reputation_guild_bonus(giver_id, source)

        return True, int(count or 0)

    @commands.hybrid_command(name="reputation", aliases=("rep",))
    @app_commands.describe(
        target="A user to rep, or `server`/`guild` to rep this server."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reputation(self, ctx: Context, *, target: str | None = None) -> None:
        """Give one user reputation each UTC day or one guild each UTC week."""

        if not target:
            raise commands.BadArgument(
                "Mention a user, or use `server` to give this server reputation."
            )

        now = utc_now()
        guild = await self._resolve_reputation_guild(ctx, target)
        if guild is not None or target.strip().casefold() in {
            "server",
            "guild",
            "this server",
            "this guild",
        }:
            if guild is None:
                raise commands.BadArgument("This command needs to be used in a server.")
            accepted, count = await self._record_event(
                giver_id=ctx.author.id,
                receiver_id=None,
                guild_id=guild.id,
                kind="guild",
                source="fishie",
                period_start=guild_period_start(now),
                source_message_id=getattr(getattr(ctx, "message", None), "id", None),
            )
            if not accepted:
                await ctx.send(
                    f"You already gave this server reputation this week. "
                    f"You can rep it again {discord.utils.format_dt(next_guild_reset(now), 'R')}.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await ctx.send(
                f"{escape_markdown(ctx.author.name)} gave "
                f"**{escape_markdown(guild.name)}** a reputation point.\n"
                f"-# *This server now has {plural(count):point}.*",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            user = await commands.UserConverter().convert(ctx, target)
        except commands.BadArgument as exc:
            raise commands.BadArgument("Could not find that user or server.") from exc
        if user.id == ctx.author.id:
            raise commands.BadArgument("You cannot give reputation to yourself.")

        accepted, count = await self._record_event(
            giver_id=ctx.author.id,
            receiver_id=user.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            kind="user",
            source="fishie",
            period_start=user_period_start(now),
            source_message_id=getattr(getattr(ctx, "message", None), "id", None),
        )
        if not accepted:
            await ctx.send(
                "You already gave a user reputation today. "
                f"You can rep someone again {discord.utils.format_dt(next_user_reset(now), 'R')}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            f"{escape_markdown(ctx.author.name)} gave "
            f"**{escape_markdown(user.name)}** a reputation point.\n"
            f"-# *They now have {plural(count):point}.*",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener("on_message")
    async def on_tatsu_reputation(self, message: discord.Message) -> None:
        """Import Tatsu reputation responses without trusting message text alone."""

        if message.author.id != TATSU_ID or message.guild is None:
            return
        match = TATSU_REPUTATION_PATTERN.search(message.content or "")
        if match is None:
            return

        receiver_id = int(match.group("receiver"))
        interaction_metadata = getattr(message, "interaction_metadata", None)
        interaction_user = getattr(interaction_metadata, "user", None)
        if interaction_user is None:
            interaction = getattr(message, "interaction", None)
            interaction_user = getattr(interaction, "user", None)
        giver_id = getattr(interaction_user, "id", None)

        if giver_id is None:
            matches = [
                member
                for member in message.guild.members
                if self._same_name(member, match.group("giver"))
            ]
            # A text response does not contain a giver ID.  Never guess when
            # duplicate names make the import ambiguous.
            if len(matches) != 1:
                self.bot.logger.debug(
                    "Skipping Tatsu reputation %s: could not resolve giver %r",
                    message.id,
                    match.group("giver"),
                )
                return
            giver_id = matches[0].id

        if int(giver_id) == receiver_id:
            return
        await self._record_event(
            giver_id=int(giver_id),
            receiver_id=receiver_id,
            guild_id=message.guild.id,
            kind="user",
            source="tatsu",
            period_start=user_period_start(message.created_at),
            source_message_id=message.id,
            comment=message.content,
            created_at=message.created_at,
        )
