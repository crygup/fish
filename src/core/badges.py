"""Durable badge ownership and catalog helpers.

The owner-managed ``user_badges`` rows predate the shop and achievement
systems.  This service deliberately keeps those rows intact while marking
new rows with ``badge_source`` and ``catalog_key``.  Stat badges can therefore
be reconciled idempotently and purchased badges can be revoked with a 50%
refund when their source emoji is no longer available.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .currency import InsufficientFunds

MAX_BADGE_PRICE = 9_000_000_000_000_000_000


class BadgeError(Exception):
    """Base error for expected badge operations."""


class BadgeAlreadyOwned(BadgeError):
    """Raised when a user already owns an active badge key."""


class BadgeNotFound(BadgeError):
    """Raised when a requested badge or ownership row does not exist."""


@dataclass(frozen=True, slots=True)
class BadgeRevocation:
    """Result of revoking an owned badge."""

    badge: Any
    refund_amount: int


@dataclass(frozen=True, slots=True)
class BadgePurchase:
    """Purchase result with the resulting wallet balance."""

    badge: Any
    wallet_balance: int

    @property
    def badge_display(self) -> str:
        emoji = str(_row_value(self.badge, "emoji_name", ""))
        text = str(_row_value(self.badge, "text", ""))
        return f"{emoji} {text}".strip()

    def __getitem__(self, key: str) -> Any:
        if key == "wallet_balance":
            return self.wallet_balance
        if key == "badge_display":
            return self.badge_display
        return _row_value(self.badge, key)


@dataclass(frozen=True, slots=True)
class StatBadge:
    """A derived badge assignment from a current leaderboard."""

    user_id: int
    badge_key: str
    emoji_name: str
    text: str
    value: int


STAT_BADGE_KEYS = frozenset(
    {
        "stat:corn_receiver",
        "stat:connectfour_hard_winner",
        "stat:command_user",
        "stat:richest",
    }
)
STAT_BADGE_PREFIX = "stat:"
STAT_BADGE_EMOJIS = {
    "corn_receiver": "🌽",
    "connectfour_hard_winner": "4️⃣",
    "command_user": "🏅",
    "richest": "💰",
}


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read a dict/asyncpg row without requiring asyncpg in unit tests."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _leader_ids(rows: Iterable[Mapping[str, Any] | Any]) -> tuple[set[int], int]:
    """Return every user tied for the highest positive ``total`` value."""

    totals: dict[int, int] = {}
    for row in rows:
        try:
            user_id = int(_row_value(row, "user_id", 0))
            total = int(_row_value(row, "total", 0))
        except (TypeError, ValueError):
            continue
        if user_id > 0 and total > 0:
            totals[user_id] = total
    if not totals:
        return set(), 0
    highest = max(totals.values())
    return {user_id for user_id, total in totals.items() if total == highest}, highest


def command_badge_leaders(
    rows: Iterable[Mapping[str, Any] | Any],
) -> list[StatBadge]:
    """Choose the one highest parent-command badge for each qualifying user.

    The SQL caller should provide parent command names (for example with
    ``split_part(lower(command), ' ', 1)``), so subcommands are deliberately
    counted under their parent.  Every user tied for a command's top count is
    eligible; if a user leads multiple commands only their highest-count one
    is kept, with an alphabetical tie-break for deterministic results.
    """

    grouped: dict[str, dict[int, int]] = {}
    for row in rows:
        try:
            command = str(_row_value(row, "command", "")).strip().casefold()
            user_id = int(_row_value(row, "user_id", 0))
            total = int(_row_value(row, "total", 0))
        except (TypeError, ValueError):
            continue
        if command and user_id > 0 and total > 0:
            grouped.setdefault(command, {})[user_id] = total

    candidates: dict[int, list[tuple[int, str]]] = {}
    for command, totals in grouped.items():
        highest = max(totals.values(), default=0)
        for user_id, total in totals.items():
            if total == highest:
                candidates.setdefault(user_id, []).append((total, command))

    badges: list[StatBadge] = []
    for user_id, choices in candidates.items():
        total, command = sorted(choices, key=lambda item: (-item[0], item[1]))[0]
        badges.append(
            StatBadge(
                user_id=user_id,
                badge_key="stat:command_user",
                emoji_name=STAT_BADGE_EMOJIS["command_user"],
                text=f"#1 {command} user",
                value=total,
            )
        )
    return badges


class BadgeService:
    """Atomic catalog, ownership, purchase, and refund operations."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def catalog(self, badge_key: str | None = None) -> list[Any]:
        """Return enabled catalog entries, optionally selecting one key."""

        if badge_key is None:
            rows = await self.pool.fetch("""
                SELECT badge_key, category, display_name, emoji_name, emoji_id,
                       is_custom, unicode, animated, price, enabled, metadata
                FROM badge_catalog
                WHERE enabled
                ORDER BY category, badge_key
                """)
        else:
            rows = await self.pool.fetch(
                """
                SELECT badge_key, category, display_name, emoji_name, emoji_id,
                       is_custom, unicode, animated, price, enabled, metadata
                FROM badge_catalog
                WHERE enabled AND badge_key = $1
                """,
                str(badge_key),
            )
        return list(rows)

    async def list_catalog(self) -> list[Any]:
        """Compatibility name used by shop commands."""

        return await self.catalog()

    async def owned(self, user_id: int, *, include_inactive: bool = False) -> list[Any]:
        """Return badges visible on a profile, newest first."""

        query = """
            SELECT id, user_id, emoji_name, emoji_id, is_custom, unicode,
                   animated, badge_key, text, created_at, badge_source,
                   catalog_key, purchase_price, purchase_guild_id, active,
                   revoked_at, refund_amount, revocation_reason
            FROM user_badges
            WHERE user_id = $1
        """
        if not include_inactive:
            query += " AND active"
        query += " ORDER BY id"
        return list(await self.pool.fetch(query, int(user_id)))

    async def award_stat_badge(
        self,
        user_id: int,
        *,
        badge_key: str,
        emoji_name: str,
        text: str,
        emoji_id: int | None = None,
        is_custom: bool = False,
        unicode: bool = True,
        animated: bool = False,
    ) -> Any:
        """Create or refresh one idempotent stat badge assignment.

        ``badge_key`` should be stable (for example
        ``stat:connectfour_hard_winner``).  Calling this repeatedly for a
        current leader updates their label without creating duplicates.
        """

        return await self.pool.fetchrow(
            """
            INSERT INTO user_badges (
                user_id, emoji_name, emoji_id, is_custom, unicode, animated,
                badge_key, text, badge_source, catalog_key, active,
                revoked_at, refund_amount, revocation_reason
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'stat', $7, TRUE,
                    NULL, 0, NULL)
            ON CONFLICT (user_id, badge_key) DO UPDATE
            SET emoji_name = EXCLUDED.emoji_name,
                emoji_id = EXCLUDED.emoji_id,
                is_custom = EXCLUDED.is_custom,
                unicode = EXCLUDED.unicode,
                animated = EXCLUDED.animated,
                text = EXCLUDED.text,
                badge_source = 'stat',
                catalog_key = EXCLUDED.catalog_key,
                active = TRUE,
                revoked_at = NULL,
                refund_amount = 0,
                revocation_reason = NULL
            RETURNING *
            """,
            int(user_id),
            str(emoji_name),
            emoji_id,
            bool(is_custom),
            bool(unicode),
            bool(animated),
            str(badge_key),
            str(text),
        )

    async def remove_stat_badge(self, user_id: int, badge_key: str) -> bool:
        """Hide a stat badge without affecting owner/purchase badges."""

        result = await self.pool.execute(
            """
            UPDATE user_badges
            SET active = FALSE, revoked_at = now(),
                revocation_reason = 'stat no longer current'
            WHERE user_id = $1 AND badge_key = $2
              AND badge_source = 'stat' AND active
            """,
            int(user_id),
            str(badge_key),
        )
        return result.endswith("1")

    async def purchase_badge(
        self,
        user_id: int,
        badge_key: str | None = None,
        *,
        catalog_key: str | None = None,
        emoji_name: str | None = None,
        emoji: str | None = None,
        text: str | None = None,
        display_name: str | None = None,
        price: int | None = None,
        emoji_id: int | None = None,
        is_custom: bool = False,
        unicode: bool = True,
        animated: bool = False,
        purchase_guild_id: int | None = None,
        guild_id: int | None = None,
    ) -> Any:
        """Atomically debit Coins and grant a purchased badge.

        A revoked badge key can be bought again; active ownership and wallet
        balance are both checked while the transaction holds their rows.
        """

        key = str(badge_key or catalog_key or "").strip()
        if not key:
            raise ValueError("A badge key is required.")
        catalog_key = str(catalog_key or key)
        # Shop callers may use the concise ``emoji``/``display_name`` names;
        # retain the explicit names for callers that already use this module.
        emoji_name = str(emoji_name if emoji_name is not None else emoji or "")
        text = str(text if text is not None else display_name or emoji_name)
        if not emoji_name:
            raise ValueError("A badge emoji is required.")
        purchase_guild_id = (
            purchase_guild_id if purchase_guild_id is not None else guild_id
        )
        if price is None:
            base_key = catalog_key
            if base_key not in {key, ""}:
                lookup_key = base_key
            elif key.count(":") > 1:
                lookup_key = key.rsplit(":", 1)[0]
            else:
                lookup_key = key
            catalog_row = await self.pool.fetchrow(
                "SELECT price FROM badge_catalog WHERE badge_key = $1 AND enabled",
                lookup_key,
            )
            if catalog_row is None or catalog_row["price"] is None:
                raise BadgeNotFound(lookup_key)
            price = int(catalog_row["price"])
        price = int(price)
        if price <= 0 or price > MAX_BADGE_PRICE:
            raise ValueError("Badge price is outside the supported range.")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO currency_wallets(user_id)
                    VALUES ($1)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    int(user_id),
                )
                wallet = await connection.fetchrow(
                    """
                    SELECT balance FROM currency_wallets
                    WHERE user_id = $1 FOR UPDATE
                    """,
                    int(user_id),
                )
                balance = int(wallet["balance"] if wallet else 0)
                if balance < price:
                    raise InsufficientFunds(balance, price)
                existing = await connection.fetchrow(
                    """
                    SELECT id, active FROM user_badges
                    WHERE user_id = $1 AND badge_key = $2 FOR UPDATE
                    """,
                    int(user_id),
                    key,
                )
                if existing is not None and bool(existing["active"]):
                    raise BadgeAlreadyOwned(key)
                reference = f"badge-purchase:{int(user_id)}:{uuid4().hex}"
                await connection.execute(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    price,
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions
                        (user_id, amount, source, reference_key)
                    VALUES ($1, $2, 'badge_purchase', $3)
                    """,
                    int(user_id),
                    -price,
                    reference,
                )
                badge = await connection.fetchrow(
                    """
                    INSERT INTO user_badges (
                        user_id, emoji_name, emoji_id, is_custom, unicode,
                        animated, badge_key, text, badge_source, catalog_key,
                        purchase_price, purchase_guild_id, active,
                        revoked_at, refund_amount, revocation_reason
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'purchase', $11,
                            $9, $10, TRUE, NULL, 0, NULL)
                    ON CONFLICT (user_id, badge_key) DO UPDATE
                    SET emoji_name = EXCLUDED.emoji_name,
                        emoji_id = EXCLUDED.emoji_id,
                        is_custom = EXCLUDED.is_custom,
                        unicode = EXCLUDED.unicode,
                        animated = EXCLUDED.animated,
                        text = EXCLUDED.text,
                        badge_source = 'purchase',
                        catalog_key = EXCLUDED.catalog_key,
                        purchase_price = EXCLUDED.purchase_price,
                        purchase_guild_id = EXCLUDED.purchase_guild_id,
                        active = TRUE,
                        revoked_at = NULL,
                        refund_amount = 0,
                        revocation_reason = NULL
                    RETURNING *
                    """,
                    int(user_id),
                    str(emoji_name),
                    emoji_id,
                    bool(is_custom),
                    bool(unicode),
                    bool(animated),
                    key,
                    str(text),
                    price,
                    purchase_guild_id,
                    catalog_key,
                )
                return BadgePurchase(badge, balance - price)

    async def revoke_badge(
        self,
        user_id: int,
        badge_key: str,
        *,
        reason: str,
        refund_purchase: bool = True,
    ) -> BadgeRevocation | None:
        """Deactivate a badge and refund half of a purchase when applicable."""

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT * FROM user_badges
                    WHERE user_id = $1 AND badge_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    str(badge_key),
                )
                if row is None:
                    return None
                price = int(_row_value(row, "purchase_price", 0) or 0)
                refund = (
                    price // 2
                    if refund_purchase
                    and str(_row_value(row, "badge_source", "")) == "purchase"
                    else 0
                )
                if refund:
                    await connection.execute(
                        """
                        INSERT INTO currency_wallets(user_id)
                        VALUES ($1) ON CONFLICT (user_id) DO NOTHING
                        """,
                        int(user_id),
                    )
                    reference = f"badge-refund:{int(user_id)}:{uuid4().hex}"
                    await connection.execute(
                        """
                        UPDATE currency_wallets
                        SET balance = balance + $2, updated_at = now()
                        WHERE user_id = $1
                        """,
                        int(user_id),
                        refund,
                    )
                    await connection.execute(
                        """
                        INSERT INTO currency_transactions
                            (user_id, amount, source, reference_key)
                        VALUES ($1, $2, 'badge_refund', $3)
                        """,
                        int(user_id),
                        refund,
                        reference,
                    )
                updated = await connection.fetchrow(
                    """
                    UPDATE user_badges
                    SET active = FALSE, revoked_at = now(),
                        refund_amount = $3, revocation_reason = $4
                    WHERE user_id = $1 AND badge_key = $2
                    RETURNING *
                    """,
                    int(user_id),
                    str(badge_key),
                    refund,
                    str(reason)[:500],
                )
                return BadgeRevocation(updated, refund)

    async def sell_badge(
        self,
        user_id: int,
        badge_key: str,
        *,
        reason: str = "sold by owner",
    ) -> BadgeRevocation:
        """Sell an active purchased badge for half of its purchase price.

        Selling is intentionally separate from :meth:`revoke_badge`: owner
        and achievement badges must never be converted into Coins.  The badge
        row and wallet are locked in the same transaction so concurrent sell
        requests cannot pay twice for one badge.
        """

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT * FROM user_badges
                    WHERE user_id = $1 AND badge_key = $2
                      AND active AND badge_source = 'purchase'
                    FOR UPDATE
                    """,
                    int(user_id),
                    str(badge_key),
                )
                if row is None:
                    raise BadgeNotFound(str(badge_key))
                price = int(_row_value(row, "purchase_price", 0) or 0)
                refund = price // 2
                if refund:
                    await connection.execute(
                        """
                        INSERT INTO currency_wallets(user_id)
                        VALUES ($1) ON CONFLICT (user_id) DO NOTHING
                        """,
                        int(user_id),
                    )
                    reference = f"badge-sale:{int(user_id)}:{uuid4().hex}"
                    await connection.execute(
                        """
                        UPDATE currency_wallets
                        SET balance = balance + $2, updated_at = now()
                        WHERE user_id = $1
                        """,
                        int(user_id),
                        refund,
                    )
                    await connection.execute(
                        """
                        INSERT INTO currency_transactions
                            (user_id, amount, source, reference_key)
                        VALUES ($1, $2, 'badge_sale', $3)
                        """,
                        int(user_id),
                        refund,
                        reference,
                    )
                updated = await connection.fetchrow(
                    """
                    UPDATE user_badges
                    SET active = FALSE, revoked_at = now(),
                        refund_amount = $3, revocation_reason = $4
                    WHERE user_id = $1 AND badge_key = $2
                    RETURNING *
                    """,
                    int(user_id),
                    str(badge_key),
                    refund,
                    str(reason)[:500],
                )
                return BadgeRevocation(updated, refund)

    async def revoke_unavailable_custom_badges(
        self,
        available_emoji_ids: Collection[int],
        *,
        reason: str = "custom emoji is no longer available",
    ) -> list[BadgeRevocation]:
        """Revoke purchased custom-emoji badges absent from ``available``.

        Callers should build ``available_emoji_ids`` from the bot's currently
        visible guild emojis after a guild leave/update event.  Unicode and
        owner/stat badges are never touched by this reconciliation.
        """

        available = {int(value) for value in available_emoji_ids}
        rows = await self.pool.fetch("""
            SELECT user_id, badge_key, emoji_id
            FROM user_badges
            WHERE badge_source = 'purchase' AND active AND is_custom
            """)
        revoked: list[BadgeRevocation] = []
        for row in rows:
            emoji_id = _row_value(row, "emoji_id")
            if emoji_id is None or int(emoji_id) in available:
                continue
            result = await self.revoke_badge(
                int(_row_value(row, "user_id", 0)),
                str(_row_value(row, "badge_key", "")),
                reason=reason,
            )
            if result is not None:
                revoked.append(result)
        return revoked


