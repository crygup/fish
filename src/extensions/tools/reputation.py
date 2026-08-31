from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from core import Cog, is_operational_guild
from core.cache import REPUTATION_BONUS_GUILD_ID, REPUTATION_BONUS_USER_ID
from core.currency import MAX_COIN_BALANCE
from core.handoff import is_legacy_instance
from utils.formats import plural

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


TATSU_ID = 172002275412279296
REPUTATION_USER_BONUS_COINS = 500
REPUTATION_GUILD_BONUS_COINS = 1_000
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


def reputation_bonus_amount(
    *,
    kind: Literal["user", "guild"],
    receiver_id: int | None,
    guild_id: int | None,
    source: Literal["fishie", "tatsu"],
) -> int:
    """Return the Coins awarded for a configured reputation target."""

    if kind == "user" and receiver_id == REPUTATION_BONUS_USER_ID:
        return REPUTATION_USER_BONUS_COINS
    if kind == "guild" and guild_id == REPUTATION_BONUS_GUILD_ID and source == "fishie":
        return REPUTATION_GUILD_BONUS_COINS
    return 0


class Reputation(Cog):
    """Fishie reputation commands and Tatsu reputation imports."""

    async def cog_load(self) -> None:
        if is_legacy_instance(self.bot):
            self.bot.logger.info(
                "Skipping reputation repair task on legacy bot instance"
            )
            return
        # A previous version could record a reputation event or its ledger
        # transaction without applying the matching wallet update. Repair
        # active-day/week events, plus older rows that carry the signature of
        # that known wallet-creation bug; do not replay ordinary old history.
        self._reputation_repair_task = asyncio.create_task(
            self._repair_uncredited_bonus_events()
        )

    def cog_unload(self) -> None:
        task = getattr(self, "_reputation_repair_task", None)
        if task is not None:
            task.cancel()

    async def _repair_uncredited_bonus_events(self) -> None:
        await self.bot.wait_until_ready()
        now = utc_now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=(day_start.weekday() + 1) % 7)
        try:
            rows = await self.bot.pool.fetch(
                """
                SELECT re.id, re.giver_id, re.receiver_id, re.guild_id, re.kind,
                       re.source, re.source_message_id, re.period_start,
                       re.created_at
                FROM reputation_events AS re
                WHERE (
                    (re.kind = 'user'
                     AND re.receiver_id = $1
                     AND re.created_at >= $2)
                    OR
                    (re.kind = 'guild'
                     AND re.guild_id = $3
                     AND re.source = 'fishie'
                     AND re.created_at >= $4)
                    OR EXISTS (
                        SELECT 1
                        FROM currency_transactions AS ct
                        JOIN currency_wallets AS cw ON cw.user_id = ct.user_id
                        WHERE ct.user_id = re.giver_id
                          AND ct.reference_key =
                              'reputation:' || re.id::text || ':coins'
                          AND cw.created_at = ct.created_at
                    )
                )
                ORDER BY re.created_at
                """,
                REPUTATION_BONUS_USER_ID,
                day_start,
                REPUTATION_BONUS_GUILD_ID,
                week_start,
            )
        except Exception:
            self.bot.logger.exception("Could not repair reputation wallet bonuses")
            return

        repaired = 0
        for row in rows:
            try:
                _accepted, _count, coins_added = await self._record_event(
                    giver_id=int(row["giver_id"]),
                    receiver_id=(
                        int(row["receiver_id"])
                        if row["receiver_id"] is not None
                        else None
                    ),
                    guild_id=(
                        int(row["guild_id"]) if row["guild_id"] is not None else None
                    ),
                    kind=str(row["kind"]),  # type: ignore[arg-type]
                    source=str(row["source"]),  # type: ignore[arg-type]
                    period_start=row["period_start"],
                    source_message_id=(
                        int(row["source_message_id"])
                        if row["source_message_id"] is not None
                        else None
                    ),
                    created_at=row["created_at"],
                )
            except Exception:
                self.bot.logger.exception(
                    "Could not repair reputation event id=%s", row["id"]
                )
                continue
            if coins_added:
                repaired += 1
                self.bot.logger.info(
                    "Repaired %s Coins for reputation event giver=%s event=%s",
                    coins_added,
                    row["giver_id"],
                    row["id"],
                )
        if repaired:
            self.bot.logger.info(
                "Repaired %d missing reputation wallet bonus transaction(s)", repaired
            )

    @staticmethod
    def _same_name(member: discord.Member, value: str) -> bool:
        wanted = value.strip().strip("`").casefold()
        # Tatsu's text response puts the giver's username before "has given".
        # Do not use display names or global names here because they are not
        # guaranteed to identify the same account.
        return member.name.casefold() == wanted

    async def _resolve_tatsu_giver(
        self, guild: discord.Guild, value: str
    ) -> discord.Member | None:
        """Resolve the username Tatsu puts in a text response.

        ``Guild.members`` is not guaranteed to contain every member when a
        guild has not finished chunking (and it can be stale after a restart),
        so query Discord when the local cache cannot resolve the username.
        The exact username is required; display names are deliberately not
        considered because they are not unique.
        """

        wanted = value.strip().strip("`")

        def exact(members: list[discord.Member], name: str) -> list[discord.Member]:
            return [member for member in members if self._same_name(member, name)]

        matches = exact(guild.members, wanted)
        if len(matches) == 1:
            return matches[0]

        try:
            queried = await guild.query_members(query=wanted, limit=100, cache=True)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            queried = []
        matches = exact(queried, wanted)
        if len(matches) == 1:
            return matches[0]

        # Some older Tatsu responses put a sentence-ending period after the
        # username.  Prefer an exact match above, then retry without that
        # punctuation only when necessary.
        if wanted.endswith("."):
            trimmed = wanted[:-1].rstrip()
            matches = exact(guild.members, trimmed)
            if len(matches) != 1:
                try:
                    queried = await guild.query_members(
                        query=trimmed, limit=100, cache=True
                    )
                except (
                    discord.Forbidden,
                    discord.HTTPException,
                    TypeError,
                    ValueError,
                ):
                    queried = []
                matches = exact(queried, trimmed)
            if len(matches) == 1:
                return matches[0]
        return None

    @staticmethod
    def _interaction_user_id(message: discord.Message) -> int | None:
        """Return the user attached to a Tatsu slash interaction, if present."""

        metadata = getattr(message, "interaction_metadata", None)
        interaction = getattr(message, "interaction", None)
        for payload in (metadata, interaction):
            if payload is None:
                continue
            if isinstance(payload, Mapping):
                user = payload.get("user")
            else:
                user = getattr(payload, "user", None)
            if user is None:
                continue
            raw_id = (
                user.get("id")
                if isinstance(user, Mapping)
                else getattr(user, "id", None)
            )
            try:
                if raw_id is not None:
                    return int(raw_id)
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    async def _resolve_reputation_guild(
        self, ctx: Context, value: str
    ) -> discord.Guild | None:
        normalized = value.strip().casefold()
        if normalized in {"server", "guild", "this server", "this guild"}:
            return ctx.guild
        if normalized.isdigit():
            return self.bot.get_guild(int(normalized))
        matches = [
            guild for guild in self.bot.guilds if guild.name.casefold() == normalized
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
    ) -> tuple[bool, int, int]:
        """Insert an event, aggregate it, and award any reputation bonus.

        The event insert is the idempotency boundary.  When an event already
        exists (for example, it was imported before wallet bonuses were
        enabled), we still check for its bonus transaction and repair a
        missing credit without incrementing the reputation aggregate again.
        """

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
                        RETURNING id, giver_id, receiver_id, guild_id, kind, source
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
                        RETURNING id, giver_id, receiver_id, guild_id, kind, source
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

                event_created = row is not None
                if row is None:
                    # PostgreSQL's partial unique-index conflict target does
                    # not return the existing row. Fetch it so a previously
                    # recorded event can still receive a missing bonus.
                    if source == "fishie":
                        row = await connection.fetchrow(
                            """
                            SELECT id, giver_id, receiver_id, guild_id, kind, source
                            FROM reputation_events
                            WHERE giver_id = $1
                              AND kind = $2
                              AND period_start = $3
                              AND source = 'fishie'
                            """,
                            giver_id,
                            kind,
                            period_start,
                        )
                    elif source_message_id is not None:
                        row = await connection.fetchrow(
                            """
                            SELECT id, giver_id, receiver_id, guild_id, kind, source
                            FROM reputation_events
                            WHERE source = 'tatsu'
                              AND source_message_id = $1
                            """,
                            source_message_id,
                        )
                    if row is None:
                        self.bot.logger.warning(
                            "Reputation event conflict had no existing row: "
                            "source=%s giver=%s kind=%s period=%s message=%s",
                            source,
                            giver_id,
                            kind,
                            period_start,
                            source_message_id,
                        )
                        return False, 0, 0

                event_id = int(row["id"])
                event_giver_id = int(row["giver_id"])
                event_kind = str(row["kind"])
                event_source = str(row["source"])
                event_receiver_id = row["receiver_id"]
                event_guild_id = row["guild_id"]

                if event_created and event_kind == "user":
                    assert event_receiver_id is not None
                    await connection.execute(
                        """
                        INSERT INTO user_rep(user_id, count)
                        VALUES ($1, 1)
                        ON CONFLICT (user_id) DO UPDATE
                        SET count = user_rep.count + 1
                        """,
                        event_receiver_id,
                    )
                    if event_source == "tatsu":
                        await connection.execute(
                            """
                            INSERT INTO user_rep_logs(
                                user_id, author_id, value, comment, guild_id,
                                source_message_id, created_at
                            ) VALUES ($1, $2, TRUE, $3, $4, $5, $6)
                            -- The source-message index is partial on older
                            -- installations.  Omitting the conflict target
                            -- lets PostgreSQL handle that index without
                            -- requiring a specific index predicate here.
                            ON CONFLICT DO NOTHING
                            """,
                            event_receiver_id,
                            event_giver_id,
                            comment,
                            guild_id,
                            source_message_id,
                            created_at,
                        )
                elif event_created:
                    assert event_guild_id is not None
                    await connection.execute(
                        """
                        INSERT INTO guild_rep(guild_id, count)
                        VALUES ($1, 1)
                        ON CONFLICT (guild_id) DO UPDATE
                        SET count = guild_rep.count + 1
                        """,
                        event_guild_id,
                    )
                if event_kind == "user":
                    count = await connection.fetchval(
                        "SELECT count FROM user_rep WHERE user_id = $1",
                        event_receiver_id,
                    )
                else:
                    count = await connection.fetchval(
                        "SELECT count FROM guild_rep WHERE guild_id = $1",
                        event_guild_id,
                    )

                bonus_amount = reputation_bonus_amount(
                    kind=event_kind,  # type: ignore[arg-type]
                    receiver_id=(
                        int(event_receiver_id)
                        if event_receiver_id is not None
                        else None
                    ),
                    guild_id=(
                        int(event_guild_id) if event_guild_id is not None else None
                    ),
                    source=event_source,  # type: ignore[arg-type]
                )

                coins_added = 0
                if bonus_amount:
                    reference_key = f"reputation:{event_id}:coins"
                    await connection.execute(
                        """
                        INSERT INTO currency_wallets(user_id, balance)
                        VALUES ($1, 0)
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        event_giver_id,
                    )
                    wallet = await connection.fetchrow(
                        """
                        SELECT balance, created_at
                        FROM currency_wallets
                        WHERE user_id = $1
                        FOR UPDATE
                        """,
                        event_giver_id,
                    )
                    existing_transaction = await connection.fetchrow(
                        """
                        SELECT id, amount, created_at
                        FROM currency_transactions
                        WHERE user_id = $1 AND reference_key = $2
                        """,
                        event_giver_id,
                        reference_key,
                    )
                    if existing_transaction is None:
                        if (
                            wallet is not None
                            and int(wallet["balance"])
                            <= MAX_COIN_BALANCE - bonus_amount
                        ):
                            await connection.execute(
                                """
                                UPDATE currency_wallets
                                SET balance = balance + $2, updated_at = now()
                                WHERE user_id = $1
                                """,
                                event_giver_id,
                                bonus_amount,
                            )
                            await connection.execute(
                                """
                                INSERT INTO currency_transactions(
                                    user_id, amount, source, reference_key
                                ) VALUES ($1, $2, $3, $4)
                                """,
                                event_giver_id,
                                bonus_amount,
                                f"reputation_{event_source}",
                                reference_key,
                            )
                            coins_added = bonus_amount
                    elif wallet is not None and (
                        wallet["created_at"] == existing_transaction["created_at"]
                    ):
                        # Early reputation-credit code could insert the
                        # transaction while creating a new wallet but forget
                        # to update that wallet's balance.  The matching
                        # creation timestamps identify that old path. Reconcile
                        # only an under-credited wallet against its immutable
                        # ledger; never reduce a balance that is already higher.
                        ledger_balance = await connection.fetchval(
                            """
                            SELECT COALESCE(SUM(amount), 0)
                            FROM currency_transactions
                            WHERE user_id = $1
                            """,
                            event_giver_id,
                        )
                        current_balance = int(wallet["balance"])
                        missing_balance = int(ledger_balance or 0) - current_balance
                        if 0 < missing_balance <= (MAX_COIN_BALANCE - current_balance):
                            await connection.execute(
                                """
                                UPDATE currency_wallets
                                SET balance = balance + $2, updated_at = now()
                                WHERE user_id = $1
                                """,
                                event_giver_id,
                                missing_balance,
                            )
                            coins_added = missing_balance

        # Keep XP bonus eligibility in memory after the event is committed.
        # Fishie and Tatsu events use the same sets, so a giver can receive
        # both user and guild bonuses in the same period.
        if event_kind == "user" and event_receiver_id == REPUTATION_BONUS_USER_ID:
            self.bot.db_cache.add_reputation_user_bonus(event_giver_id, event_source)
        elif event_kind == "guild" and event_guild_id == REPUTATION_BONUS_GUILD_ID:
            self.bot.db_cache.add_reputation_guild_bonus(event_giver_id, event_source)

        return event_created, int(count or 0), coins_added

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
            accepted, count, coins_added = await self._record_event(
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
                f"-# *This server now has {plural(count):point}.*"
                + (
                    f" | *{coins_added:,} Coins added to your wallet*"
                    if coins_added
                    else ""
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            user = await commands.UserConverter().convert(ctx, target)
        except commands.BadArgument as exc:
            raise commands.BadArgument("Could not find that user or server.") from exc
        if user.id == ctx.author.id:
            raise commands.BadArgument("You cannot give reputation to yourself.")

        accepted, count, coins_added = await self._record_event(
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
            f"-# *They now have {plural(count):point}.*"
            + (
                f" | *{coins_added:,} Coins added to your wallet*"
                if coins_added
                else ""
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener("on_message")
    async def on_tatsu_reputation(self, message: discord.Message) -> None:
        """Import Tatsu reputation responses without trusting message text alone."""

        if is_legacy_instance(self.bot):
            return
        if (
            message.author.id != TATSU_ID
            or message.guild is None
            or is_operational_guild(message)
        ):
            return
        match = TATSU_REPUTATION_PATTERN.search(message.content or "")
        if match is None:
            return

        receiver_id = int(match.group("receiver"))
        giver_id = self._interaction_user_id(message)

        if giver_id is None:
            # A text response does not contain a giver ID.  Never guess when
            # duplicate names make the import ambiguous.
            giver = await self._resolve_tatsu_giver(message.guild, match.group("giver"))
            if giver is None:
                self.bot.logger.debug(
                    "Skipping Tatsu reputation %s: could not resolve giver %r",
                    message.id,
                    match.group("giver"),
                )
                return
            giver_id = giver.id

        if int(giver_id) == receiver_id:
            return
        try:
            _accepted, _count, coins_added = await self._record_event(
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
        except Exception:
            self.bot.logger.exception(
                "Failed to import Tatsu reputation message=%s giver=%s receiver=%s",
                message.id,
                giver_id,
                receiver_id,
            )
            return
        self.bot.logger.info(
            "Imported Tatsu reputation message=%s giver=%s receiver=%s coins_added=%s",
            message.id,
            giver_id,
            receiver_id,
            coins_added,
        )
