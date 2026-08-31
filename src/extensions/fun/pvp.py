"""Shared helpers and views for player-versus-player Coin games.

The normal game views are intentionally kept separate from this module.  A
pending duel can therefore collect bids and consent before a game controller
escrows any Coins, which also makes cancelling a challenge side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord.ext import commands

from core.currency import EVERYTHING_AMOUNT, CoinAmountError, parse_coin_amount

if TYPE_CHECKING:
    from extensions.context import Context


DUEL_MIN_BID = 10
ACCOUNT_AGE = timedelta(days=31)


def account_is_old_enough(
    user: discord.abc.User, *, now: datetime | None = None
) -> bool:
    """Return whether *user* has had a Discord account for at least 31 days."""

    created_at = getattr(user, "created_at", None)
    if not isinstance(created_at, datetime):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    current = now or discord.utils.utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - created_at >= ACCOUNT_AGE


def account_age_message(user: discord.abc.User) -> str:
    """Return a consistent, actionable account-age rejection message."""

    created_at = getattr(user, "created_at", None)
    if isinstance(created_at, datetime):
        timestamp = int(created_at.timestamp() + ACCOUNT_AGE.total_seconds())
        return (
            f"{user.mention} cannot join this duel yet; their account must be at "
            f"least 31 days old (available <t:{timestamp}:R>)."
        )
    return f"{user.mention} cannot join this duel because their account age is unavailable."


async def parse_duel_arguments(
    ctx: Context, arguments: tuple[object, ...]
) -> tuple[discord.abc.User | None, int | None]:
    """Parse a text-command duel's target and bid in either order.

    ``commands.UserConverter`` handles mentions, IDs, usernames, and display
    names.  Only one user and one integer are accepted, making malformed input
    fail before a challenge is sent.
    """

    target: discord.abc.User | None = None
    amount: int | None = None
    amount_tokens: list[str] = []
    converter = commands.UserConverter()
    for raw in arguments:
        # Hybrid command children pass an already-converted User while text
        # commands pass strings.  Preserve the converted object rather than
        # trying to feed its repr back through UserConverter.
        if isinstance(raw, discord.abc.User):
            if target is not None:
                raise commands.BadArgument("Give only one player to challenge.")
            target = raw
            continue
        value = str(raw).strip()
        if not value:
            continue
        # Keep the amount tokens together so spaced forms such as
        # ``1 hundred thousand`` survive Discord's text tokenisation.  User
        # mentions/IDs are still resolved eagerly below, preserving the
        # dynamic ``<user> <bid>`` and ``<bid> <user>`` ordering.
        try:
            number = parse_coin_amount(value)
        except CoinAmountError:
            number = None
        # Discord snowflake IDs are much larger than any practical bid. Try
        # resolving those numeric tokens as users before treating them as a
        # Coin amount, so ``ttt 123456789012345678 @user`` remains dynamic.
        if isinstance(number, int) and number >= 1_000_000_000_000:
            try:
                converted = await converter.convert(ctx, value)
            except commands.BadArgument:
                converted = None
            if converted is not None:
                if target is not None:
                    raise commands.BadArgument("Give only one player to challenge.")
                target = converted
                continue
        if number is not None:
            amount_tokens.append(value)
            continue
        if target is not None:
            amount_tokens.append(value)
            continue
        try:
            target = await converter.convert(ctx, value)
        except commands.BadArgument:
            amount_tokens.append(value)

    if amount_tokens:
        expression = " ".join(amount_tokens)
        try:
            parsed = parse_coin_amount(expression)
        except CoinAmountError as exc:
            # If this was not an amount, preserve the old useful user lookup
            # error rather than exposing an implementation parsing detail.
            raise commands.BadArgument(
                f"I couldn't find a player matching `{expression}`."
            ) from exc
        if parsed == EVERYTHING_AMOUNT:
            currency = getattr(getattr(ctx, "bot", None), "currency", None)
            get_wallet = getattr(currency, "get_wallet", None)
            if not callable(get_wallet):
                raise commands.BadArgument("Your wallet is unavailable right now.")
            wallet = await get_wallet(ctx.author.id)
            amount = int(getattr(wallet, "balance", 0) or 0)
            if amount < DUEL_MIN_BID:
                raise commands.BadArgument(
                    f"You need at least {DUEL_MIN_BID:,} Coins to bid everything."
                )
            prompt = getattr(ctx, "prompt", None)
            if not callable(prompt):
                raise commands.BadArgument("I couldn't confirm that bid.")
            confirmed = await prompt(
                "Are you sure you want to bid everything?",
                confirm_label="Yes",
                cancel_label="No",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if confirmed is None:
                raise commands.BadArgument("Bid cancelled.")
        else:
            amount = int(parsed)
    return target, amount


@dataclass(slots=True)
class DuelBid:
    user_id: int
    amount: int
    confirmed: bool = False


ReadyCallback = Callable[[discord.Interaction, tuple[int, int]], Awaitable[None]]
ChallengeCallback = Callable[[discord.Interaction], Awaitable[None]]
ChoiceCallback = Callable[[discord.Interaction, dict[int, str]], Awaitable[None]]
ExpireCallback = Callable[[], Awaitable[None] | None]


class DuelChallengeView(discord.ui.View):
    """Simple accept/decline prompt for an un-wagered player duel."""

    def __init__(
        self,
        ctx: Context,
        challenger: discord.abc.User,
        opponent: discord.abc.User,
        *,
        game_name: str,
        on_accept: ChallengeCallback,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.challenger = challenger
        self.opponent = opponent
        self.game_name = game_name
        self.on_accept = on_accept
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opponent.id:
            return True
        await interaction.response.send_message(
            "Only the challenged user can answer this invitation.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.on_accept(interaction)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(
            content=f"## {self.game_name}\nChallenge declined.",
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content=f"## {self.game_name}\nChallenge expired.",
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass


class DuelChoiceView(discord.ui.LayoutView):
    """Collect one choice from each participant, then resolve the duel."""

    def __init__(
        self,
        challenger: discord.abc.User,
        opponent: discord.abc.User,
        *,
        game_name: str,
        choices: tuple[str, ...],
        on_resolve: ChoiceCallback,
        accent_color: discord.Colour | int | None = None,
        on_expire: ExpireCallback | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent
        self.game_name = game_name
        self.on_resolve = on_resolve
        self.on_expire = on_expire
        self.selections: dict[int, str] = {}
        self.message: discord.Message | None = None
        self.status = discord.ui.TextDisplay(self.render())
        buttons: list[discord.ui.Button] = []
        for value in choices:
            button = discord.ui.Button(
                label=value.title(), style=discord.ButtonStyle.secondary
            )

            async def callback(
                interaction: discord.Interaction, selected: str = value
            ) -> None:
                await self._choose(interaction, selected)

            button.callback = callback
            buttons.append(button)
        self.buttons = buttons
        self.container = discord.ui.Container(
            self.status,
            discord.ui.ActionRow(*buttons),
            accent_color=accent_color,
        )
        self.add_item(self.container)

    def render(self) -> str:
        def state(user: discord.abc.User) -> str:
            return (
                "Locked in their answer!"
                if user.id in self.selections
                else "thinking..."
            )

        return (
            f"## {self.game_name}\n"
            f"{self.challenger.mention}: **{state(self.challenger)}**\n"
            f"{self.opponent.mention}: **{state(self.opponent)}**"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in {self.challenger.id, self.opponent.id}:
            if interaction.user.id in self.selections:
                await interaction.response.send_message(
                    "You already chose for this round.", ephemeral=True
                )
                return False
            return True
        await interaction.response.send_message(
            "This player duel is not for you.", ephemeral=True
        )
        return False

    async def _choose(self, interaction: discord.Interaction, choice: str) -> None:
        self.selections[interaction.user.id] = choice
        if len(self.selections) < 2:
            self.status.content = self.render()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        for button in self.buttons:
            button.disabled = True
        self.status.content = self.render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.on_resolve(interaction, dict(self.selections))
        self.stop()

    async def on_timeout(self) -> None:
        if self.on_expire is not None:
            result = self.on_expire()
            if isawaitable(result):
                await result
        for button in self.buttons:
            button.disabled = True
        self.status.content = f"## {self.game_name}\nDuel timed out."
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass


class DuelBidModal(discord.ui.Modal, title="Place your bid"):
    amount = discord.ui.TextInput(
        label="Coin bid",
        placeholder="10 or more",
        min_length=1,
        max_length=20,
        required=True,
    )

    def __init__(self, view: "DuelBidView", user_id: int):
        super().__init__()
        self.view_ref = view
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            parsed = parse_coin_amount(str(self.amount.value))
            if parsed == EVERYTHING_AMOUNT:
                raise CoinAmountError(
                    "Use a numeric bid here; use the command with `everything` "
                    "to confirm bidding your full wallet."
                )
            amount = int(parsed)
        except CoinAmountError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.view_ref.set_bid(interaction, self.user_id, amount)


class DuelBidView(discord.ui.LayoutView):
    """Collect per-player bids and explicit consent from both participants."""

    _active: dict[tuple[int, int, int], "DuelBidView"] = {}

    def __init__(
        self,
        ctx: Context,
        challenger: discord.abc.User,
        opponent: discord.abc.User,
        *,
        game_name: str,
        challenger_bid: int,
        on_ready: ReadyCallback,
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.challenger = challenger
        self.opponent = opponent
        self.game_name = game_name
        self.on_ready = on_ready
        self.message: discord.Message | None = None
        # Bids remain editable until both players have confirmed.  A player
        # may confirm first, but changing either bid before the second
        # confirmation must make the round require consent again.
        self.confirmation_started = False
        self.bids: dict[int, DuelBid] = {
            challenger.id: DuelBid(challenger.id, challenger_bid)
        }
        first_id, second_id = sorted((int(challenger.id), int(opponent.id)))
        self.key = (ctx.channel.id, first_id, second_id)
        self._active[self.key] = self
        self.status = discord.ui.TextDisplay(self.content())
        self.place = discord.ui.Button(
            label="Place bid", style=discord.ButtonStyle.secondary
        )
        self.match = discord.ui.Button(
            label="Match bid", style=discord.ButtonStyle.secondary
        )
        self.confirm = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.success, disabled=True
        )
        self.cancel = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger
        )
        self.place.callback = self._place
        self.match.callback = self._match
        self.confirm.callback = self._confirm
        self.cancel.callback = self._cancel
        self.container = discord.ui.Container(
            self.status,
            discord.ui.ActionRow(self.place, self.match),
            discord.ui.ActionRow(self.confirm, self.cancel),
            accent_color=getattr(ctx.bot, "embedcolor", None),
        )
        self.add_item(self.container)

    def content(self) -> str:
        challenger = self.bids.get(self.challenger.id)
        opponent = self.bids.get(self.opponent.id)
        left = f"{challenger.amount:,} Coins" if challenger else "not set"
        right = f"{opponent.amount:,} Coins" if opponent else "not set"
        # A custom bid is valid on its own; the two players do not have to
        # enter the same amount.  Each player's bid is escrowed separately
        # and the game controller settles the resulting pool.
        ready = challenger is not None and opponent is not None
        note = " Both players must confirm." if ready else ""

        # Keep the consent state visible on the prompt itself.  This is
        # deliberately an emoji prefix rather than a colour/style change so
        # it remains clear for users with colour-blindness and when the
        # Components V2 view is rendered in compact clients.
        challenger_status = "🟢" if challenger and challenger.confirmed else "🔴"
        opponent_status = "🟢" if opponent and opponent.confirmed else "🔴"
        return (
            f"## {self.game_name} · Bid\n"
            f"{challenger_status} {self.challenger.mention}: **{left}**\n"
            f"{opponent_status} {self.opponent.mention}: **{right}**\n"
            "Place a bid or match the challenger's bid."
            f"{note}"
        )

    def _refresh(self) -> None:
        self.status.content = self.content()
        challenger = self.bids.get(self.challenger.id)
        opponent = self.bids.get(self.opponent.id)
        # Keep Confirm available for any two placed bids, including custom
        # amounts that differ from the challenger's original bid.
        ready = challenger is not None and opponent is not None
        self.confirm.disabled = not ready

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in {self.challenger.id, self.opponent.id}:
            return True
        await interaction.response.send_message(
            "This duel bid prompt is not for you.", ephemeral=True
        )
        return False

    async def set_bid(
        self, interaction: discord.Interaction, user_id: int, amount: int
    ) -> None:
        if self.confirmation_started:
            await interaction.response.send_message(
                "Bids can no longer be changed after both players confirm.",
                ephemeral=True,
            )
            return
        if amount < DUEL_MIN_BID:
            await interaction.response.send_message(
                f"Bids must be at least {DUEL_MIN_BID:,} Coins.", ephemeral=True
            )
            return
        # Any bid change invalidates consent that was given for the previous
        # amounts.  Reset both statuses so neither player can accidentally
        # start a round without reviewing the new pool.
        for bid in self.bids.values():
            bid.confirmed = False
        self.bids[user_id] = DuelBid(user_id, amount)
        self._refresh()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def set_bid_from_context(self, ctx: Context, amount: int) -> bool:
        """Set a pending bid from the text-only ``bid`` command."""

        if (
            self.is_finished()
            or self.confirmation_started
            or amount < DUEL_MIN_BID
            or ctx.author.id
            not in {
                self.challenger.id,
                self.opponent.id,
            }
        ):
            return False
        for bid in self.bids.values():
            bid.confirmed = False
        self.bids[ctx.author.id] = DuelBid(ctx.author.id, amount)
        self._refresh()
        if self.message is not None:
            await self.message.edit(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        return True

    async def set_bid_from_command(self, user_id: int, amount: int) -> bool:
        """Update a pending bid from the text-only ``bid`` command."""

        if (
            self.is_finished()
            or self.confirmation_started
            or amount < DUEL_MIN_BID
            or user_id
            not in {
                self.challenger.id,
                self.opponent.id,
            }
        ):
            return False
        for bid in self.bids.values():
            bid.confirmed = False
        self.bids[user_id] = DuelBid(user_id, amount)
        self._refresh()
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                return False
        return True

    async def _place(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DuelBidModal(self, interaction.user.id))

    async def _match(self, interaction: discord.Interaction) -> None:
        challenger = self.bids.get(self.challenger.id)
        if challenger is None:
            await interaction.response.send_message(
                "The challenger has not placed a bid yet.", ephemeral=True
            )
            return
        await self.set_bid(interaction, interaction.user.id, challenger.amount)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        challenger = self.bids.get(self.challenger.id)
        opponent = self.bids.get(self.opponent.id)
        bid = self.bids.get(interaction.user.id)
        if bid is None:
            await interaction.response.send_message(
                "Place a bid before confirming.", ephemeral=True
            )
            return
        if challenger is None or opponent is None:
            await interaction.response.send_message(
                "Both players must place a bid before confirming.", ephemeral=True
            )
            return
        bid.confirmed = True
        if not all(bid.confirmed for bid in self.bids.values()):
            self._refresh()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        self.confirmation_started = True
        self._active.pop(self.key, None)
        self.stop()
        await self.on_ready(interaction, (challenger.amount, opponent.amount))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self._active.pop(self.key, None)
        self.stop()
        self.place.disabled = True
        self.match.disabled = True
        self.confirm.disabled = True
        self.cancel.disabled = True
        self.status.content = f"## {self.game_name} · Cancelled"
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def on_timeout(self) -> None:
        self._active.pop(self.key, None)
        self.place.disabled = True
        self.match.disabled = True
        self.confirm.disabled = True
        self.cancel.disabled = True
        self.status.content = f"## {self.game_name} · Bid expired"
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass

    @classmethod
    def active_for(cls, channel_id: int, user_id: int) -> "DuelBidView | None":
        for key, view in cls._active.items():
            if key[0] == channel_id and user_id in key[1:]:
                return view
        return None