async def award_stat_badge(pool: Any, user_id: int, **kwargs: Any) -> Any:
    """Functional wrapper for extensions that do not retain a service."""

    return await BadgeService(pool).award_stat_badge(user_id, **kwargs)


async def purchase_badge(pool: Any, user_id: int, **kwargs: Any) -> Any:
    """Functional wrapper for :meth:`BadgeService.purchase_badge`."""

    return await BadgeService(pool).purchase_badge(user_id, **kwargs)


async def revoke_badge(
    pool: Any, user_id: int, badge_key: str, **kwargs: Any
) -> BadgeRevocation | None:
    """Functional wrapper for :meth:`BadgeService.revoke_badge`."""

    return await BadgeService(pool).revoke_badge(user_id, badge_key, **kwargs)


async def collect_stat_badges(
    pool: Any, *, bot_id: int | None = None
) -> list[StatBadge]:
    """Read current leaderboard sources and build derived badge assignments."""

    badges: list[StatBadge] = []

    corn_rows = await pool.fetch(
        "SELECT receiver_id AS user_id, COUNT(*) AS total "
        "FROM corn_reacts GROUP BY receiver_id"
    )
    corn_leaders, corn_total = _leader_ids(corn_rows)
    badges.extend(
        StatBadge(
            user_id=user_id,
            badge_key="stat:corn_receiver",
            emoji_name=STAT_BADGE_EMOJIS["corn_receiver"],
            text="#1 Corn receiver",
            value=corn_total,
        )
        for user_id in corn_leaders
    )

    connect_query = (
        "SELECT winner_id AS user_id, COUNT(*) AS total "
        "FROM connectfour_games "
        "WHERE against_bot = TRUE AND bot_difficulty = 'hard' "
        "AND winner_id IS NOT NULL "
        + ("AND winner_id <> $1 " if bot_id is not None else "")
        + "GROUP BY winner_id"
    )
    if bot_id is None:
        connect_rows = await pool.fetch(connect_query)
    else:
        connect_rows = await pool.fetch(connect_query, int(bot_id))
    connect_leaders, connect_total = _leader_ids(connect_rows)
    badges.extend(
        StatBadge(
            user_id=user_id,
            badge_key="stat:connectfour_hard_winner",
            emoji_name=STAT_BADGE_EMOJIS["connectfour_hard_winner"],
            text="#1 Connect4 Hard Mode Winner",
            value=connect_total,
        )
        for user_id in connect_leaders
    )

    command_rows = await pool.fetch(
        "SELECT user_id, split_part(LOWER(command), ' ', 1) AS command, "
        "COUNT(*) AS total FROM command_logs "
        "WHERE command IS NOT NULL AND BTRIM(command) <> '' "
        "GROUP BY user_id, split_part(LOWER(command), ' ', 1)"
    )
    badges.extend(command_badge_leaders(command_rows))

    richest_rows = await pool.fetch(
        "SELECT user_id, balance AS total FROM currency_wallets WHERE balance > 0"
    )
    richest_leaders, richest_total = _leader_ids(richest_rows)
    badges.extend(
        StatBadge(
            user_id=user_id,
            badge_key="stat:richest",
            emoji_name=STAT_BADGE_EMOJIS["richest"],
            text="Richest",
            value=richest_total,
        )
        for user_id in richest_leaders
    )
    return badges


