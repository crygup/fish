"""Friends, follows, and marriage commands for the Fun cog."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence, cast

import discord
from discord.ext import commands

from utils import get_or_fetch_user

if TYPE_CHECKING:
    from extensions.context import Context


MARRIAGE_COOLDOWN = timedelta(days=7)
SOCIAL_PAGE_SIZE = 10
DiscordUser = discord.User | discord.Member


def friendship_pair(first_id: int, second_id: int) -> tuple[int, int]:
    """Return the canonical database ordering for a friendship."""

    return tuple(sorted((int(first_id), int(second_id))))  # type: ignore[return-value]


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return getattr(row, key, default)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Marriage:
    id: int
    proposer_id: int
    recipient_id: int
    ring_key: str
    ring_display: str
    married_at: datetime

    @property
    def member_ids(self) -> tuple[int, int]:
        return self.proposer_id, self.recipient_id


class SocialError(RuntimeError):
    """A user-facing social relationship error."""


class SocialService:
    """Database operations shared by commands and interactive views."""

    def __init__(self, pool: Any):
        self.pool = pool

    async def friend_requests_enabled(self, user_id: int) -> bool:
        value = await self.pool.fetchval(
            """
            SELECT friend_requests_enabled
            FROM social_user_settings
            WHERE user_id = $1
            """,
            int(user_id),
        )
        return True if value is None else bool(value)

    async def set_friend_requests_enabled(self, user_id: int, enabled: bool) -> None:
        await self.pool.execute(
            """
            INSERT INTO social_user_settings(user_id, friend_requests_enabled)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET
                friend_requests_enabled = EXCLUDED.friend_requests_enabled,
                updated_at = now()
            """,
            int(user_id),
            bool(enabled),
        )

    async def are_friends(self, first_id: int, second_id: int) -> bool:
        low_id, high_id = friendship_pair(first_id, second_id)
        return bool(
            await self.pool.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM social_friendships
                    WHERE user_low_id = $1 AND user_high_id = $2
                )
                """,
                low_id,
                high_id,
            )
        )

    async def request_friend(self, requester_id: int, recipient_id: int) -> str:
        requester_id = int(requester_id)
        recipient_id = int(recipient_id)
        if requester_id == recipient_id:
            raise SocialError("You can't send yourself a friend request.")
        if not await self.friend_requests_enabled(recipient_id):
            raise SocialError("That user has friend requests disabled.")
        if await self.are_friends(requester_id, recipient_id):
            return "friends"
        reverse = await self.pool.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM social_friend_requests
                WHERE requester_id = $1 AND recipient_id = $2
            )
            """,
            recipient_id,
            requester_id,
        )
        if reverse:
            return "reverse"
        result = await self.pool.execute(
            """
            INSERT INTO social_friend_requests(requester_id, recipient_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            requester_id,
            recipient_id,
        )
        return "created" if result.endswith("1") else "pending"

    async def accept_friend(self, recipient_id: int, requester_id: int) -> bool:
        recipient_id = int(recipient_id)
        requester_id = int(requester_id)
        low_id, high_id = friendship_pair(recipient_id, requester_id)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                deleted = await connection.fetchval(
                    """
                    DELETE FROM social_friend_requests
                    WHERE requester_id = $1 AND recipient_id = $2
                    RETURNING requester_id
                    """,
                    requester_id,
                    recipient_id,
                )
                if deleted is None:
                    return False
                await connection.execute(
                    """
                    INSERT INTO social_friendships(user_low_id, user_high_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    low_id,
                    high_id,
                )
                await connection.execute(
                    """
                    DELETE FROM social_friend_requests
                    WHERE (requester_id = $1 AND recipient_id = $2)
                       OR (requester_id = $2 AND recipient_id = $1)
                    """,
                    requester_id,
                    recipient_id,
                )
        return True

    async def decline_friend(self, recipient_id: int, requester_id: int) -> bool:
        result = await self.pool.execute(
            """
            DELETE FROM social_friend_requests
            WHERE requester_id = $1 AND recipient_id = $2
            """,
            int(requester_id),
            int(recipient_id),
        )
        return result.endswith("1")

    async def unfriend(self, first_id: int, second_id: int) -> bool:
        low_id, high_id = friendship_pair(first_id, second_id)
        result = await self.pool.execute(
            """
            DELETE FROM social_friendships
            WHERE user_low_id = $1 AND user_high_id = $2
            """,
            low_id,
            high_id,
        )
        return result.endswith("1")

    async def friend_ids(self, user_id: int) -> list[int]:
        rows = await self.pool.fetch(
            """
            SELECT CASE
                       WHEN user_low_id = $1 THEN user_high_id
                       ELSE user_low_id
                   END AS user_id
            FROM social_friendships
            WHERE user_low_id = $1 OR user_high_id = $1
            ORDER BY created_at DESC
            """,
            int(user_id),
        )
        return [int(_row_value(row, "user_id")) for row in rows]

    async def friend_request_ids(self, recipient_id: int) -> list[int]:
        rows = await self.pool.fetch(
            """
            SELECT requester_id
            FROM social_friend_requests
            WHERE recipient_id = $1
            ORDER BY created_at DESC
            """,
            int(recipient_id),
        )
        return [int(_row_value(row, "requester_id")) for row in rows]

    async def follow(self, follower_id: int, followed_id: int) -> bool:
        if int(follower_id) == int(followed_id):
            raise SocialError("You can't follow yourself.")
        result = await self.pool.execute(
            """
            INSERT INTO social_follows(follower_id, followed_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            int(follower_id),
            int(followed_id),
        )
        return result.endswith("1")

    async def unfollow(self, follower_id: int, followed_id: int) -> bool:
        result = await self.pool.execute(
            """
            DELETE FROM social_follows
            WHERE follower_id = $1 AND followed_id = $2
            """,
            int(follower_id),
            int(followed_id),
        )
        return result.endswith("1")

    async def following_ids(self, user_id: int) -> list[int]:
        rows = await self.pool.fetch(
            """
            SELECT followed_id AS user_id
            FROM social_follows
            WHERE follower_id = $1
            ORDER BY created_at DESC
            """,
            int(user_id),
        )
        return [int(_row_value(row, "user_id")) for row in rows]

    async def follower_ids(self, user_id: int) -> list[int]:
        rows = await self.pool.fetch(
            """
            SELECT follower_id AS user_id
            FROM social_follows
            WHERE followed_id = $1
            ORDER BY created_at DESC
            """,
            int(user_id),
        )
        return [int(_row_value(row, "user_id")) for row in rows]

    async def marriage_for(self, user_id: int) -> Marriage | None:
        row = await self.pool.fetchrow(
            """
            SELECT marriage.id, marriage.proposer_id, marriage.recipient_id,
                   marriage.ring_key, marriage.married_at,
                   catalog.display AS ring_display
            FROM social_marriage_members AS member
            JOIN social_marriages AS marriage ON marriage.id = member.marriage_id
            JOIN ring_catalog AS catalog ON catalog.ring_key = marriage.ring_key
            WHERE member.user_id = $1
            """,
            int(user_id),
        )
        if row is None:
            return None
        return Marriage(
            id=int(_row_value(row, "id")),
            proposer_id=int(_row_value(row, "proposer_id")),
            recipient_id=int(_row_value(row, "recipient_id")),
            ring_key=str(_row_value(row, "ring_key")),
            ring_display=str(_row_value(row, "ring_display")),
            married_at=_utc(_row_value(row, "married_at")),
        )

    async def cooldown_until(self, user_id: int) -> datetime | None:
        value = await self.pool.fetchval(
            """
            SELECT available_at
            FROM social_marriage_cooldowns
            WHERE user_id = $1 AND available_at > now()
            """,
            int(user_id),
        )
        return _utc(value) if isinstance(value, datetime) else None

    async def eligible_ring_rows(self, user_id: int) -> list[Any]:
        return list(
            await self.pool.fetch(
                """
                SELECT rings.ring_key, rings.quantity, catalog.display_name,
                       catalog.emoji_name, catalog.emoji_id, catalog.unicode,
                       catalog.display, catalog.price
                FROM user_rings AS rings
                JOIN ring_catalog AS catalog USING (ring_key)
                WHERE rings.user_id = $1 AND rings.quantity > 0 AND catalog.enabled
                ORDER BY catalog.price DESC, catalog.display_name ASC
                """,
                int(user_id),
            )
        )

    async def accept_marriage(
        self, proposer_id: int, recipient_id: int, ring_key: str
    ) -> Marriage:
        """Create a marriage and equip one ring for each spouse atomically.

        The proposal ring is transferred to the recipient and equipped there.
        The proposer then equips the most expensive ring left in their
        inventory.  Equipping is intentionally a swap (rather than a separate
        unequip action), so a married user can change which owned ring is shown
        without ever having to remove their equipped ring first.
        """

        proposer_id = int(proposer_id)
        recipient_id = int(recipient_id)
        now = datetime.now(timezone.utc)
        low_id, high_id = friendship_pair(proposer_id, recipient_id)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                friends = await connection.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM social_friendships
                        WHERE user_low_id = $1 AND user_high_id = $2
                    )
                    """,
                    low_id,
                    high_id,
                )
                if not friends:
                    raise SocialError(
                        "You must still be friends to accept this proposal."
                    )
                existing = await connection.fetchrow(
                    """
                    SELECT user_id
                    FROM social_marriage_members
                    WHERE user_id = ANY($1::BIGINT[])
                    FOR UPDATE
                    """,
                    [proposer_id, recipient_id],
                )
                if existing is not None:
                    raise SocialError("One of you is already married.")
                cooldowns = await connection.fetch(
                    """
                    SELECT user_id, available_at
                    FROM social_marriage_cooldowns
                    WHERE user_id = ANY($1::BIGINT[]) AND available_at > now()
                    FOR UPDATE
                    """,
                    [proposer_id, recipient_id],
                )
                if cooldowns:
                    until = max(
                        _utc(_row_value(row, "available_at")) for row in cooldowns
                    )
                    raise SocialError(
                        f"One of you cannot marry again until <t:{int(until.timestamp())}:R>."
                    )
                # The recipient may never have used a currency command. Their
                # wallet must exist before the ring inventory FK can be added.
                first_member_id, second_member_id = sorted((proposer_id, recipient_id))
                await connection.execute(
                    """
                    INSERT INTO currency_wallets(user_id)
                    VALUES ($1), ($2)
                    ON CONFLICT DO NOTHING
                    """,
                    first_member_id,
                    second_member_id,
                )
                total_rings = await connection.fetchval(
                    """
                    SELECT COALESCE(SUM(quantity), 0)
                    FROM user_rings
                    WHERE user_id = $1 AND quantity > 0
                    """,
                    proposer_id,
                )
                if int(total_rings or 0) < 2:
                    raise SocialError(
                        "You need to own at least two rings to marry someone."
                    )
                # Lock the proposer's complete inventory before transferring a
                # unit.  This keeps the remaining-ring choice consistent if a
                # purchase/equip request races with proposal acceptance.
                await connection.fetch(
                    """
                    SELECT ring_key
                    FROM user_rings
                    WHERE user_id = $1 AND quantity > 0
                    FOR UPDATE
                    """,
                    proposer_id,
                )
                selected = await connection.fetchrow(
                    """
                    SELECT quantity
                    FROM user_rings
                    WHERE user_id = $1 AND ring_key = $2 AND quantity > 0
                    FOR UPDATE
                    """,
                    proposer_id,
                    ring_key,
                )
                if selected is None:
                    raise SocialError("You no longer own that ring.")
                marriage_id = await connection.fetchval(
                    """
                    INSERT INTO social_marriages(proposer_id, recipient_id, ring_key)
                    VALUES ($1, $2, $3)
                    RETURNING id
                    """,
                    proposer_id,
                    recipient_id,
                    ring_key,
                )
                await connection.execute(
                    """
                    INSERT INTO social_marriage_members(user_id, marriage_id)
                    VALUES ($1, $3), ($2, $3)
                    """,
                    proposer_id,
                    recipient_id,
                    int(marriage_id),
                )
                sender_quantity = int(_row_value(selected, "quantity")) - 1
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET quantity = $3, equipped_count = $4, updated_at = now()
                    WHERE user_id = $1 AND ring_key = $2
                    """,
                    proposer_id,
                    ring_key,
                    sender_quantity,
                    0,
                )
                # The proposer must always have a ring equipped after the
                # marriage.  Clear any previous choice, then select the most
                # expensive ring still owned (the transferred unit no longer
                # counts toward this inventory).
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET equipped_count = 0, updated_at = now()
                    WHERE user_id = $1 AND equipped_count > 0
                    """,
                    proposer_id,
                )
                proposer_ring = await connection.fetchrow(
                    """
                    SELECT rings.ring_key
                    FROM user_rings AS rings
                    JOIN ring_catalog AS catalog USING (ring_key)
                    WHERE rings.user_id = $1 AND rings.quantity > 0
                    ORDER BY catalog.price DESC, catalog.display_name ASC,
                             rings.ring_key ASC
                    LIMIT 1
                    FOR UPDATE OF rings
                    """,
                    proposer_id,
                )
                if proposer_ring is None:
                    raise SocialError(
                        "You need to keep one ring for yourself to marry."
                    )
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET equipped_count = 1, updated_at = now()
                    WHERE user_id = $1 AND ring_key = $2
                    """,
                    proposer_id,
                    str(_row_value(proposer_ring, "ring_key")),
                )
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET equipped_count = 0, updated_at = now()
                    WHERE user_id = $1 AND equipped_count > 0
                    """,
                    recipient_id,
                )
                await connection.execute(
                    """
                    INSERT INTO user_rings(user_id, ring_key, quantity, equipped_count)
                    VALUES ($1, $2, 1, 1)
                    ON CONFLICT (user_id, ring_key) DO UPDATE SET
                        quantity = user_rings.quantity + 1,
                        equipped_count = 1,
                        updated_at = now()
                    """,
                    recipient_id,
                    ring_key,
                )
                row = await connection.fetchrow(
                    """
                    SELECT marriage.id, marriage.proposer_id, marriage.recipient_id,
                           marriage.ring_key, marriage.married_at,
                           catalog.display AS ring_display
                    FROM social_marriages AS marriage
                    JOIN ring_catalog AS catalog ON catalog.ring_key = marriage.ring_key
                    WHERE marriage.id = $1
                    """,
                    int(marriage_id),
                )
        if row is None:  # pragma: no cover - returned immediately after insert.
            raise SocialError("I couldn't finish that marriage.")
        return Marriage(
            id=int(_row_value(row, "id")),
            proposer_id=int(_row_value(row, "proposer_id")),
            recipient_id=int(_row_value(row, "recipient_id")),
            ring_key=str(_row_value(row, "ring_key")),
            ring_display=str(_row_value(row, "ring_display")),
            married_at=_utc(_row_value(row, "married_at", now)),
        )

    async def divorce(self, user_id: int) -> Marriage | None:
        user_id = int(user_id)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT marriage.id, marriage.proposer_id, marriage.recipient_id,
                           marriage.ring_key, marriage.married_at,
                           catalog.display AS ring_display
                    FROM social_marriage_members AS member
                    JOIN social_marriages AS marriage ON marriage.id = member.marriage_id
                    JOIN ring_catalog AS catalog ON catalog.ring_key = marriage.ring_key
                    WHERE member.user_id = $1
                    FOR UPDATE OF marriage
                    """,
                    user_id,
                )
                if row is None:
                    return None
                proposer_id = int(_row_value(row, "proposer_id"))
                recipient_id = int(_row_value(row, "recipient_id"))
                available_at = datetime.now(timezone.utc) + MARRIAGE_COOLDOWN
                await connection.execute(
                    "DELETE FROM social_marriages WHERE id = $1",
                    int(_row_value(row, "id")),
                )
                await connection.execute(
                    """
                    INSERT INTO social_marriage_cooldowns(user_id, available_at)
                    VALUES ($1, $3), ($2, $3)
                    ON CONFLICT (user_id) DO UPDATE SET
                        available_at = EXCLUDED.available_at,
                        updated_at = now()
                    """,
                    proposer_id,
                    recipient_id,
                    available_at,
                )
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET equipped_count = 0, updated_at = now()
                    WHERE user_id = ANY($1::BIGINT[]) AND equipped_count > 0
                    """,
                    [proposer_id, recipient_id],
                )
        return Marriage(
            id=int(_row_value(row, "id")),
            proposer_id=proposer_id,
            recipient_id=recipient_id,
            ring_key=str(_row_value(row, "ring_key")),
            ring_display=str(_row_value(row, "ring_display")),
            married_at=_utc(_row_value(row, "married_at")),
        )


def _social_container(
    title: str,
    body: str,
    *,
    color: discord.Color | int | None,
    thumbnail: str | None = None,
) -> discord.ui.Container:
    displays: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(f"## {title}")]
    if body:
        displays.append(discord.ui.TextDisplay(body))
    if thumbnail:
        section = discord.ui.Section(
            *displays, accessory=discord.ui.Thumbnail(thumbnail)
        )
        return discord.ui.Container(section, accent_color=color)
    return discord.ui.Container(*displays, accent_color=color)


class FriendRequestPrompt(discord.ui.LayoutView):
    def __init__(
        self,
        cog: SocialCommands,
        ctx: Context,
        requester: DiscordUser,
        recipient: DiscordUser,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.requester = requester
        self.recipient = recipient
        self.message: discord.Message | None = None
        self.accept_button = discord.ui.Button(
            label="Accept", style=discord.ButtonStyle.green
        )
        self.decline_button = discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.red
        )
        self.accept_button.callback = self._accept
        self.decline_button.callback = self._decline
        self.add_item(
            _social_container(
                "Friend request",
                f"{recipient.mention}, **{requester}** sent you a friend request.",
                color=cog._social_color(ctx),
                thumbnail=requester.display_avatar.url,
            )
        )
        self.add_item(discord.ui.ActionRow(self.accept_button, self.decline_button))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.recipient.id:
            return True
        await interaction.response.send_message(
            "Only the person receiving this friend request can respond.", ephemeral=True
        )
        return False

    def _disable(self) -> None:
        self.accept_button.disabled = True
        self.decline_button.disabled = True

    async def _accept(self, interaction: discord.Interaction) -> None:
        accepted = await self.cog.social.accept_friend(
            self.recipient.id, self.requester.id
        )
        self._disable()
        self.stop()
        text = (
            f"**{self.recipient}** and **{self.requester}** are now friends."
            if accepted
            else "That friend request is no longer pending."
        )
        self.clear_items()
        self.add_item(
            _social_container(
                "Friend request", text, color=self.cog._social_color(self.ctx)
            )
        )
        await interaction.response.edit_message(view=self)

    async def _decline(self, interaction: discord.Interaction) -> None:
        await self.cog.social.decline_friend(self.recipient.id, self.requester.id)
        self._disable()
        self.stop()
        self.clear_items()
        self.add_item(
            _social_container(
                "Friend request",
                f"**{self.recipient}** declined **{self.requester}**'s friend request.",
                color=self.cog._social_color(self.ctx),
            )
        )
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SocialListView(discord.ui.LayoutView):
    def __init__(self, ctx: Context, title: str, users: Sequence[DiscordUser]):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.title = title
        self.users = list(users)
        self.page = 0
        self.message: discord.Message | None = None
        self.previous = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.previous.callback = self._previous
        self.next.callback = self._next
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.users) + SOCIAL_PAGE_SIZE - 1) // SOCIAL_PAGE_SIZE)

    def _render(self) -> None:
        self.clear_items()
        start = self.page * SOCIAL_PAGE_SIZE
        users = self.users[start : start + SOCIAL_PAGE_SIZE]
        lines = [
            f"{index}. **{user}** (`{user.id}`)"
            for index, user in enumerate(users, start + 1)
        ]
        body = "\n".join(lines) if lines else "No users to show."
        if self.page_count > 1:
            body += f"\n\n-# Page {self.page + 1}/{self.page_count}"
        self.add_item(
            _social_container(
                self.title,
                body,
                color=getattr(self.ctx, "embedcolor", self.ctx.bot.embedcolor),
            )
        )
        if self.page_count > 1:
            self.add_item(discord.ui.ActionRow(self.previous, self.next))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this list can use its controls.", ephemeral=True
        )
        return False

    async def _change(self, interaction: discord.Interaction, offset: int) -> None:
        self.page = (self.page + offset) % self.page_count
        self._render()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, 1)


class FriendRequestsView(discord.ui.LayoutView):
    def __init__(self, cog: SocialCommands, ctx: Context, users: Sequence[DiscordUser]):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.users = list(users)
        self.page = 0
        self.message: discord.Message | None = None
        self.previous = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.accept_button = discord.ui.Button(
            label="Accept", style=discord.ButtonStyle.green
        )
        self.decline_button = discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.red
        )
        self.previous.callback = self._previous
        self.next.callback = self._next
        self.accept_button.callback = self._accept
        self.decline_button.callback = self._decline
        self._render()

    def _render(self) -> None:
        self.clear_items()
        if not self.users:
            self.add_item(
                _social_container(
                    "Friend requests",
                    "You have no pending friend requests.",
                    color=self.cog._social_color(self.ctx),
                )
            )
            return
        self.page %= len(self.users)
        user = self.users[self.page]
        body = (
            f"**{user}** (`{user.id}`)\n\n-# Request {self.page + 1}/{len(self.users)}"
        )
        self.add_item(
            _social_container(
                "Friend requests",
                body,
                color=self.cog._social_color(self.ctx),
                thumbnail=user.display_avatar.url,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                self.previous, self.accept_button, self.decline_button, self.next
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the owner of these friend requests can respond.", ephemeral=True
        )
        return False

    async def _change(self, interaction: discord.Interaction, offset: int) -> None:
        if self.users:
            self.page = (self.page + offset) % len(self.users)
        self._render()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, 1)

    async def _resolve(self, interaction: discord.Interaction, accept: bool) -> None:
        if not self.users:
            await interaction.response.send_message(
                "That request is no longer pending.", ephemeral=True
            )
            return
        requester = self.users[self.page]
        if accept:
            await self.cog.social.accept_friend(self.ctx.author.id, requester.id)
        else:
            await self.cog.social.decline_friend(self.ctx.author.id, requester.id)
        self.users.pop(self.page)
        if self.users:
            self.page %= len(self.users)
        else:
            self.page = 0
        self._render()
        await interaction.response.edit_message(view=self)

    async def _accept(self, interaction: discord.Interaction) -> None:
        await self._resolve(interaction, True)

    async def _decline(self, interaction: discord.Interaction) -> None:
        await self._resolve(interaction, False)


class ProposalView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: SocialCommands,
        ctx: Context,
        proposer: DiscordUser,
        recipient: DiscordUser,
        ring_key: str,
        ring_display: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.proposer = proposer
        self.recipient = recipient
        self.ring_key = ring_key
        self.ring_display = ring_display
        self.message: discord.Message | None = None
        self.yes_button = discord.ui.Button(
            label="Say Yes", style=discord.ButtonStyle.green
        )
        self.no_button = discord.ui.Button(
            label="Say No", style=discord.ButtonStyle.red
        )
        self.yes_button.callback = self._yes
        self.no_button.callback = self._no
        self._render(
            f"{recipient.mention}, **{proposer}** proposed to you with {ring_display}."
        )

    def _render(self, text: str, *, finished: bool = False) -> None:
        self.clear_items()
        self.add_item(
            _social_container(
                "Marriage proposal", text, color=self.cog._social_color(self.ctx)
            )
        )
        if not finished:
            self.add_item(discord.ui.ActionRow(self.yes_button, self.no_button))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in {self.proposer.id, self.recipient.id}:
            return True
        await interaction.response.send_message(
            "Only the people in this proposal can use these buttons.", ephemeral=True
        )
        return False

    async def _yes(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.recipient.id:
            await interaction.response.send_message(
                "Only the person receiving the proposal can say yes.", ephemeral=True
            )
            return
        try:
            marriage = await self.cog.social.accept_marriage(
                self.proposer.id, self.recipient.id, self.ring_key
            )
        except SocialError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            self.cog.bot.logger.exception("Failed to accept marriage proposal")
            await interaction.response.send_message(
                "I couldn't finish that marriage. Please try again.", ephemeral=True
            )
            return
        self.stop()
        self._render(
            f"**{self.proposer}** and **{self.recipient}** are now married {marriage.ring_display}",
            finished=True,
        )
        await interaction.response.edit_message(view=self)

    async def _no(self, interaction: discord.Interaction) -> None:
        self.stop()
        actor = (
            self.proposer if interaction.user.id == self.proposer.id else self.recipient
        )
        self._render(f"**{actor}** declined the proposal.", finished=True)
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self.yes_button.disabled = True
        self.no_button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SocialCommands:
    """Mixin providing Fishie's social commands."""

    bot: Any

    @property
    def social(self) -> SocialService:
        service = getattr(self, "_social_service", None)
        if service is None:
            service = SocialService(self.bot.pool)
            self._social_service = service
        return service

    def _social_color(self, ctx: Context) -> discord.Color | int | None:
        return getattr(ctx, "embedcolor", getattr(self.bot, "embedcolor", None))

    async def is_married(self, user_id: int) -> bool:
        """Public hook used by the currency cog's ring equip command."""

        return await self.social.marriage_for(int(user_id)) is not None

    async def marriage_for(self, user_id: int) -> Marriage | None:
        """Return a user's current marriage for profile rendering."""

        return await self.social.marriage_for(int(user_id))

    async def _users_from_ids(self, ids: Iterable[int]) -> list[DiscordUser]:
        users: list[DiscordUser] = []
        for user_id in ids:
            try:
                users.append(await get_or_fetch_user(self.bot, int(user_id)))
            except (discord.HTTPException, discord.NotFound):
                continue
        return users

    @cast(Any, commands.command)(name="friend")
    async def friend(self, ctx: Context, *, user: discord.User) -> None:
        """Send a user a friend request."""

        if user.bot:
            await ctx.send("You can't friend bots.")
            return
        try:
            state = await self.social.request_friend(ctx.author.id, user.id)
        except SocialError as error:
            await ctx.send(str(error))
            return
        if state == "friends":
            await ctx.send(f"You are already friends with **{user}**.")
            return
        if state == "pending":
            await ctx.send(f"You already sent **{user}** a friend request.")
            return
        if state == "reverse":
            await ctx.send(
                f"**{user}** already sent you a request. Use `{ctx.clean_prefix}friend-requests` to respond."
            )
            return
        view = FriendRequestPrompt(self, ctx, ctx.author, user)
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[user], replied_user=False
            ),
        )

    @cast(Any, commands.command)(name="unfriend")
    async def unfriend(self, ctx: Context, *, user: discord.User) -> None:
        """Remove a user from your friends."""

        if await self.social.unfriend(ctx.author.id, user.id):
            await ctx.send(f"You are no longer friends with **{user}**.")
        else:
            await ctx.send(f"You are not friends with **{user}**.")

    @cast(Any, commands.command)(name="friends")
    async def friends(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's Fishie friends."""

        users = await self._users_from_ids(await self.social.friend_ids(user.id))
        view = SocialListView(ctx, f"Friends for {user}", users)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @cast(Any, commands.group)(
        name="friend-requests",
        aliases=("friendrequests", "frs"),
        invoke_without_command=True,
    )
    async def friend_requests(self, ctx: Context) -> None:
        """View and manage your friend requests."""

        ids = await self.social.friend_request_ids(ctx.author.id)
        users = await self._users_from_ids(ids)
        view = FriendRequestsView(self, ctx, users)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @friend_requests.command(name="enable")
    async def friend_requests_enable(self, ctx: Context) -> None:
        """Allow other users to send you friend requests."""

        await self.social.set_friend_requests_enabled(ctx.author.id, True)
        await ctx.send("Friend requests are now enabled.")

    @friend_requests.command(name="disable")
    async def friend_requests_disable(self, ctx: Context) -> None:
        """Stop other users from sending you friend requests."""

        await self.social.set_friend_requests_enabled(ctx.author.id, False)
        await ctx.send("Friend requests are now disabled.")

    @cast(Any, commands.command)(name="follow")
    async def follow(self, ctx: Context, *, user: discord.User) -> None:
        """Follow a Fishie user or bot."""

        try:
            added = await self.social.follow(ctx.author.id, user.id)
        except SocialError as error:
            await ctx.send(str(error))
            return
        await ctx.send(
            f"You are now following **{user}**."
            if added
            else f"You are already following **{user}**."
        )

    @cast(Any, commands.command)(name="unfollow")
    async def unfollow(self, ctx: Context, *, user: discord.User) -> None:
        """Stop following a Fishie user or bot."""

        removed = await self.social.unfollow(ctx.author.id, user.id)
        await ctx.send(
            f"You are no longer following **{user}**."
            if removed
            else f"You were not following **{user}**."
        )

    @cast(Any, commands.command)(name="followers")
    async def followers(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's followers."""

        users = await self._users_from_ids(await self.social.follower_ids(user.id))
        view = SocialListView(ctx, f"Followers for {user}", users)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @cast(Any, commands.command)(name="following")
    async def following(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show who a user follows."""

        users = await self._users_from_ids(await self.social.following_ids(user.id))
        view = SocialListView(ctx, f"{user}'s following", users)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _proposal_arguments(
        self, ctx: Context, arguments: str
    ) -> tuple[discord.User, str | None]:
        raw = arguments.strip()
        if not raw:
            raise commands.BadArgument("Choose someone to propose to.")
        match = re.search(r"<@!?(\d{15,22})>", raw)
        converter = commands.UserConverter()
        if match:
            user = await converter.convert(ctx, match.group(0))
            selector = (raw[: match.start()] + " " + raw[match.end() :]).strip()
            return user, selector or None
        tokens = raw.split()
        for index, token in enumerate(tokens):
            try:
                user = await converter.convert(ctx, token)
            except commands.BadArgument:
                continue
            selector = " ".join(tokens[:index] + tokens[index + 1 :]).strip()
            return user, selector or None
        raise commands.BadArgument("I couldn't find the user you want to propose to.")

    async def _select_proposal_ring(self, user_id: int, selector: str | None) -> Any:
        rows = await self.social.eligible_ring_rows(user_id)
        total = sum(int(_row_value(row, "quantity", 0)) for row in rows)
        if total < 2:
            raise SocialError("You need to own at least two rings to propose.")
        if selector is None:
            return rows[0]
        normalized = selector.casefold().strip()
        partial = discord.PartialEmoji.from_str(selector.strip())
        selected_emoji_id = partial.id
        # Accept every selector users can see in the shop/inventory: stable
        # key, display name, numeric custom-emoji ID, or a full Discord emoji
        # token.  Matching against the stored ID avoids relying on emoji names
        # (which can be changed independently of the ID).
        exact = []
        for row in rows:
            ring_key = str(_row_value(row, "ring_key", "")).casefold()
            display_name = str(_row_value(row, "display_name", "")).casefold()
            emoji_name = str(_row_value(row, "emoji_name", "")).casefold()
            emoji_id = _row_value(row, "emoji_id")
            id_match = False
            if selected_emoji_id is not None:
                id_match = str(emoji_id or "") == str(selected_emoji_id)
            elif normalized.isdecimal():
                id_match = str(emoji_id or "") == normalized
            if id_match or normalized in {ring_key, display_name, emoji_name}:
                exact.append(row)
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise SocialError("That ring name matches more than one ring.")
        raise SocialError("You don't own a ring matching that name or ID.")

    @cast(Any, commands.command)(
        name="propose",
        aliases=("marry",),
        extras={"usage": "<@user> [ring]"},
    )
    async def propose(self, ctx: Context, *, arguments: str) -> None:
        """Propose to a friend using two owned rings."""

        user, selector = await self._proposal_arguments(ctx, arguments)
        fishie_ids = {
            int(getattr(self.bot, "active_bot_id", 0)),
            int(getattr(self.bot, "legacy_bot_id", 0)),
            int(getattr(self.bot, "new_bot_id", 0)),
            int(getattr(getattr(self.bot, "user", None), "id", 0)),
        }
        fishie_ids.discard(0)
        if user.id in fishie_ids:
            await ctx.send("You can't marry me baka~")
            return
        if user.bot:
            await ctx.send("You can't marry bots.")
            return
        if user.id == ctx.author.id:
            await ctx.send("You can't marry yourself.")
            return
        if not await self.social.are_friends(ctx.author.id, user.id):
            await ctx.send("You must be friends before you can propose.")
            return
        if await self.is_married(ctx.author.id) or await self.is_married(user.id):
            await ctx.send("One of you is already married.")
            return
        cooldowns = [
            value
            for value in (
                await self.social.cooldown_until(ctx.author.id),
                await self.social.cooldown_until(user.id),
            )
            if value is not None
        ]
        if cooldowns:
            available_at = max(cooldowns)
            await ctx.send(
                f"One of you cannot marry again until <t:{int(available_at.timestamp())}:R>."
            )
            return
        try:
            ring = await self._select_proposal_ring(ctx.author.id, selector)
        except SocialError as error:
            await ctx.send(str(error))
            return
        view = ProposalView(
            self,
            ctx,
            ctx.author,
            user,
            str(_row_value(ring, "ring_key")),
            str(_row_value(ring, "display")),
        )
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[user], replied_user=False
            ),
        )

    @cast(Any, commands.command)(name="divorce", aliases=("unmarry",))
    async def divorce(self, ctx: Context) -> None:
        """End your marriage and begin a one-week remarriage cooldown."""

        marriage = await self.social.marriage_for(ctx.author.id)
        if marriage is None:
            await ctx.send("You are not married.")
            return
        spouse_id = (
            marriage.recipient_id
            if marriage.proposer_id == ctx.author.id
            else marriage.proposer_id
        )
        try:
            spouse = await get_or_fetch_user(self.bot, spouse_id)
            spouse_name = str(spouse)
        except discord.HTTPException:
            spouse_name = str(spouse_id)
        confirmation = await ctx.prompt(
            f"Are you sure you want to divorce **{spouse_name}**? This starts a "
            "one-week remarriage cooldown. Your rings will be kept.",
            confirm_label="Divorce",
            cancel_label="Keep marriage",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if confirmation is None:
            await ctx.send(
                "Divorce cancelled.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        divorced = await self.social.divorce(ctx.author.id)
        if divorced is None:
            await ctx.send(
                "That marriage is no longer active.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            f"You and **{spouse_name}** are no longer married. You can marry again in one week."
        )