async def reconcile_stat_badges(
    pool: Any, *, bot_id: int | None = None
) -> list[StatBadge]:
    """Refresh only stat badges and revoke rows that no longer qualify.

    ``BadgeService`` marks stale rows inactive rather than deleting them. This
    preserves an audit trail and keeps purchased/owner-managed rows untouched.
    """

    badges = await collect_stat_badges(pool, bot_id=bot_id)
    desired = {(badge.user_id, badge.badge_key) for badge in badges}
    service = BadgeService(pool)

    active_rows = await pool.fetch(
        "SELECT user_id, badge_key FROM user_badges "
        "WHERE badge_source = 'stat' AND active"
    )
    for row in active_rows:
        identity = (
            int(_row_value(row, "user_id", 0)),
            str(_row_value(row, "badge_key", "")),
        )
        if identity not in desired:
            await service.remove_stat_badge(*identity)

    for badge in badges:
        await service.award_stat_badge(
            badge.user_id,
            badge_key=badge.badge_key,
            emoji_name=badge.emoji_name,
            text=badge.text,
        )
    return badges


def stat_badge_cache_entry(badge: StatBadge) -> dict[str, Any]:
    """Return the runtime cache shape used by userinfo badge rendering."""

    return {
        "emoji_name": badge.emoji_name,
        "emoji_id": None,
        "is_custom": False,
        "animated": False,
        "text": badge.text,
        "badge_key": badge.badge_key,
        "created_at": None,
    }


async def refresh_stat_badges(bot: Any) -> list[StatBadge]:
    """Reconcile and update the in-memory userinfo badge cache."""

    bot_id = bot.user.id if getattr(bot, "user", None) is not None else None
    badges = await reconcile_stat_badges(bot.pool, bot_id=bot_id)
    cache = getattr(bot, "db_cache", None)
    cache_badges = getattr(cache, "user_badges", None)
    if isinstance(cache_badges, dict):
        for user_id, entries in list(cache_badges.items()):
            filtered = [
                entry
                for entry in entries
                if not str(entry.get("badge_key", "")).startswith(STAT_BADGE_PREFIX)
            ]
            if filtered:
                cache_badges[user_id] = filtered
            else:
                cache_badges.pop(user_id, None)
        for badge in badges:
            cache_badges.setdefault(badge.user_id, []).append(
                stat_badge_cache_entry(badge)
            )
    return badges


def schedule_stat_badge_refresh(bot: Any, *, delay: float = 5.0) -> None:
    """Debounce leaderboard updates caused by bursts of events."""

    task = getattr(bot, "_stat_badge_refresh_task", None)
    if task is not None and not task.done():
        return

    async def _refresh() -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await refresh_stat_badges(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger = getattr(bot, "logger", None)
            if logger is not None:
                logger.exception("Could not refresh stat badges")

    bot._stat_badge_refresh_task = asyncio.create_task(
        _refresh(), name="fishie-stat-badge-refresh"
    )
