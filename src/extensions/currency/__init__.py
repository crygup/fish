from __future__ import annotations

import asyncio
import random
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, cast
from uuid import uuid4

import discord
import emoji as emoji_lib
import pycountry
from discord import app_commands
from discord.ext import commands, tasks

from core import Cog
from core.badges import BadgeAlreadyOwned, BadgeNotFound, refresh_stat_badges
from core.catalogs import sync_shop_catalog
from core.currency import (
    COIN_AMOUNT_DESCRIPTION,
    EVERYTHING_AMOUNT,
    LOTTERY_TICKET_PRICE,
    BalanceOverflow,
    ClaimType,
    CoinAmountError,
    ColorAlreadyOwned,
    ColorNotOwned,
    CurrencyEarnings,
    CurrencyEarningSource,
    InsufficientFunds,
    InvalidAmount,
    LotteryDraw,
    LotteryStatus,
    RacingEmojiAlreadyOwned,
    RacingEmojiNotOwned,
    RingEquipped,
    RingNotOwned,
    TitleAlreadyOwned,
    TitleNotOwned,
    claim_period_start,
    lottery_period_start,
    next_claim_reset,
    parse_coin_amount,
)
from core.handoff import is_legacy_instance
from extensions.fun.wordle import get_wordle_settings, new_wordle_game
from utils.formats import plural
from utils.paths import FILES_ROOT
from utils.racing_emoji import classify_racing_emoji
from utils.shop_catalog import SHOP_CATALOG

from .profile_card import (
    ProfileCardData,
    inline_badge_markup,
    render_profile_card,
    resolve_badge_images,
)
from .work import WorkClickView, WorkMathView, WorkWordleView

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


DAILY_COINS = 100
WEEKLY_COINS = 1_000
MIN_TRANSFER_ACCOUNT_AGE = timedelta(days=31)
FISHIE_USER_ID = 876391494485950504


def _account_age_remaining(user: discord.abc.User) -> timedelta:
    """Return how much longer *user* must exist before currency transfers.

    Discord exposes account creation through the snowflake-backed
    ``created_at`` property.  Keeping this check at the command boundary
    means the persistence layer remains usable by scheduled jobs and tests
    that do not have Discord user objects available.
    """

    created_at = getattr(user, "created_at", None)
    if not isinstance(created_at, datetime):
        return timedelta(0)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = discord.utils.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(MIN_TRANSFER_ACCOUNT_AGE - (now - created_at), timedelta(0))


def _currency_source_label(source: str) -> str:
    """Turn an internal transaction source into a readable label."""

    parts = [part.strip() for part in str(source).split(":") if part.strip()]
    return " · ".join(part.replace("_", " ").title() for part in parts) or "Other"


@dataclass(frozen=True, slots=True)
class BadgeOffer:
    """A badge that can be bought from the Coins shop.

    The built-in seed catalog is stored in ``shop_catalog.json``.  The
    database is synchronized from that file at startup, so an owner command
    can add or disable catalog rows without requiring a schema migration.
    """

    key: str
    emoji: str
    price: int
    description: str
    kind: Literal["emoji", "custom_emoji", "flag"] = "emoji"
    selector: str | None = None
    enabled: bool = True

    @property
    def command_selector(self) -> str:
        """Return the short selector shown to users in the shop."""

        return self.selector or self.key


@dataclass(frozen=True, slots=True)
class EarningsGroup:
    """A readable group of related transaction sources."""

    key: str
    label: str
    sources: tuple[CurrencyEarningSource, ...]

    @property
    def earned(self) -> int:
        return sum(source.earned for source in self.sources)

    @property
    def lost(self) -> int:
        return sum(source.lost for source in self.sources)

    @property
    def net(self) -> int:
        return self.earned - self.lost


_WAGER_GROUP_LABELS = {
    "higher_lower": "Higher or Lower",
    "heads_tails": "Heads or Tails",
    "rock_paper_scissors": "Rock Paper Scissors",
    "blackjack": "Blackjack",
    "crash": "Crash",
    "mines": "Mines",
    "slots": "Slots",
}
_PVP_GROUP_LABELS = {
    "tictactoe": "Tic-Tac-Toe",
    "tic_tac_toe": "Tic-Tac-Toe",
    "ttt": "Tic-Tac-Toe",
    "connectfour": "Connect Four",
    "connect_four": "Connect Four",
    "connect4": "Connect Four",
    "c4": "Connect Four",
    "heads_tails": "Heads or Tails",
    "heads-or-tails": "Heads or Tails",
    "headsortails": "Heads or Tails",
    "coinflip": "Heads or Tails",
    "rock_paper_scissors": "Rock Paper Scissors",
    "rock-paper-scissors": "Rock Paper Scissors",
    "rps": "Rock Paper Scissors",
}
_EARNINGS_GROUP_ORDER = (
    "game_rewards",
    "work",
    "lottery",
    "races",
    "luckyroll",
    "wager:higher_lower",
    "wager:heads_tails",
    "wager:rock_paper_scissors",
    "wager:blackjack",
    "wager:crash",
    "wager:mines",
    "wager:slots",
    "pvp:tictactoe",
    "pvp:tic_tac_toe",
    "pvp:ttt",
    "pvp:connectfour",
    "pvp:connect_four",
    "pvp:connect4",
    "pvp:c4",
    "pvp:heads_tails",
    "pvp:heads-or-tails",
    "pvp:headsortails",
    "pvp:coinflip",
    "pvp:rock_paper_scissors",
    "pvp:rock-paper-scissors",
    "pvp:rps",
    "claims",
    "reputation",
    "supporters",
    "gifts",
    "shop",
    "other",
)


def _earnings_group_for_source(source: str) -> tuple[str, str]:
    """Return the dropdown group for one internal transaction source."""

    raw = str(source)
    if raw.startswith("work_"):
        return "work", "Work"
    if raw.startswith("game_reward:"):
        return "game_rewards", "Game rewards"
    if raw.startswith("lottery_"):
        return "lottery", "Lottery"
    if raw.startswith("sea_animal_race_"):
        return "races", "Races"
    if raw.startswith("luckyroll_"):
        return "luckyroll", "Lucky Roll"
    if raw.startswith("wager_"):
        _, _, game = raw.partition(":")
        if game.startswith("pvp_"):
            game = game.removeprefix("pvp_")
            label = _PVP_GROUP_LABELS.get(game, _currency_source_label(game))
            return f"pvp:{game}", f"{label} · PvP"
        return f"wager:{game}", _WAGER_GROUP_LABELS.get(
            game, _currency_source_label(game)
        )
    if raw.startswith("pvp_") or raw.startswith("pvp:"):
        action, separator, game = raw.partition(":")
        # A source without a colon is still useful (for example a generic
        # pvp_refund row); keep the action as its game key rather than
        # dropping it into the unhelpful "Other" bucket.
        if not separator:
            game = action.removeprefix("pvp_") or "other"
        label = _PVP_GROUP_LABELS.get(game, _currency_source_label(game))
        return f"pvp:{game}", f"{label} · PvP"
    if raw.startswith("claim_"):
        return "claims", "Daily and weekly claims"
    if raw.startswith("reputation_"):
        return "reputation", "Reputation"
    if raw == "fishie_supporter":
        return "supporters", "Fishie Supporter"
    if raw == "birthday":
        return "birthday", "Birthday rewards"
    if raw in {"give", "give_sent", "give_received"} or raw.startswith("give_"):
        return "gifts", "Gifts"
    if raw.startswith(("badge_", "title_", "color_", "racing_", "ring_")):
        return "shop", "Shop purchases and sales"
    return "other", "Other"


def _earnings_source_detail_label(source: str) -> str:
    """Make a transaction source concise when shown inside a group."""

    raw = str(source)
    if raw.startswith("work_"):
        return raw.removeprefix("work_").replace("_", " ").title()
    if raw.startswith("game_reward:"):
        value = raw.partition(":")[2]
        labels = {
            "click": "Click rewards",
            "lightsout": "Lights Out",
            "wordbomb": "Word Bomb",
            # These shared sources predate game-specific source keys and may
            # represent either Tic-Tac-Toe or Connect Four.  Migration 82
            # resolves unambiguous rows; keep any remaining ambiguous rows
            # neutral rather than mislabeling them as Tic-Tac-Toe.
            "game_easy": "Game rewards · Easy",
            "game_normal": "Game rewards · Normal",
            "game_hard": "Game rewards · Hard",
        }
        if value in labels:
            return labels[value]
        if value.startswith("game_connectfour_"):
            difficulty = value.removeprefix("game_connectfour_")
            return f"Connect 4 · {difficulty.title()}"
        if value.startswith("game_tictactoe_"):
            difficulty = value.removeprefix("game_tictactoe_")
            return f"Tic Tac Toe · {difficulty.title()}"
        return value.removeprefix("game_").replace("_", " ").title()
    if raw == "lottery_ticket":
        return "Tickets"
    if raw == "lottery_win":
        return "Lottery winnings"
    if raw.startswith("sea_animal_race_"):
        return raw.removeprefix("sea_animal_race_").replace("_", " ").title()
    if raw.startswith("luckyroll_"):
        return raw.removeprefix("luckyroll_").replace("_", " ").title()
    if raw.startswith("wager_"):
        action, _, game = raw.partition(":")
        if game.startswith("pvp_"):
            return action.removeprefix("wager_").replace("_", " ").title()
        action = action.removeprefix("wager_").replace("_", " ").title()
        return action or _currency_source_label(game)
    if raw.startswith("pvp_") or raw.startswith("pvp:"):
        action, separator, game = raw.partition(":")
        if separator:
            return action.removeprefix("pvp_").replace("_", " ").title()
        return action.removeprefix("pvp_").replace("_", " ").title()
    if raw.startswith("claim_"):
        return raw.removeprefix("claim_").replace("_", " ").title()
    if raw.startswith("reputation_"):
        return raw.removeprefix("reputation_").replace("_", " ").title()
    if raw == "fishie_supporter":
        return "Fishie Supporter"
    if raw == "birthday":
        return "Birthday reward"
    if raw in {"give", "give_sent", "give_received"}:
        return {
            "give": "Gifts",
            "give_sent": "Sent",
            "give_received": "Received",
        }[raw]
    if raw.startswith("give_"):
        return raw.removeprefix("give_").replace("_", " ").title()
    return _currency_source_label(raw)


def _earnings_source_sort_key(source: str) -> tuple[int, str]:
    """Sort game difficulty rows in the user-facing Easy/Normal/Hard order.

    Transaction sources are stored as stable machine keys and therefore sort
    alphabetically by default (which places ``Hard`` before ``Normal``).  Keep
    every other source's existing alphabetical ordering while giving the three
    difficulty labels an explicit order.
    """

    label = _earnings_source_detail_label(source)
    match = re.search(r"(?:·\s*)?(easy|normal|hard)(?:\s+mode)?$", label, re.I)
    if match is not None:
        difficulty_order = {"easy": 0, "normal": 1, "hard": 2}
        return difficulty_order[match.group(1).casefold()], label.casefold()
    return 3, label.casefold()


def _group_earnings_sources(
    sources: tuple[CurrencyEarningSource, ...],
) -> tuple[EarningsGroup, ...]:
    grouped: dict[str, tuple[str, list[CurrencyEarningSource]]] = {}
    for source in sources:
        key, label = _earnings_group_for_source(source.source)
        if key not in grouped:
            grouped[key] = (label, [])
        grouped[key][1].append(source)

    order = {key: index for index, key in enumerate(_EARNINGS_GROUP_ORDER)}
    result: list[EarningsGroup] = []
    for key, (label, grouped_sources) in grouped.items():
        result.append(
            EarningsGroup(
                key=key,
                label=label,
                sources=tuple(
                    sorted(
                        grouped_sources,
                        key=lambda source: _earnings_source_sort_key(source.source),
                    )
                ),
            )
        )
    result.sort(key=lambda group: (order.get(group.key, len(order)), group.label))
    return tuple(result)


def _earnings_totals_text(earned: int, lost: int) -> str:
    return (
        f"Earned: {earned:,} Coins\n"
        f"Lost: {lost:,} Coins\n"
        f"Net: {earned - lost:+,} Coins"
    )


class EarningsCategorySelect(discord.ui.Select):
    """Dropdown for the total page and grouped earnings pages."""

    def __init__(self, view: "EarningsView") -> None:
        self.earnings_view = view
        options = [
            discord.SelectOption(
                label="Total",
                value="total",
                description="View total Coins earned, lost, and net.",
                default=view.selected == "total",
            )
        ]
        for group in view.groups[:24]:
            description = f"Net {group.net:+,} Coins"
            options.append(
                discord.SelectOption(
                    label=group.label[:100],
                    value=group.key,
                    description=description[:100],
                    default=view.selected == group.key,
                )
            )
        super().__init__(
            placeholder="Choose an earnings category",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.earnings_view._check_user(interaction):
            return
        self.earnings_view.selected = self.values[0] if self.values else "total"
        self.earnings_view._render()
        await interaction.response.edit_message(
            view=self.earnings_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class EarningsView(discord.ui.LayoutView):
    """Components V2 earnings summary with grouped source pages."""

    def __init__(
        self,
        ctx: Context,
        user: discord.User,
        summary: CurrencyEarnings,
        *,
        accent_color: discord.Colour | int | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.user = user
        self.summary = summary
        self.groups = _group_earnings_sources(summary.sources)
        self.selected = "total"
        self.accent_color = accent_color
        self._render()

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this earnings view can use the dropdown.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _title(self, suffix: str | None = None) -> str:
        name = (
            "Your Coins earnings"
            if self.user.id == self.ctx.author.id
            else f"{discord.utils.escape_markdown(self.user.name)}'s Coins earnings"
        )
        return f"### {name}{f' · {suffix}' if suffix else ''}"

    def _render(self) -> None:
        self.clear_items()
        content: list[discord.ui.Item[Any]] = []
        if self.selected == "total":
            content.extend(
                (
                    discord.ui.TextDisplay(self._title()),
                    discord.ui.TextDisplay(
                        _earnings_totals_text(self.summary.earned, self.summary.lost)
                    ),
                )
            )
        else:
            group = next(
                (
                    candidate
                    for candidate in self.groups
                    if candidate.key == self.selected
                ),
                None,
            )
            if group is None:
                self.selected = "total"
                self._render()
                return
            content.extend(
                (
                    discord.ui.TextDisplay(self._title(group.label)),
                    discord.ui.TextDisplay(
                        _earnings_totals_text(group.earned, group.lost)
                    ),
                    discord.ui.Separator(),
                )
            )
            lines: list[str] = []
            for source in group.sources:
                changes: list[str] = []
                if source.earned:
                    changes.append(f"+{source.earned:,} earned")
                if source.lost:
                    changes.append(f"-{source.lost:,} lost")
                if changes:
                    lines.append(
                        f"**{discord.utils.escape_markdown(_earnings_source_detail_label(source.source))}:** "
                        + " · ".join(changes)
                    )
            content.append(
                discord.ui.TextDisplay("\n".join(lines) or "No transactions recorded.")
            )
        self.add_item(
            discord.ui.Container(
                *content,
                accent_color=self.accent_color,
            )
        )
        self.add_item(discord.ui.ActionRow(EarningsCategorySelect(self)))


class ShopCategorySelect(discord.ui.Select):
    """Switch between the available Coins shop categories."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        selected: str | None = None,
        command_prefix: str = "fish ",
        owned_badges: frozenset[str] = frozenset(),
        owned_titles: frozenset[str] = frozenset(),
        owned_colors: frozenset[str] = frozenset(),
        owned_racing: frozenset[str] = frozenset(),
    ) -> None:
        self.accent_color = accent_color
        self.command_prefix = command_prefix
        self.owned_badges = owned_badges
        self.owned_titles = owned_titles
        self.owned_colors = owned_colors
        self.owned_racing = owned_racing
        options = [
            discord.SelectOption(
                label="Badges",
                value="badges",
                description="Browse purchasable profile badges.",
                default=selected == "badges",
            ),
            discord.SelectOption(
                label="Titles",
                value="titles",
                description="Browse purchasable profile titles.",
                default=selected == "titles",
            ),
            discord.SelectOption(
                label="Colors",
                value="colors",
                description="Browse purchasable profile colors.",
                default=selected == "colors",
            ),
            discord.SelectOption(
                label="Lottery",
                value="lottery",
                description="Learn about lottery tickets and the hourly draw.",
                default=selected == "lottery",
            ),
            discord.SelectOption(
                label="Racing Emoji",
                value="racing-emoji",
                description="Choose an emoji for Sea Animal Race.",
                default=selected == "racing-emoji",
            ),
            discord.SelectOption(
                label="Rings",
                value="rings",
                description="Browse rings used for marriage.",
                default=selected == "rings",
            ),
        ]
        super().__init__(
            placeholder="Choose a shop category",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        category = self.values[0] if self.values else ""
        if category == "badges":
            view: discord.ui.LayoutView = BadgeShopView(
                accent_color=self.accent_color,
                command_prefix=self.command_prefix,
                owned_badges=self.owned_badges,
                owned_titles=self.owned_titles,
                owned_colors=self.owned_colors,
                owned_racing=self.owned_racing,
            )
        elif category == "titles":
            view = TitleShopView(
                accent_color=self.accent_color,
                command_prefix=self.command_prefix,
                owned_badges=self.owned_badges,
                owned_titles=self.owned_titles,
                owned_colors=self.owned_colors,
                owned_racing=self.owned_racing,
            )
        elif category == "racing-emoji":
            view = RacingEmojiShopView(
                accent_color=self.accent_color,
                command_prefix=self.command_prefix,
                owned_racing=self.owned_racing,
            )
        elif category == "colors":
            view = ColorShopView(
                accent_color=self.accent_color,
                command_prefix=self.command_prefix,
                owned_badges=self.owned_badges,
                owned_titles=self.owned_titles,
                owned_colors=self.owned_colors,
                owned_racing=self.owned_racing,
            )
        elif category == "lottery":
            view = LotteryShopView(
                accent_color=self.accent_color,
                command_prefix=self.command_prefix,
                owned_badges=self.owned_badges,
                owned_titles=self.owned_titles,
                owned_colors=self.owned_colors,
                owned_racing=self.owned_racing,
            )
        elif category == "rings":
            view = RingShopView(
                accent_color=self.accent_color,
                command_prefix=self.command_prefix,
            )
        else:  # pragma: no cover - Discord only sends configured option values.
            await interaction.response.send_message(
                "That shop category is unavailable.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.edit_message(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class BadgeShopView(discord.ui.LayoutView):
    """Components V2 presentation of the purchasable badge catalog."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
        owned_badges: frozenset[str] = frozenset(),
        owned_titles: frozenset[str] = frozenset(),
        owned_colors: frozenset[str] = frozenset(),
        owned_racing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(timeout=600)
        # Disabled catalog rows remain available for existing owners to sell
        # or manage, but are omitted from the public shop.
        offers = sorted(
            _badge_catalog_offers(enabled_only=True),
            key=lambda offer: -offer.price,
        )
        lines: list[str] = []
        for offer in offers:
            label = offer.description
            if offer.emoji and offer.kind != "custom_emoji":
                label = f"{offer.emoji} {label}"
            line = (
                f"{label} (`{offer.command_selector}`) · " f"**{offer.price:,} Coins**"
            )
            lines.append(
                f"~~{line}~~" if _shop_badge_owned(offer, owned_badges) else line
            )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Badge shop\n"
                    "Spend Coins to add one of these badges to your profile."
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(lines)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Purchase one with `{command_prefix}purchase badge <badge>`."
                ),
                accent_color=accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=accent_color,
                    selected="badges",
                    command_prefix=command_prefix,
                    owned_badges=owned_badges,
                    owned_titles=owned_titles,
                    owned_colors=owned_colors,
                    owned_racing=owned_racing,
                )
            )
        )


@dataclass(frozen=True, slots=True)
class TitleOffer:
    """A title sold independently from profile badges."""

    key: str
    price: int
    description: str
    selector: str | None = None
    category: str = "Misc titles"
    enabled: bool = True

    @property
    def command_selector(self) -> str:
        return self.selector or self.key


@dataclass(frozen=True, slots=True)
class RingOffer:
    """A stackable ring sold by the Coins shop."""

    key: str
    name: str
    emoji: str
    price: int
    enabled: bool = True


RING_OFFERS = tuple(RingOffer(**item) for item in SHOP_CATALOG["rings"])


class TitleShopView(discord.ui.LayoutView):
    """Components V2 presentation of the purchasable title catalog."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
        owned_titles: frozenset[str] = frozenset(),
        owned_badges: frozenset[str] = frozenset(),
        owned_colors: frozenset[str] = frozenset(),
        owned_racing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(timeout=600)
        self.accent_color = accent_color
        self.command_prefix = command_prefix
        self.owned_badges = owned_badges
        self.owned_titles = owned_titles
        self.owned_colors = owned_colors
        self.owned_racing = owned_racing
        self.category_index = 0
        self.page_index = 0
        self._render()

    @staticmethod
    def _categories() -> tuple[tuple[str, tuple[TitleOffer, ...]], ...]:
        """Return title categories in their stable shop order."""

        categories: list[tuple[str, tuple[TitleOffer, ...]]] = []
        configured = set(TITLE_SHOP_CATEGORY_ORDER)
        extra_categories = sorted(
            {
                offer.category
                for offer in _title_catalog_offers(enabled_only=True)
                if offer.category not in configured
            },
            key=str.casefold,
        )
        for category in (*TITLE_SHOP_CATEGORY_ORDER, *extra_categories):
            offers = tuple(
                sorted(
                    (
                        offer
                        for offer in _title_catalog_offers(enabled_only=True)
                        if offer.category == category
                    ),
                    key=lambda offer: (-offer.price, offer.description.casefold()),
                )
            )
            if offers:
                categories.append((category, offers))
        return tuple(categories)

    @staticmethod
    def _pages(offers: tuple[TitleOffer, ...]) -> tuple[tuple[TitleOffer, ...], ...]:
        """Split a category into 10–15 item pages where possible.

        Pages are kept in descending size order.  This makes the first page
        the fullest page and leaves any short remainder at the end (for
        example, 31 entries become ``15, 10, 6`` rather than ``15, 6, 10``).
        Categories with fewer than sixteen entries stay on one page.
        """

        if len(offers) <= 15:
            return (offers,)

        total = len(offers)
        # Ten entries is the normal page target.  For 16–19 entries, two
        # pages are still needed but the first page remains at ten so the
        # second page is never reduced below five entries.
        page_count = max(2, total // 10)
        if total < page_count * 10:
            sizes = [10, total - 10]
        else:
            remainder = total - page_count * 10
            sizes = [10] * page_count
            if remainder:
                if remainder <= 5:
                    # Move capacity from the final page to the first one.
                    # The result is 15, 10, ..., 5+remainder.
                    sizes[0] += remainder + (5 - remainder)
                    sizes[-1] -= 5 - remainder
                else:
                    # Fill the first page, then distribute any remaining
                    # entries from left to right to preserve descending
                    # sizes (15, 11, 10 rather than 15, 10, 11).
                    remaining = remainder
                    for index in range(page_count):
                        add = min(5, remaining)
                        sizes[index] += add
                        remaining -= add
                        if not remaining:
                            break

        pages: list[tuple[TitleOffer, ...]] = []
        offset = 0
        for size in sizes:
            pages.append(offers[offset : offset + size])
            offset += size
        return tuple(pages)

    def _render(self) -> None:
        self.clear_items()
        categories = self._categories()
        if not categories:  # pragma: no cover - the static catalog is non-empty.
            return
        self.category_index %= len(categories)
        category_name, offers = categories[self.category_index]
        pages = self._pages(offers)
        self.page_index %= len(pages)
        page_offers = pages[self.page_index]

        lines = [f"### {category_name}"]
        for offer in page_offers:
            display_name = offer.description
            if offer.category.casefold() == "emoticons":
                # Visible emoticon names are Markdown-sensitive (especially
                # underscores, which otherwise enable italics).
                # Escape only the visible name; the selector remains inside
                # backticks and therefore needs no escaping.
                display_name = discord.utils.escape_mentions(
                    discord.utils.escape_markdown(display_name)
                )
            line = (
                f"{display_name} (`{offer.command_selector}`) · "
                f"**{offer.price:,} Coins**"
            )
            lines.append(f"~~{line}~~" if offer.key in self.owned_titles else line)
        lines.append(f"-# Page {self.page_index + 1}/{len(pages)}")

        previous_category = discord.ui.Button(
            label="<<", style=discord.ButtonStyle.secondary
        )
        previous_page = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        next_page = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        next_category = discord.ui.Button(
            label=">>", style=discord.ButtonStyle.secondary
        )
        previous_category.callback = self._previous_category
        previous_page.callback = self._previous_page
        next_page.callback = self._next_page
        next_category.callback = self._next_category

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Title shop\n" "Spend Coins to add a title to your profile."
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(lines)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Purchase one with `{self.command_prefix}purchase title <title>`."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    previous_category,
                    previous_page,
                    next_page,
                    next_category,
                ),
                accent_color=self.accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=self.accent_color,
                    selected="titles",
                    command_prefix=self.command_prefix,
                    owned_badges=self.owned_badges,
                    owned_titles=self.owned_titles,
                    owned_colors=self.owned_colors,
                    owned_racing=self.owned_racing,
                )
            )
        )

    async def _previous_category(self, interaction: discord.Interaction) -> None:
        self.category_index = (self.category_index - 1) % len(self._categories())
        self.page_index = 0
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _next_category(self, interaction: discord.Interaction) -> None:
        self.category_index = (self.category_index + 1) % len(self._categories())
        self.page_index = 0
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        categories = self._categories()
        if self.page_index:
            self.page_index -= 1
        else:
            self.category_index = (self.category_index - 1) % len(categories)
            self.page_index = len(self._pages(categories[self.category_index][1])) - 1
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        categories = self._categories()
        _category_name, offers = categories[self.category_index]
        pages = self._pages(offers)
        if self.page_index + 1 < len(pages):
            self.page_index += 1
        else:
            self.category_index = (self.category_index + 1) % len(categories)
            self.page_index = 0
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )


@dataclass(frozen=True, slots=True)
class ColorOffer:
    """A preset or custom colour sold for Coins."""

    key: str
    price: int
    description: str
    hex_value: str | None = None
    selector: str | None = None
    enabled: bool = True

    @property
    def command_selector(self) -> str:
        return self.selector or self.key


@dataclass(frozen=True, slots=True)
class RacingEmojiCategory:
    """A price bucket shown in the Racing Emoji shop."""

    key: str
    label: str
    price: int
    samples: tuple[str, ...]


RACING_EMOJI_CATEGORIES = tuple(
    RacingEmojiCategory(
        item["key"], item["label"], item["price"], tuple(item["samples"])
    )
    for item in SHOP_CATALOG["racing_emoji"]
    if item["key"] != "custom"
)
RACING_EMOJI_CATEGORY_BY_KEY = {
    category.key: category for category in RACING_EMOJI_CATEGORIES
}
RACING_EMOJI_CUSTOM_PRICE = next(
    item["price"] for item in SHOP_CATALOG["racing_emoji"] if item["key"] == "custom"
)
RACING_EMOJI_BLOCKED_MARKERS = frozenset(
    {
        "square",
        "circle",
        "diamond",
        "triangle",
        "octagon",
        "rectangle",
    }
)
RACING_EMOJI_SEA_MARKERS = frozenset(
    {
        "fish",
        "octopus",
        "squid",
        "jellyfish",
        "shrimp",
        "lobster",
        "crab",
        "blowfish",
        "tropical_fish",
        "dolphin",
        "whale",
        "shark",
        "seal",
        "crocodile",
        "otter",
        "oyster",
        "coral",
        "shell",
    }
)
RACING_EMOJI_ANIMAL_MARKERS = frozenset(
    {
        "animal",
        "cat",
        "dog",
        "mouse",
        "hamster",
        "rabbit",
        "fox",
        "bear",
        "panda",
        "koala",
        "tiger",
        "lion",
        "cow",
        "pig",
        "frog",
        "monkey",
        "chicken",
        "penguin",
        "bird",
        "bug",
        "bee",
        "butterfly",
        "snail",
        "horse",
        "unicorn",
        "dragon",
        "dinosaur",
        "wolf",
        "boar",
        "deer",
        "elephant",
        "giraffe",
        "zebra",
        "gorilla",
        "orangutan",
        "camel",
        "llama",
        "raccoon",
        "badger",
        "skunk",
        "kangaroo",
        "hippopotamus",
        "rhinoceros",
        "mouse",
    }
)
RACING_EMOJI_FOOD_MARKERS = frozenset(
    {
        "apple",
        "avocado",
        "banana",
        "beans",
        "beer",
        "bread",
        "burger",
        "cake",
        "candy",
        "carrot",
        "cheese",
        "cherries",
        "chocolate",
        "cookie",
        "corn",
        "croissant",
        "cucumber",
        "cupcake",
        "doughnut",
        "egg",
        "fries",
        "garlic",
        "grape",
        "hotdog",
        "lemon",
        "mango",
        "meat",
        "melon",
        "mushroom",
        "noodles",
        "orange",
        "pancake",
        "peach",
        "pear",
        "pepper",
        "pie",
        "pizza",
        "popcorn",
        "potato",
        "ramen",
        "rice",
        "sandwich",
        "shaved_ice",
        "spaghetti",
        "strawberry",
        "sushi",
        "taco",
        "tomato",
        "watermelon",
        "wine",
    }
)
RACING_EMOJI_HEART_MARKERS = frozenset({"heart", "love", "cupid"})
RACING_EMOJI_FACE_MARKERS = frozenset(
    {
        "face",
        "smile",
        "grin",
        "laugh",
        "wink",
        "kiss",
        "cry",
        "sob",
        "angry",
        "rage",
        "scream",
        "thinking",
        "person",
        "man",
        "woman",
        "boy",
        "girl",
        "human",
        "head",
    }
)


def _racing_emoji_category(value: str) -> RacingEmojiCategory:
    """Classify a Unicode emoji into its Racing Emoji price bucket."""
    try:
        category = classify_racing_emoji(value).category
    except ValueError as error:
        raise commands.BadArgument(str(error)) from error
    category_key = {
        "sea_animal": "sea",
        "human_face": "face",
    }.get(category, category)
    return RACING_EMOJI_CATEGORY_BY_KEY[category_key]


class ColorShopView(discord.ui.LayoutView):
    """Components V2 presentation of the purchasable colour catalog."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
        owned_colors: frozenset[str] = frozenset(),
        owned_badges: frozenset[str] = frozenset(),
        owned_titles: frozenset[str] = frozenset(),
        owned_racing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(timeout=600)
        offers = sorted(
            _color_catalog_offers(enabled_only=True), key=lambda offer: -offer.price
        )
        lines: list[str] = []
        for offer in offers:
            suffix = f" · `{offer.hex_value}`" if offer.hex_value else ""
            line = (
                f"{offer.description} (`{offer.command_selector}`){suffix} · "
                f"**{offer.price:,} Coins**"
            )
            lines.append(f"~~{line}~~" if offer.key in owned_colors else line)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Color shop\n" "Spend Coins to add a profile color."
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(lines)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Purchase one with `{command_prefix}purchase color <color> "
                    "[hex/rgb]`."
                ),
                accent_color=accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=accent_color,
                    selected="colors",
                    command_prefix=command_prefix,
                    owned_badges=owned_badges,
                    owned_titles=owned_titles,
                    owned_colors=owned_colors,
                    owned_racing=owned_racing,
                )
            )
        )


class RacingEmojiShopView(discord.ui.LayoutView):
    """Components V2 presentation of Racing Emoji price categories."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
        owned_racing: frozenset[str] = frozenset(),
        owned_badges: frozenset[str] = frozenset(),
        owned_titles: frozenset[str] = frozenset(),
        owned_colors: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(timeout=600)
        # Keep each separator as a real Components V2 separator rather than
        # putting Markdown dashes into a TextDisplay.  This preserves the
        # compact shop layout on both desktop and mobile clients.
        content: list[Any] = [
            discord.ui.TextDisplay(
                "## Racing Emoji shop\n"
                "Spend Coins to use a specific emoji in racing games!"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"### Custom · **{RACING_EMOJI_CUSTOM_PRICE:,} Coins**\n"
                "Use your own custom emoji!\n"
                "-# *Fishie needs to be in that server!*"
            ),
        ]
        # Keep the shop consistent with the other purchase pages: expensive
        # categories come first, with a stable alphabetical tie-breaker.
        for category in sorted(
            RACING_EMOJI_CATEGORIES,
            key=lambda category: (-category.price, category.label.casefold()),
        ):
            samples = " ".join(category.samples[:10])
            owned_samples = {key for key in owned_racing if key in category.samples}
            # Samples are illustrative and can be struck through when the
            # caller owns that exact Unicode emoji.
            if owned_samples:
                samples = " ".join(
                    f"~~{sample}~~" if sample in owned_samples else sample
                    for sample in category.samples[:10]
                )
            content.extend(
                (
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        f"### {category.label} · **{category.price:,} Coins**\n{samples}"
                    ),
                )
            )
        content.extend(
            (
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Purchase one with `{command_prefix}purchase racing-emoji <emoji>`."
                ),
            )
        )
        self.add_item(
            discord.ui.Container(
                *content,
                accent_color=accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=accent_color,
                    selected="racing-emoji",
                    command_prefix=command_prefix,
                    owned_badges=owned_badges,
                    owned_titles=owned_titles,
                    owned_colors=owned_colors,
                    owned_racing=owned_racing,
                )
            )
        )


class RingShopView(discord.ui.LayoutView):
    """Components V2 presentation of the stackable ring catalog."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
    ) -> None:
        super().__init__(timeout=600)
        lines = [
            f"{offer.emoji} {offer.name} (`{offer.key}`) · "
            f"**{offer.price:,} Coins**"
            for offer in sorted(
                _ring_catalog_offers(enabled_only=True), key=lambda item: -item.price
            )
        ]
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Ring shop\n"
                    "Purchase rings to propose to another Fishie user. "
                    "You can own more than one of each ring."
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(lines)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Purchase one with `{command_prefix}purchase ring <ring>`."
                ),
                accent_color=accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=accent_color,
                    selected="rings",
                    command_prefix=command_prefix,
                )
            )
        )


class ShopLandingView(discord.ui.LayoutView):
    """Landing page shown by the base ``shop`` command."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
        owned_badges: frozenset[str] = frozenset(),
        owned_titles: frozenset[str] = frozenset(),
        owned_colors: frozenset[str] = frozenset(),
        owned_racing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Coin shop"),
                discord.ui.TextDisplay(
                    "Spend Coins on profile badges, titles, colors, rings, and lottery tickets.\n\n"
                    f"Buy items with `{command_prefix}purchase <category> <item>`\n\n"
                    "Choose a category below to browse the available items."
                ),
                accent_color=accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=accent_color,
                    command_prefix=command_prefix,
                    owned_badges=owned_badges,
                    owned_titles=owned_titles,
                    owned_colors=owned_colors,
                    owned_racing=owned_racing,
                )
            )
        )


class LotteryShopView(discord.ui.LayoutView):
    """Components V2 explanation page for the hourly lottery."""

    def __init__(
        self,
        *,
        accent_color: discord.Colour | int | None = None,
        command_prefix: str = "fish ",
        owned_badges: frozenset[str] = frozenset(),
        owned_titles: frozenset[str] = frozenset(),
        owned_colors: frozenset[str] = frozenset(),
        owned_racing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Lottery shop\n\n"
                    "Buy a ticket to participate in the hourly lottery! Every "
                    "ticket bought increases the prize pool, starting at 1,000. "
                    "Every hour a ticket is selected and the winner gets the "
                    "entire pool."
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Lottery ticket · **{LOTTERY_TICKET_PRICE:,} Coins**"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Purchase tickets with `{command_prefix}purchase ticket <amount>`."
                ),
                accent_color=accent_color,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                ShopCategorySelect(
                    accent_color=accent_color,
                    selected="lottery",
                    command_prefix=command_prefix,
                    owned_badges=owned_badges,
                    owned_titles=owned_titles,
                    owned_colors=owned_colors,
                    owned_racing=owned_racing,
                )
            )
        )


class LotteryTicketModal(discord.ui.Modal, title="Buy lottery tickets"):
    """Collect a ticket quantity from anyone viewing a lottery message."""

    amount = discord.ui.TextInput(
        label="Number of tickets",
        placeholder="Enter a whole number",
        required=True,
        max_length=6,
    )

    def __init__(self, view: "LotteryView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.view.buy_tickets(interaction, str(self.amount.value))


class LotteryView(discord.ui.LayoutView):
    """Live hourly lottery status with a public ticket purchase button."""

    def __init__(
        self,
        cog: "Currency",
        status: LotteryStatus,
        *,
        accent_color: discord.Colour | int | None = None,
        display_user_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.status = status
        self.display_user_id = int(display_user_id)
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._content())
        self.buy_button = discord.ui.Button(
            label="Buy ticket",
            style=discord.ButtonStyle.secondary,
            custom_id=f"lottery:buy:{self.display_user_id}",
        )
        self.buy_button.callback = self._open_modal
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Lottery"),
                discord.ui.Separator(),
                self.display,
                accent_color=accent_color,
            )
        )
        self.add_item(discord.ui.ActionRow(self.buy_button))

    def _content(self) -> str:
        next_draw = self.status.round_start + timedelta(hours=1)
        return (
            f"**Prize pool:** {self.status.prize_pool:,} Coins\n"
            f"**Tickets bought:** {self.status.total_tickets:,}\n"
            f"**Your tickets:** {self.status.user_tickets:,}\n"
            f"**Your chance:** {self.status.chance_percent:.2f}%\n\n"
            f"Tickets cost **{LOTTERY_TICKET_PRICE:,} Coins** each.\n"
            f"The winner is drawn {discord.utils.format_dt(next_draw, 'R')}."
        )

    def refresh(self, status: LotteryStatus) -> None:
        self.status = status
        self.display.content = self._content()

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LotteryTicketModal(self))

    async def buy_tickets(
        self, interaction: discord.Interaction, raw_amount: str
    ) -> None:
        """Purchase tickets for the clicking user and refresh this message."""

        # A database transaction can occasionally take longer than Discord's
        # three-second initial response window, so acknowledge the modal
        # before doing any wallet or lottery work.
        await interaction.response.defer()
        try:
            parsed_amount = parse_coin_amount(raw_amount)
            if parsed_amount == EVERYTHING_AMOUNT:
                raise CoinAmountError("Enter a ticket count.")
            amount = int(parsed_amount)
        except (CoinAmountError, TypeError, ValueError):
            await interaction.followup.send(
                "Enter a whole number of tickets.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            result = await self.cog.bot.currency.purchase_lottery_tickets(
                interaction.user.id, amount
            )
        except (InvalidAmount, InsufficientFunds) as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Keep the message's "Your tickets" row tied to the person who ran
        # ``lottery`` while updating the shared pool and chance after every
        # public purchase.
        status = await self.cog.bot.currency.get_lottery_status(self.display_user_id)
        self.refresh(status)
        message = interaction.message or self.message
        if message is not None:
            try:
                await message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, discord.NotFound):
                self.cog.bot.logger.warning(
                    "Could not refresh lottery message after ticket purchase",
                    exc_info=True,
                )
        await interaction.followup.send(
            f"Bought **{len(result.tickets):,} "
            f"{plural(len(result.tickets), False):lottery ticket}** for "
            f"**{len(result.tickets) * LOTTERY_TICKET_PRICE:,} Coins**.\n"
            f"-# *Prize pool: {result.prize_pool:,} Coins · "
            f"Wallet balance: {result.wallet_balance:,} Coins*",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class InventoryView(discord.ui.LayoutView):
    """Components V2 inventory browser for badges, titles, and colours."""

    PAGE_SIZE = 10

    def __init__(
        self,
        ctx: Context,
        entries: list[tuple[str, str]],
        *,
        display_user: discord.abc.User | None = None,
        accent_color: discord.Colour | int | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.entries = entries
        # ``ctx.author`` is the viewer (and the only person allowed to use
        # the paginator), while ``display_user`` is the inventory owner.  Keep
        # those roles separate so users can inspect somebody else's inventory
        # without handing control of the view to that person.
        self.display_user = display_user or ctx.author
        self.page = 0
        self.message: discord.Message | None = None
        self.accent_color = accent_color
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _render(self) -> None:
        self.clear_items()
        self.page %= self.page_count
        start = self.page * self.PAGE_SIZE
        page_entries = self.entries[start : start + self.PAGE_SIZE]
        lines = [
            "## "
            f"{discord.utils.escape_mentions(discord.utils.escape_markdown(self.display_user.display_name))}"
            "'s inventory"
        ]
        if not page_entries:
            lines.append("This inventory is empty.")
        else:
            last_category: str | None = None
            for index, (category, text) in enumerate(page_entries):
                if category != last_category:
                    if index:
                        lines.append("")
                    lines.append(f"### {category}")
                    last_category = category
                lines.append(text)
            lines.extend(("", f"-# Page {self.page + 1}/{self.page_count}"))
        content: list[discord.ui.Item[Any]] = []
        if not page_entries:
            content.append(discord.ui.TextDisplay("\n".join(lines)))
        else:
            content.append(discord.ui.TextDisplay(lines[0]))
            last_category = None
            for index, (category, text) in enumerate(page_entries):
                if category != last_category:
                    content.append(discord.ui.Separator())
                    content.append(discord.ui.TextDisplay(f"### {category}"))
                    last_category = category
                content.append(discord.ui.TextDisplay(text))
            content.append(discord.ui.Separator())
            content.append(
                discord.ui.TextDisplay(f"-# Page {self.page + 1}/{self.page_count}")
            )
        self.add_item(
            discord.ui.Container(
                *content,
                accent_color=self.accent_color,
            )
        )
        if self.page_count > 1:
            previous = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
            next_page = discord.ui.Button(
                label=">", style=discord.ButtonStyle.secondary
            )
            previous.callback = self._previous_page
            next_page.callback = self._next_page
            self.add_item(discord.ui.ActionRow(previous, next_page))

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "This inventory is not yours to browse.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        if not await self._check_user(interaction):
            return
        self.page = (self.page - 1) % self.page_count
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if not await self._check_user(interaction):
            return
        self.page = (self.page + 1) % self.page_count
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            for child in getattr(item, "children", ()):
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass


PURCHASABLE_BADGES = tuple(BadgeOffer(**item) for item in SHOP_CATALOG["badges"])
PURCHASABLE_TITLES = tuple(TitleOffer(**item) for item in SHOP_CATALOG["titles"])


TITLE_SHOP_CATEGORY_ORDER: tuple[str, ...] = tuple(SHOP_CATALOG["title_categories"])

# Catalog tuples are only the built-in seed data.  Once the currency cog has
# loaded, these caches contain every catalog row (including rows added by an
# owner command) and preserve each row's enabled flag.  Keeping a static
# fallback makes the shop helpers useful in tests and while the cog is still
# starting, without putting a database query on every command lookup or view
# refresh.
_TITLE_CATALOG_CACHE: tuple[TitleOffer, ...] | None = None
_BADGE_CATALOG_CACHE: tuple[BadgeOffer, ...] | None = None
_COLOR_CATALOG_CACHE: tuple[ColorOffer, ...] | None = None
_RING_CATALOG_CACHE: tuple[RingOffer, ...] | None = None


def _color_catalog_offers(*, enabled_only: bool = False) -> tuple[ColorOffer, ...]:
    offers = (
        _COLOR_CATALOG_CACHE if _COLOR_CATALOG_CACHE is not None else PURCHASABLE_COLORS
    )
    return tuple(offer for offer in offers if offer.enabled) if enabled_only else offers


def _ring_catalog_offers(*, enabled_only: bool = False) -> tuple[RingOffer, ...]:
    offers = _RING_CATALOG_CACHE if _RING_CATALOG_CACHE is not None else RING_OFFERS
    return tuple(offer for offer in offers if offer.enabled) if enabled_only else offers


def _title_catalog_offers(*, enabled_only: bool = False) -> tuple[TitleOffer, ...]:
    offers = (
        _TITLE_CATALOG_CACHE if _TITLE_CATALOG_CACHE is not None else PURCHASABLE_TITLES
    )
    if enabled_only:
        return tuple(offer for offer in offers if offer.enabled)
    return offers


def _badge_catalog_offers(*, enabled_only: bool = False) -> tuple[BadgeOffer, ...]:
    offers = (
        _BADGE_CATALOG_CACHE if _BADGE_CATALOG_CACHE is not None else PURCHASABLE_BADGES
    )
    if enabled_only:
        return tuple(offer for offer in offers if offer.enabled)
    return offers


def _catalog_row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read a catalog row from an asyncpg record or a test mapping."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


PURCHASABLE_COLORS = tuple(ColorOffer(**item) for item in SHOP_CATALOG["colors"])

_BADGE_OFFER_ALIASES = {
    "fish": "fish",
    "🐟": "fish",
    "nazar": "amulet",
    "amulet": "amulet",
    "hamsa": "amulet",
    "🪬": "amulet",
    "flag": "flag",
    "flags": "flag",
    "🇺🇳": "flag",
    "alien": "alien",
    "👽": "alien",
    "custom": "custom",
    "smiling-imp": "smiling_imp",
    "smiling_imp": "smiling_imp",
    "😈": "smiling_imp",
    "imp": "imp",
    "👿": "imp",
    "seal": "seal",
    "🦭": "seal",
    "cat": "cat",
    "🐱": "cat",
    "dog": "dog",
    "🐶": "dog",
    "mouse": "mouse",
    "🐭": "mouse",
    "hamster": "hamster",
    "🐹": "hamster",
    "rabbit": "rabbit",
    "🐰": "rabbit",
    "fox": "fox",
    "🦊": "fox",
}
_TITLE_OFFER_ALIASES = {
    "custom": "custom_title",
    "custom-title": "custom_title",
    "customtitle": "custom_title",
    "title": "custom_title",
    "dr-pepper": "dr_pepper",
    "dr-pepper-connoisseur": "dr_pepper",
    "drpepper": "dr_pepper",
    "monarch": "monarch",
    "mudae": "mudae_enjoyer",
    "mudae-enjoyer": "mudae_enjoyer",
    "six-seven": "six_seven",
    "sixseven": "six_seven",
    "fishie": "fishie",
    "epic": "epic",
    "cool": "cool",
    "bot": "bot",
    "fan": "fan",
    "player": "player",
}
_COLOR_OFFER_ALIASES = {
    "red": "red",
    "orange": "orange",
    "yellow": "yellow",
    "green": "green",
    "blue": "blue",
    "purple": "purple",
    "pink": "pink",
    "gray": "gray",
    "grey": "gray",
    "white": "white",
    "black": "black",
    "custom": "custom",
    "custom-color": "custom",
    "customcolor": "custom",
}
_CUSTOM_EMOJI_RE = re.compile(
    r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_~]+):(?P<id>\d+)>$"
)
_HEX_COLOR_RE = re.compile(r"^(?:#|0x)?(?P<hex>[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_COLOR_RE = re.compile(
    r"^(?:rgb\(\s*)?(?P<red>\d{1,3})\s*[, ]\s*"
    r"(?P<green>\d{1,3})\s*[, ]\s*(?P<blue>\d{1,3})\s*\)?$",
    re.IGNORECASE,
)
_PURCHASE_DISPLAY_NAMES = {
    "fish": "Fishie",
    "amulet": "Hamsa",
    "alien": "Alien",
}
_FLAG_BASES = frozenset("🏳🏴🏁🚩")
_FLAG_SEQUENCE_SYMBOLS = frozenset("☠⚧🌈")
_FLAG_VARIATION_AND_JOINERS = frozenset({"\ufe0e", "\ufe0f", "\u200d"})


def _unicode_flag(value: str) -> bool:
    """Return whether *value* is a Unicode flag emoji.

    Country flags are represented by two regional-indicator code points.
    Discord also accepts standalone/ZWJ flag sequences (such as pirate,
    rainbow, transgender, and subdivision flags), so those are accepted
    without treating arbitrary emoji or custom Discord markup as a flag.
    """

    value = value.strip()
    if len(value) == 2 and all(
        "\U0001f1e6" <= character <= "\U0001f1ff" for character in value
    ):
        return True
    if not value or not any(character in _FLAG_BASES for character in value):
        return False
    tag_characters = {chr(codepoint) for codepoint in range(0xE0020, 0xE007F)} | {
        "\U000e007f"
    }
    allowed = _FLAG_BASES | _FLAG_SEQUENCE_SYMBOLS | _FLAG_VARIATION_AND_JOINERS
    allowed |= tag_characters
    return all(character in allowed for character in value)


_SPECIAL_FLAG_NAMES = {
    "🏳️‍🌈": "Rainbow flag",
    "🏳️‍⚧️": "Trans flag",
    "🏴‍☠️": "Pirate flag",
    "🏳️": "White flag",
    "🏴": "Black flag",
    "🏁": "Chequered flag",
    "🚩": "Triangular flag",
}
_FLAG_TAG_NAMES = {
    "gbeng": "England",
    "gbsct": "Scotland",
    "gbwls": "Wales",
    "gbnir": "Northern Ireland",
}


def _flag_display_name(value: str) -> str:
    """Return a human-readable name for a Unicode flag badge.

    Regional-indicator flags are resolved through ``pycountry`` so the badge
    text remains useful instead of exposing an emoji name.  Discord's
    subdivision and common ZWJ flags are handled locally because they are not
    ISO-3166 country codes.
    """

    normalized = value.strip()
    special = _SPECIAL_FLAG_NAMES.get(normalized)
    if special is not None:
        return special
    # Subdivision flags encode their name with tag characters.  Ignore the
    # variation selector and terminating tag when reading the code.
    tag_code = "".join(
        chr(ord(character) - 0xE0000)
        for character in normalized
        if 0xE0061 <= ord(character) <= 0xE007A
    )
    if tag_code:
        subdivision = _FLAG_TAG_NAMES.get(tag_code.casefold())
        if subdivision is not None:
            return subdivision
    if len(normalized) == 2 and all(
        "\U0001f1e6" <= character <= "\U0001f1ff" for character in normalized
    ):
        code = "".join(
            chr(ord(character) - 0x1F1E6 + ord("A")) for character in normalized
        )
        country = pycountry.countries.get(alpha_2=code)
        if country is not None:
            # Discord users generally refer to this by its full common name.
            if code == "US":
                return "United States of America"
            return str(country.name)
        return f"{code} flag"
    # This should only be reached for a valid but uncommon flag sequence.
    return (
        " ".join(
            part.capitalize()
            for part in unicodedata.normalize("NFKD", normalized).split()
        )
        or "Unicode flag"
    )


def _shop_badge_owned(offer: BadgeOffer, owned: frozenset[str]) -> bool:
    """Return whether any purchased badge represented by *offer* is owned."""

    if offer.kind == "flag":
        return "flag" in owned or any(key.startswith("purchase:flag:") for key in owned)
    if offer.kind == "custom_emoji":
        return "custom" in owned or any(
            key.startswith("purchase:custom:") for key in owned
        )
    return offer.key in owned or f"purchase:{offer.key}" in owned


class Currency(Cog):
    """Check your balance, work, buy items and more."""

    emoji = discord.PartialEmoji(name="🪙")

    def __init__(self, bot: Fishie) -> None:
        self.bot = bot
        for command in self.__cog_commands__:
            if command.name == "lottery":
                command.extras["help_category"] = "Games"
        # A user may have one active work task at a time.  The token is kept
        # separate from the view so a late interaction from an expired view
        # cannot claim a second reward.
        self._work_sessions: dict[int, str] = {}
        self._work_wordle_games: dict[tuple[int, int], Any] = {}
        self._work_math_tasks: dict[tuple[int, int], WorkMathView] = {}

    async def refresh_catalog_cache(self) -> None:
        """Refresh the in-memory shop catalog from the database.

        Catalog rows are intentionally data rather than migrations.  This
        method is also public so a future owner command can insert, edit, or
        disable an offer and refresh the shop immediately without waiting for
        a restart.  Disabled rows stay in the cache for ownership/sale
        lookups, while shop views filter them out.
        """

        global _BADGE_CATALOG_CACHE, _TITLE_CATALOG_CACHE, _COLOR_CATALOG_CACHE, _RING_CATALOG_CACHE

        title_rows = await self.bot.pool.fetch("""
            SELECT title_key, display_name, price, category, enabled
            FROM title_catalog
            ORDER BY title_key
            """)
        badge_rows = await self.bot.pool.fetch("""
            SELECT badge_key, category, display_name, emoji_name, emoji_id,
                   is_custom, unicode, animated, price, enabled
            FROM badge_catalog
            WHERE category = 'purchase'
            ORDER BY badge_key
            """)

        title_seeds = {offer.key: offer for offer in PURCHASABLE_TITLES}
        titles: list[TitleOffer] = []
        for row in title_rows:
            key = str(_catalog_row_value(row, "title_key", "")).strip()
            if not key:
                continue
            seed = title_seeds.get(key)
            try:
                price = int(_catalog_row_value(row, "price", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            display_name = str(
                _catalog_row_value(
                    row,
                    "display_name",
                    seed.description if seed is not None else key,
                )
                or (seed.description if seed is not None else key)
            ).strip()
            category = str(
                _catalog_row_value(
                    row,
                    "category",
                    seed.category if seed is not None else "Misc titles",
                )
                or (seed.category if seed is not None else "Misc titles")
            ).strip()
            titles.append(
                TitleOffer(
                    key=key,
                    price=price,
                    description=display_name,
                    selector=seed.selector if seed is not None else None,
                    category=category,
                    enabled=bool(_catalog_row_value(row, "enabled", True)),
                )
            )

        badge_seeds = {offer.key: offer for offer in PURCHASABLE_BADGES}
        badges: list[BadgeOffer] = []
        for row in badge_rows:
            catalog_key = str(_catalog_row_value(row, "badge_key", "")).strip()
            if not catalog_key.startswith("purchase:"):
                continue
            key = catalog_key.removeprefix("purchase:")
            seed = badge_seeds.get(key)
            display_name = str(
                _catalog_row_value(
                    row,
                    "display_name",
                    seed.description if seed is not None else key,
                )
                or (seed.description if seed is not None else key)
            ).strip()
            emoji_name = str(
                _catalog_row_value(
                    row,
                    "emoji_name",
                    seed.emoji if seed is not None else "",
                )
                or (seed.emoji if seed is not None else "")
            )
            if seed is not None:
                kind = seed.kind
                selector = seed.selector
            elif bool(_catalog_row_value(row, "is_custom", False)):
                kind = "custom_emoji"
                selector = None
            elif key == "flag":
                kind = "flag"
                selector = None
            else:
                kind = "emoji"
                selector = None
            try:
                price = int(_catalog_row_value(row, "price", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            badges.append(
                BadgeOffer(
                    key=key,
                    emoji=emoji_name,
                    price=price,
                    description=display_name,
                    kind=kind,
                    selector=selector,
                    enabled=bool(_catalog_row_value(row, "enabled", True)),
                )
            )

        color_seeds = {offer.key: offer for offer in PURCHASABLE_COLORS}
        colors = tuple(
            ColorOffer(
                key=row["color_key"],
                description=row["display_name"],
                price=row["price"],
                hex_value=None if row["color_key"] == "custom" else row["hex_value"],
                selector=(
                    color_seeds[row["color_key"]].selector
                    if row["color_key"] in color_seeds
                    else None
                ),
                enabled=row["enabled"],
            )
            for row in await self.bot.pool.fetch(
                "SELECT * FROM color_catalog ORDER BY color_key"
            )
        )
        rings = tuple(
            RingOffer(
                row["ring_key"],
                row["display_name"],
                row["display"],
                row["price"],
                row["enabled"],
            )
            for row in await self.bot.pool.fetch(
                "SELECT * FROM ring_catalog ORDER BY ring_key"
            )
        )
        _TITLE_CATALOG_CACHE = tuple(titles)
        _BADGE_CATALOG_CACHE = tuple(badges)
        _COLOR_CATALOG_CACHE = colors
        _RING_CATALOG_CACHE = rings

    async def sync_catalogs(self) -> None:
        """Seed built-in offers, then cache all database catalog rows.

        Prices, labels, and categories follow the editable JSON seed list.
        Existing ``enabled`` values remain database-owned so an owner command
        can retire or restore an offer and that choice survives a restart.
        Rows not present in the list are preserved for future owner-created
        offers, which can call :meth:`refresh_catalog_cache` without a
        migration.
        """

        async with self.bot.pool.acquire() as connection:
            await sync_shop_catalog(connection)
        await self.refresh_catalog_cache()

    async def set_title_catalog_enabled(self, title_key: str, enabled: bool) -> bool:
        """Enable or retire one title without creating a migration."""

        result = await self.bot.pool.execute(
            "UPDATE title_catalog SET enabled = $2 WHERE title_key = $1",
            str(title_key).strip(),
            bool(enabled),
        )
        await self.refresh_catalog_cache()
        return str(result).endswith("1")

    async def set_badge_catalog_enabled(self, badge_key: str, enabled: bool) -> bool:
        """Enable or retire one purchasable badge without a migration."""

        key = str(badge_key).strip()
        catalog_key = key if key.startswith("purchase:") else f"purchase:{key}"
        result = await self.bot.pool.execute(
            """
            UPDATE badge_catalog
            SET enabled = $2
            WHERE badge_key = $1 AND category = 'purchase'
            """,
            catalog_key,
            bool(enabled),
        )
        await self.refresh_catalog_cache()
        return str(result).endswith("1")

    async def cog_check(self, ctx: Context) -> bool:
        """Require currency tracking before persisting wallet activity.

        Currency commands create or update wallets, wagers, purchases, and
        reward history.  Keeping this guard at cog level makes the setting
        apply uniformly to daily/weekly claims, games, shop actions, and
        profile views without duplicating checks in every command.
        """

        cache = getattr(self.bot, "db_cache", None)
        enabled = True
        checker = getattr(cache, "user_currency_tracking_enabled", None)
        if callable(checker):
            enabled = bool(checker(ctx.author.id))
        if enabled:
            return True
        await ctx.send(
            "Currency tracking is disabled. Enable it from `settings tracking` "
            "before using Coins commands.",
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    # ``profile`` is the app-command entry point for profile-related actions.
    # The original top-level text commands remain below for backwards
    # compatibility, while these wrappers provide a single discoverable
    # slash-command namespace.
    @commands.hybrid_group(name="profile", fallback="info")
    @app_commands.describe(user="User whose profile to view (defaults to yourself).")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def profile(
        self,
        ctx: Context,
        *,
        user: discord.User = commands.Author,
    ) -> None:
        """Manage your profile, inventory, XP, and game limits."""

        await self._send_profile_card(ctx, user)

    @profile.command(name="description")
    @app_commands.describe(
        description=(
            "A short description for your profile card; leave empty to delete it."
        )
    )
    async def profile_description(
        self, ctx: Context, *, description: str | None = None
    ) -> None:
        """Set or clear the description shown on your profile card."""

        await self._set_profile_description(ctx, description)

    @profile.group(name="badges", fallback="list")
    @app_commands.describe(user="User whose badges should be shown.")
    async def profile_badges(
        self,
        ctx: Context,
        *,
        user: discord.User = commands.Author,
    ) -> None:
        """Browse profile badges."""

        info = self.bot.get_cog("Info")
        sender = getattr(info, "_send_badges", None)
        if not callable(sender):
            raise commands.CommandError("The badge service is unavailable.")
        await cast(Callable[..., Awaitable[Any]], sender)(ctx, user)

    @profile_badges.command(name="order")
    @app_commands.describe(
        badge_order="Badge names, emojis, or IDs in the order they should appear."
    )
    async def profile_badges_order(
        self, ctx: Context, *, badge_order: str = ""
    ) -> None:
        """Set the order of your profile badges."""

        info = self.bot.get_cog("Info")
        setter = getattr(info, "_set_badge_order", None)
        if not callable(setter):
            raise commands.CommandError("The badge service is unavailable.")
        await cast(Callable[..., Awaitable[Any]], setter)(ctx, badge_order)

    @profile.command(name="inventory", aliases=("inv",))
    @app_commands.describe(user="User whose inventory to view (defaults to yourself).")
    async def profile_inventory(
        self,
        ctx: Context,
        user: discord.User = commands.Author,
    ) -> None:
        """Browse a user's profile inventory."""

        await self._send_inventory(ctx, user)

    @profile.group(name="equip", fallback="help")
    async def profile_equip(self, ctx: Context) -> None:
        """Equip an owned profile item."""

        await ctx.send_help(ctx.command)

    @profile_equip.command(name="title")
    @app_commands.describe(title="The title you want to equip.")
    async def profile_equip_title(self, ctx: Context, *, title: str) -> None:
        """Equip one of your purchased titles."""

        await self._equip_title(ctx, title)

    @profile_equip.command(name="color", aliases=("colour",))
    @app_commands.describe(
        color="The colour you want to equip.",
        value="Optional hex or RGB value when editing your custom colour.",
    )
    async def profile_equip_color(
        self, ctx: Context, color: str, value: str | None = None
    ) -> None:
        """Equip a purchased colour."""

        await self._equip_color(ctx, color, value)

    @profile_equip.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    @app_commands.describe(emoji="The racing emoji you want to equip.")
    async def profile_equip_racing_emoji(self, ctx: Context, *, emoji: str) -> None:
        """Equip one of your purchased racing emojis."""

        await self._equip_racing_emoji(ctx, emoji)

    @profile_equip.command(name="ring")
    @app_commands.describe(ring="Ring ID, name, or emoji to equip.")
    async def profile_equip_ring(self, ctx: Context, *, ring: str) -> None:
        """Equip one of your rings after getting married."""

        await self._equip_ring(ctx, ring)

    @profile.group(name="unequip", fallback="help")
    async def profile_unequip(self, ctx: Context) -> None:
        """Unequip a profile item."""

        await ctx.send_help(ctx.command)

    @profile_unequip.command(name="title")
    async def profile_unequip_title(self, ctx: Context) -> None:
        """Unequip your current title."""

        await self._unequip_title(ctx)

    @profile_unequip.command(name="color", aliases=("colour",))
    async def profile_unequip_color(self, ctx: Context) -> None:
        """Unequip your current colour."""

        await self._unequip_color(ctx)

    @profile_unequip.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    async def profile_unequip_racing_emoji(self, ctx: Context) -> None:
        """Unequip your current racing emoji."""

        await self._unequip_racing_emoji(ctx)

    @profile.command(name="earnings")
    @app_commands.describe(
        user="User whose Coins earnings to check (defaults to yourself)."
    )
    async def profile_earnings(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="User whose Coins earnings to check (defaults to yourself).",
        ),
    ) -> None:
        """Show a user's Coins earnings and losses."""

        await self._send_earnings(ctx, user)

    @profile.command(name="xp")
    @app_commands.describe(user="User whose XP to check (defaults to yourself).")
    async def profile_xp(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="User whose XP to check (defaults to yourself).",
        ),
    ) -> None:
        """Show a user's XP and active bonuses."""

        await self._send_xp(ctx, user)

    @commands.command(name="limits")
    async def limits(self, ctx: Context) -> None:
        """Show today's capped minigame rewards and remaining limits."""

        await self._send_limits(ctx)

    @profile.command(name="limits")
    async def profile_limits(self, ctx: Context) -> None:
        """Show today's capped minigame rewards and remaining limits."""

        await self._send_limits(ctx)

    async def cog_load(self) -> None:
        if is_legacy_instance(self.bot):
            self.bot.logger.info(
                "Skipping shared currency schedulers on legacy bot instance"
            )
            return
        try:
            await self.sync_catalogs()
        except Exception:
            # A catalog refresh must never prevent the rest of the currency
            # cog from loading.  The static JSON seed remains available until
            # the database is reachable and a future refresh can retry it.
            self.bot.logger.exception("Failed to synchronize shop catalog")
        self.recover_stale_wagers.start()
        self.refresh_badges.start()
        self.lottery_draws.start()

    async def cog_unload(self) -> None:
        self.recover_stale_wagers.cancel()
        self.refresh_badges.cancel()
        self.lottery_draws.cancel()
        self._work_sessions.clear()
        self._work_wordle_games.clear()
        self._work_math_tasks.clear()

    @tasks.loop(minutes=10)
    async def refresh_badges(self) -> None:
        """Keep leaderboard badges and unavailable emoji refunds current."""

        try:
            await refresh_stat_badges(self.bot)
            revoke = getattr(self.bot.badges, "revoke_unavailable_custom_badges", None)
            if revoke is not None:
                await revoke({emoji.id for emoji in self.bot.emojis})
        except Exception:
            self.bot.logger.exception("Failed to refresh badges")

    @refresh_badges.before_loop
    async def before_refresh_badges(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def recover_stale_wagers(self) -> None:
        try:
            # A responsive Higher or Lower game may remain open longer than the
            # stale threshold. Keep the durable cleanup from touching wagers
            # that the currently loaded Fun cog still owns in memory.
            active_wager_ids: set[int] = set()
            fun = self.bot.get_cog("Fun")
            for attribute in (
                "_higher_or_lower_games",
                "_heads_or_tails_games",
                "_rock_paper_scissors_games",
                "_mines_games",
                "_crash_games",
                "_blackjack_games",
            ):
                games = getattr(fun, attribute, {})
                for game in games.values():
                    # Blackjack can reserve a second wager when the player
                    # doubles. Keep every reservation protected while its
                    # in-memory game is still alive.
                    wager_ids = getattr(game, "wager_ids", None)
                    if wager_ids:
                        active_wager_ids.update(int(wager_id) for wager_id in wager_ids)
                    else:
                        wager_id = getattr(game, "wager_id", None)
                        if wager_id is not None:
                            active_wager_ids.add(int(wager_id))
            recovered = await self.bot.currency.recover_stale_wagers(
                older_than=timedelta(minutes=15),
                exclude_wager_ids=active_wager_ids,
            )
        except Exception:
            self.bot.logger.exception("Failed to recover stale currency wagers")
            return
        if recovered:
            self.bot.logger.info("Refunded %s", f"{plural(recovered):wager}")

    @recover_stale_wagers.before_loop
    async def before_recover_stale_wagers(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def lottery_draws(self) -> None:
        """Draw every open round that ended before the current UTC hour.

        Looking up all stale open rounds also lets a bot that was offline for
        several hours settle each missed draw instead of silently leaving old
        tickets stranded.
        """

        try:
            current_period = lottery_period_start()
            rows = await self.bot.pool.fetch(
                """
                SELECT round_start
                FROM lottery_rounds
                WHERE status = 'open' AND round_start < $1
                ORDER BY round_start
                """,
                current_period,
            )
        except Exception:
            self.bot.logger.exception("Failed to load hourly lottery rounds")
            return
        for row in rows:
            try:
                result = await self.bot.currency.draw_lottery_round(row["round_start"])
                if result is not None:
                    await self._announce_lottery_draw(result)
            except Exception:
                self.bot.logger.exception(
                    "Failed to draw hourly lottery round %s", row["round_start"]
                )

    @lottery_draws.before_loop
    async def before_lottery_draws(self) -> None:
        await self.bot.wait_until_ready()

    async def _announce_lottery_draw(self, result: LotteryDraw) -> None:
        """DM every participant the committed outcome of a lottery round."""

        winner = result.winning_ticket
        if winner is None:
            return
        by_user: dict[int, list[str]] = {}
        for ticket in result.tickets:
            by_user.setdefault(ticket.user_id, []).append(ticket.ticket_digits)
        winner_digits = winner.ticket_digits
        for user_id, digits in by_user.items():
            try:
                user = self.bot.get_user(user_id)
                if user is None:
                    user = await self.bot.fetch_user(user_id)
                if user_id == winner.user_id:
                    content = (
                        "🎉 You won the hourly lottery! Your ticket "
                        f"`{winner_digits}` won **{result.prize_pool:,} Coins**.\n"
                        f"You bought {plural(len(digits)):ticket}."
                    )
                else:
                    content = (
                        "The hourly lottery draw has ended. You did not get the "
                        f"winning ticket, which was `{winner_digits}`. Better luck "
                        f"next time.\nYou bought {plural(len(digits)):ticket}."
                    )
                await user.send(
                    content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                self.bot.logger.info("Could not DM lottery result to user %s", user_id)

    async def _claim(
        self,
        ctx: Context,
        claim_type: ClaimType,
        base_amount: int,
    ) -> None:
        streak_bonus_per_period = 10 if claim_type == "daily" else 20
        result = await self.bot.currency.claim(
            ctx.author.id,
            claim_type,
            base_amount,
            0,
            streak_bonus_per_period=streak_bonus_per_period,
        )
        if not result.claimed:
            reset = next_claim_reset(claim_type)
            await ctx.send(
                f"You already claimed your {claim_type} Coins. "
                f"You can claim them again {discord.utils.format_dt(reset, 'R')}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        bonuses: list[str] = []
        if result.bonus_amount:
            bonuses.append(f"+{result.bonus_amount:,} reputation bonus")
        if result.streak_bonus_amount:
            label = "daily" if claim_type == "daily" else "weekly"
            bonuses.append(f"+{result.streak_bonus_amount:,} {label} streak bonus")
        bonus_text = f" ({' · '.join(bonuses)})" if bonuses else ""
        await ctx.send(
            f"You claimed **{result.amount:,} Coins**{bonus_text}.\n"
            f"-# Wallet balance: {result.wallet.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _shop_owned(
        self, user_id: int
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        """Load the caller's shop ownership once for a shop page.

        Shop pages are read-only snapshots.  Keeping the ownership keys on the
        view means category switches do not perform another database query and
        still let the page strike through items the caller already owns.
        """

        badges: list[Any] = []
        service = getattr(self.bot, "badges", None) or getattr(
            self.bot, "badge_service", None
        )
        owned_badges = getattr(service, "owned", None)
        if callable(owned_badges):
            try:
                fetch_badges = cast(Callable[[int], Awaitable[Any]], owned_badges)
                badges = list(await fetch_badges(int(user_id)))
            except Exception:
                self.bot.logger.exception("Failed to load shop badge ownership")

        titles: list[Any] = []
        owned_titles = getattr(self.bot.currency, "owned_titles", None)
        if callable(owned_titles):
            try:
                fetch_titles = cast(Callable[[int], Awaitable[Any]], owned_titles)
                titles = list(await fetch_titles(int(user_id)))
            except Exception:
                self.bot.logger.exception("Failed to load shop title ownership")

        colors: list[Any] = []
        owned_colors = getattr(self.bot.currency, "owned_colors", None)
        if callable(owned_colors):
            try:
                fetch_colors = cast(Callable[[int], Awaitable[Any]], owned_colors)
                colors = list(await fetch_colors(int(user_id)))
            except Exception:
                self.bot.logger.exception("Failed to load shop colour ownership")

        badge_keys = frozenset(
            str(self._badge_result_value(row, "badge_key", ""))
            for row in badges
            if bool(self._badge_result_value(row, "active", True))
        )
        title_keys = frozenset(
            str(self._badge_result_value(row, "title_key", ""))
            for row in titles
            if bool(self._badge_result_value(row, "active", True))
        )
        color_keys = frozenset(
            str(self._badge_result_value(row, "color_key", ""))
            for row in colors
            if bool(self._badge_result_value(row, "active", True))
        )
        return badge_keys, title_keys, color_keys

    async def _shop_racing_owned(self, user_id: int) -> frozenset[str]:
        """Load active Racing Emoji ownership for a shop snapshot."""

        owned = getattr(self.bot.currency, "owned_racing_emojis", None)
        if not callable(owned):
            return frozenset()
        try:
            fetch = cast(Callable[[int], Awaitable[Any]], owned)
            rows = await fetch(int(user_id))
        except Exception:
            self.bot.logger.exception("Failed to load racing emoji ownership")
            return frozenset()
        return frozenset(
            str(self._badge_result_value(row, "emoji_key", ""))
            for row in rows
            if bool(self._badge_result_value(row, "active", True))
        )

    @staticmethod
    def _ring_offer(selector: str) -> RingOffer | None:
        """Resolve a ring by key, display name, emoji, or custom emoji ID."""

        raw = str(selector).strip()
        if not raw:
            return None
        partial = discord.PartialEmoji.from_str(raw)
        emoji_id = int(partial.id) if partial.id is not None else None
        folded = re.sub(r"[\s_-]+", "-", raw.casefold()).strip(":-")
        compact = re.sub(r"[^a-z0-9]+", "", raw.casefold())
        for offer in _ring_catalog_offers():
            offer_partial = discord.PartialEmoji.from_str(offer.emoji)
            if emoji_id is not None and offer_partial.id == emoji_id:
                return offer
            if raw.isdecimal() and offer_partial.id == int(raw):
                return offer
            for candidate in (offer.key, offer.name, offer.emoji):
                candidate_folded = re.sub(r"[\s_-]+", "-", candidate.casefold()).strip(
                    ":-"
                )
                candidate_compact = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
                if folded == candidate_folded or (
                    compact and candidate_compact == compact
                ):
                    return offer
        return None

    async def _owned_ring(self, user_id: int, selector: str) -> Any:
        """Resolve one ring stack from a user's inventory."""

        offer = self._ring_offer(selector)
        rows = await self.bot.currency.owned_rings(int(user_id))
        for row in rows:
            if (
                offer is not None
                and str(self._badge_result_value(row, "ring_key", "")) == offer.key
            ):
                return row
        raise commands.BadArgument("You do not own that ring.")

    async def _user_is_married(self, user_id: int) -> bool:
        """Ask the social feature whether a user may equip a ring."""

        social = self.bot.get_cog("Fun")
        checker = getattr(social, "is_married", None)
        if not callable(checker):
            raise commands.CommandError(
                "Marriage information is temporarily unavailable. Please try again later."
            )
        check = cast(Callable[[int], Awaitable[bool]], checker)
        return bool(await check(int(user_id)))

    @staticmethod
    def _shop_prefix(ctx: Context) -> str:
        """Return a command prefix suitable for text and slash invocations."""

        prefix = getattr(ctx, "get_prefix", None)
        if isinstance(prefix, str) and prefix:
            return prefix
        return "/" if getattr(ctx, "interaction", None) is not None else "fish "

    @commands.hybrid_command(name="wallet", aliases=("balance", "bal", "coins"))
    @app_commands.describe(
        user="User whose Coins balance to check (defaults to yourself)."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def wallet(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="User whose Coins balance to check (defaults to yourself).",
        ),
    ) -> None:
        """Show a user's Coins balance, creating an empty wallet if needed."""

        wallet = await self.bot.currency.get_wallet(user.id)
        name = discord.utils.escape_markdown(user.name)
        if user.id == ctx.author.id:
            message = f"You have **{wallet.balance:,} Coins** in your wallet."
        else:
            message = f"{name} has **{wallet.balance:,} Coins** in their wallet."
        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        if int(user.id) == int(bot_user_id or FISHIE_USER_ID):
            total_game_losses = await self.bot.currency.get_total_game_losses()
            message += f"\n-# Total game losses: **{total_game_losses:,} Coins**"
        await ctx.send(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="give")
    @app_commands.describe(
        user="The user who should receive the Coins.",
        amount=COIN_AMOUNT_DESCRIPTION,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def give(self, ctx: Context, user: discord.User, *, amount: str) -> None:
        """Give Coins to another account that is at least 31 days old."""

        if user.id == ctx.author.id:
            await ctx.send(
                "You cannot give Coins to yourself.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        sender_remaining = _account_age_remaining(ctx.author)
        recipient_remaining = _account_age_remaining(user)
        if sender_remaining or recipient_remaining:
            blocked: list[str] = []
            if sender_remaining:
                blocked.append("your account")
            if recipient_remaining:
                blocked.append(f"{discord.utils.escape_markdown(user.name)}'s account")
            wait_until = max(sender_remaining, recipient_remaining)
            timestamp = int((discord.utils.utcnow() + wait_until).timestamp())
            subject = " and ".join(blocked)
            await ctx.send(
                f"Currency transfers require {subject} to be at least 31 days old. "
                f"Try again <t:{timestamp}:R>.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            parsed_amount = parse_coin_amount(amount)
        except CoinAmountError as error:
            await ctx.send(
                str(error),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        resolved_amount: int
        if parsed_amount == EVERYTHING_AMOUNT:
            try:
                wallet = await self.bot.currency.get_wallet(ctx.author.id)
                resolved_amount = int(wallet.balance)
            except Exception:
                self.bot.logger.exception("Failed to resolve an everything transfer")
                await ctx.send(
                    "Your wallet is unavailable right now.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if resolved_amount <= 0:
                await ctx.send(
                    "Your wallet is empty, so there are no Coins to give.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if (
                await ctx.prompt(
                    "Are you sure you want to give everything?",
                    confirm_label="Yes",
                    cancel_label="No",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                is None
            ):
                return
        else:
            resolved_amount = int(parsed_amount)
        if resolved_amount <= 0:
            await ctx.send(
                "You must give at least 1 Coin.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            result = await self.bot.currency.transfer(
                ctx.author.id,
                user.id,
                resolved_amount,
                source="give",
                reference_key=(
                    f"give:{ctx.author.id}:{user.id}:"
                    f"{getattr(getattr(ctx, 'message', None), 'id', uuid4().hex)}"
                ),
            )
        except InsufficientFunds as error:
            await ctx.send(
                f"You only have **{error.balance:,} Coins** in your wallet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except (InvalidAmount, BalanceOverflow) as error:
            await ctx.send(str(error), allowed_mentions=discord.AllowedMentions.none())
            return
        except Exception:
            self.bot.logger.exception("Failed to transfer Coins")
            await ctx.send(
                "I couldn't transfer those Coins right now. Please try again later.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        name = discord.utils.escape_markdown(user.name)
        await ctx.send(
            f"You gave **{name}** **{resolved_amount:,} Coins**.\n"
            f"-# Wallet balance: {result.sender.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="lottery")
    async def lottery(self, ctx: Context) -> None:
        """Show the current hourly lottery and your chance of winning."""

        async with ctx.typing():
            status = await self.bot.currency.get_lottery_status(ctx.author.id)
        view = LotteryView(
            self,
            status,
            accent_color=ctx.embedcolor,
            display_user_id=ctx.author.id,
        )
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="earnings")
    async def earnings(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="User whose Coins earnings to check (defaults to yourself).",
        ),
    ) -> None:
        """Show a user's total Coins earnings, losses, and their sources."""

        await self._send_earnings(ctx, user)

    async def _send_earnings(self, ctx: Context, user: discord.User) -> None:
        """Render the earnings view used by the standalone and profile paths."""

        async with ctx.typing():
            summary = await self.bot.currency.get_earnings(user.id)

        view = EarningsView(
            ctx,
            user,
            summary,
            accent_color=ctx.embedcolor,
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def _inventory_entries(self, user_id: int) -> list[tuple[str, str]]:
        """Build display-ready inventory rows for a user."""

        entries: list[tuple[str, str]] = []
        ticket_count = await self.bot.currency.get_lottery_ticket_count(user_id)
        if ticket_count:
            entries.append(
                (
                    "Lottery",
                    f"🎟️ {ticket_count:,} active {plural(ticket_count, False):ticket}",
                )
            )
        service = getattr(self.bot, "badges", None) or getattr(
            self.bot, "badge_service", None
        )
        owned_badges = getattr(service, "owned", None)
        if callable(owned_badges):
            fetch_badges = cast(Callable[[int], Awaitable[Any]], owned_badges)
            badges = await fetch_badges(int(user_id))
            for badge in badges:
                if not bool(self._badge_result_value(badge, "active", True)):
                    continue
                emoji_name = str(self._badge_result_value(badge, "emoji_name", ""))
                emoji_id = self._badge_result_value(badge, "emoji_id")
                if emoji_id:
                    animated = (
                        "a"
                        if bool(self._badge_result_value(badge, "animated", False))
                        else ""
                    )
                    emoji_name = f"<{animated}:{emoji_name}:{int(emoji_id)}>"
                text = str(self._badge_result_value(badge, "text", "")).strip()
                display = " ".join(part for part in (emoji_name, text) if part)
                if display:
                    entries.append(("Badges", display))

        owned_titles = getattr(self.bot.currency, "owned_titles", None)
        if callable(owned_titles):
            fetch_titles = cast(Callable[[int], Awaitable[Any]], owned_titles)
            for title in await fetch_titles(int(user_id)):
                text = str(self._badge_result_value(title, "text", "")).strip()
                if not text:
                    continue
                suffix = (
                    " · equipped"
                    if bool(self._badge_result_value(title, "equipped", False))
                    else ""
                )
                entries.append(
                    ("Titles", f"{discord.utils.escape_markdown(text)}{suffix}")
                )

        owned_colors = getattr(self.bot.currency, "owned_colors", None)
        if callable(owned_colors):
            fetch_colors = cast(Callable[[int], Awaitable[Any]], owned_colors)
            for color in await fetch_colors(int(user_id)):
                key = str(self._badge_result_value(color, "color_key", "")).strip()
                value = str(self._badge_result_value(color, "hex_value", "")).strip()
                if not key:
                    continue
                suffix = (
                    " · equipped"
                    if bool(self._badge_result_value(color, "equipped", False))
                    else ""
                )
                detail = f" ({value})" if value else ""
                entries.append(("Colors", f"**{key}**{detail}{suffix}"))

        owned_racing = getattr(self.bot.currency, "owned_racing_emojis", None)
        if callable(owned_racing):
            fetch_racing = cast(Callable[[int], Awaitable[Any]], owned_racing)
            for racing in await fetch_racing(int(user_id)):
                display = self._racing_emoji_markup(racing)
                suffix = (
                    " · equipped"
                    if bool(self._badge_result_value(racing, "equipped", False))
                    else ""
                )
                entries.append(("Racing Emoji", f"{display}{suffix}"))

        owned_rings = getattr(self.bot.currency, "owned_rings", None)
        if callable(owned_rings):
            fetch_rings = cast(Callable[[int], Awaitable[Any]], owned_rings)
            for ring in await fetch_rings(int(user_id)):
                quantity = int(self._badge_result_value(ring, "quantity", 0) or 0)
                if quantity <= 0:
                    continue
                display = str(self._badge_result_value(ring, "display", "💍"))
                name = discord.utils.escape_markdown(
                    str(self._badge_result_value(ring, "display_name", "Ring"))
                )
                equipped = int(self._badge_result_value(ring, "equipped_count", 0) or 0)
                suffix = " · equipped" if equipped else ""
                entries.append(
                    (
                        "Rings",
                        f"{display} **{name}** · {quantity:,}{suffix}",
                    )
                )
        return entries

    @commands.command(name="inventory", aliases=("inv",))
    async def inventory(
        self,
        ctx: Context,
        user: discord.User = commands.Author,
    ) -> None:
        """Browse a user's profile badges, titles, and colours."""

        await self._send_inventory(ctx, user)

    async def _send_inventory(
        self, ctx: Context, user: discord.abc.User | None = None
    ) -> None:
        """Render the inventory view used by standalone/profile commands."""

        inventory_user = user or ctx.author
        entries = await self._inventory_entries(inventory_user.id)
        view = InventoryView(
            ctx,
            entries,
            display_user=inventory_user,
            accent_color=ctx.embedcolor,
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(name="description")
    async def description(
        self, ctx: Context, *, description: str | None = None
    ) -> None:
        """Set or clear the description shown on your profile card."""

        await self._set_profile_description(ctx, description)

    async def _set_profile_description(
        self, ctx: Context, description: str | None
    ) -> None:
        """Persist a profile description and explain the empty-value behavior."""

        try:
            saved = await self.bot.currency.set_profile_description(
                ctx.author.id, description
            )
        except ValueError as error:
            await ctx.send(str(error), allowed_mentions=discord.AllowedMentions.none())
            return
        if saved is None:
            message = "Your profile description was removed."
        else:
            message = "Your profile description was updated."
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    async def _send_profile_card(
        self,
        ctx: Context,
        profile_user: discord.User | discord.Member | None = None,
    ) -> None:
        """Render and send a profile card for the requested user."""

        user = profile_user or ctx.author
        async with ctx.typing():
            try:
                avatar = await user.display_avatar.read()
            except (discord.HTTPException, OSError):
                avatar = b""

            try:
                description = await self.bot.currency.get_profile_description(user.id)
            except Exception:
                # The card should remain usable while an older deployment is
                # applying the profile-description migration.
                self.bot.logger.debug(
                    "Could not load profile description for %s",
                    user.id,
                    exc_info=True,
                )
                description = None

            try:
                xp_value = await self.bot.pool.fetchval(
                    "SELECT COALESCE(xp, 0) FROM message_xp WHERE user_id = $1",
                    user.id,
                )
                xp = max(0, int(xp_value or 0))
                rank_value = await self.bot.pool.fetchval(
                    "SELECT COUNT(*) + 1 FROM message_xp WHERE xp > $1",
                    xp,
                )
                rank = max(1, int(rank_value or 1))
            except Exception:
                self.bot.logger.debug("Could not load profile XP", exc_info=True)
                xp, rank = 0, 1

            try:
                reputation_value = await self.bot.pool.fetchval(
                    "SELECT COALESCE(MAX(count), 0) FROM user_rep WHERE user_id = $1",
                    user.id,
                )
                reputation = max(0, int(reputation_value or 0))
            except Exception:
                self.bot.logger.debug(
                    "Could not load profile reputation", exc_info=True
                )
                reputation = 0

            title: str | None = None
            try:
                for row in await self.bot.currency.owned_titles(user.id):
                    if bool(row["equipped"]):
                        title = str(row["text"] or "").strip() or None
                        break
            except Exception:
                self.bot.logger.debug("Could not load profile title", exc_info=True)

            logger = self.bot.logger
            badge_entries: list[dict[str, Any]] = []
            info = self.bot.get_cog("Info")
            get_entries = getattr(info, "get_badge_entries", None)
            logger.info(
                "Profile badge collection started user_id=%s info_cog=%s callable=%s",
                user.id,
                type(info).__name__ if info is not None else None,
                callable(get_entries),
            )
            if callable(get_entries):
                try:
                    fetched = (
                        user
                        if isinstance(user, discord.User)
                        else await self.bot.fetch_user(user.id)
                    )
                    badge_entries = list(
                        await cast(Callable[..., Awaitable[Any]], get_entries)(
                            user, ctx, fetched
                        )
                    )
                    logger.info(
                        "Profile badge collector returned user_id=%s count=%d keys=%s",
                        user.id,
                        len(badge_entries),
                        [
                            str(entry.get("_badge_key") or entry.get("badge_key") or "")
                            for entry in badge_entries
                            if isinstance(entry, dict)
                        ],
                    )
                except Exception:
                    # Keep the traceback at warning level: this path used to
                    # log only at debug level, which made a missing profile
                    # badge completely invisible in production logs.
                    logger.exception(
                        "Profile badge collector failed user_id=%s",
                        user.id,
                    )

            # ``Info.get_badge_entries`` also adds Discord flags and performs
            # ordering/metadata work. If that cog is unavailable (or an older
            # deployment throws while loading it), use the authoritative
            # active badge rows directly so a profile never silently loses
            # owner, purchased, or stat badges.
            if not badge_entries:
                fallback_rows: list[Any] = []
                badge_service = getattr(self.bot, "badges", None)
                if badge_service is not None:
                    try:
                        fallback_rows = list(await badge_service.owned(user.id))
                    except Exception:
                        logger.exception(
                            "Profile badge database fallback failed user_id=%s",
                            user.id,
                        )
                if not fallback_rows:
                    cached = getattr(
                        getattr(self.bot, "db_cache", None), "user_badges", {}
                    )
                    fallback_rows = (
                        list(cached.get(user.id, ()))
                        if isinstance(cached, dict)
                        else []
                    )
                for index, raw_badge in enumerate(fallback_rows):
                    if isinstance(raw_badge, dict):
                        entry = dict(raw_badge)
                    else:
                        try:
                            entry = dict(cast(Any, raw_badge))
                        except (TypeError, ValueError):
                            continue
                    if entry.get("active") is False:
                        continue
                    entry.setdefault(
                        "_badge_key", entry.get("badge_key") or f"db:{user.id}:{index}"
                    )
                    badge_entries.append(entry)
                logger.warning(
                    "Profile badge collector fallback used user_id=%s count=%d keys=%s",
                    user.id,
                    len(badge_entries),
                    [
                        str(entry.get("_badge_key") or entry.get("badge_key") or "")
                        for entry in badge_entries
                    ],
                )

            badge_markup = inline_badge_markup(badge_entries)
            badge_tokens = badge_markup.split()
            logger.info(
                "Profile badge markup prepared user_id=%s entries=%d tokens=%s markup=%r",
                user.id,
                len(badge_entries),
                badge_tokens,
                badge_markup,
            )
            badge_images = await resolve_badge_images(
                ctx.session,
                badge_entries,
                bot=self.bot,
                user_id=user.id,
            )
            logger.info(
                "Profile badge images prepared user_id=%s image_count=%d",
                user.id,
                len(badge_images),
            )

            card = await render_profile_card(
                ctx.session,
                ProfileCardData(
                    name=str(getattr(user, "display_name", None) or user.name),
                    title=title,
                    description=description,
                    badges=badge_markup,
                    rank=rank,
                    xp=xp,
                    reputation=reputation,
                    avatar=avatar,
                    accent=ctx.embed_color.value,
                    badge_images=badge_images,
                ),
            )
            logger.info(
                "Profile card rendered user_id=%s badge_images=%d bytes=%d",
                user.id,
                len(badge_images),
                card.getbuffer().nbytes,
            )

        filename = "profile.png"
        file = discord.File(card, filename=filename)
        await ctx.send(
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        logger.info("Profile card sent user_id=%s", user.id)

    async def _send_xp(self, ctx: Context, user: discord.User) -> None:
        """Render the XP summary used by the profile command."""

        xp: int | None = await self.bot.pool.fetchval(
            "SELECT xp FROM message_xp WHERE user_id = $1", user.id
        )
        if not bool(xp):
            raise commands.BadArgument("This user has no recorded XP")
        bonus = self.bot.db_cache.reputation_bonus_count(user.id) * 5
        name = discord.utils.escape_markdown(user.name)
        bonus_text = f"*+{bonus:,} bonus.*" if bonus else "*No XP bonuses active*"
        await ctx.send(
            f"{name} has {int(xp):,} XP\n-# {bonus_text}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_limits(self, ctx: Context) -> None:
        """Render today's capped minigame rewards as a Components V2 view."""

        user_id = int(ctx.author.id)
        period = claim_period_start("daily")
        # Tic-Tac-Toe and Connect Four have independent reward sources, so each
        # game and difficulty receives its own daily allowance.
        limits: tuple[tuple[str, str, int], ...] = (
            ("Tic-Tac-Toe · Easy Mode", "game_tictactoe_easy", 10_000),
            ("Tic-Tac-Toe · Normal Mode", "game_tictactoe_normal", 10_000),
            ("Tic-Tac-Toe · Hard Mode", "game_tictactoe_hard", 10_000),
            ("Connect Four · Easy Mode", "game_connectfour_easy", 10_000),
            ("Connect Four · Normal Mode", "game_connectfour_normal", 10_000),
            ("Connect Four · Hard Mode", "game_connectfour_hard", 10_000),
            ("Lights Out", "lightsout", 5_000),
            ("Word Bomb", "wordbomb", 5_000),
        )
        usage: dict[str, int] = {}
        try:
            rows = await self.bot.pool.fetch(
                """
                SELECT source, amount
                FROM currency_daily_rewards
                WHERE user_id = $1 AND period_start = $2
                """,
                user_id,
                period,
            )
            usage = {
                str(row["source"]): max(0, int(row["amount"] or 0)) for row in rows
            }
        except Exception:
            # Older deployments may not have the optional reward table yet;
            # keep the command useful and show fresh limits instead of failing.
            self.bot.logger.debug("Could not load daily game limits", exc_info=True)

        lines = [
            f"**{label}:** {min(usage.get(source, 0), cap):,}/{cap:,} Coins"
            for label, source, cap in limits
        ]
        view = discord.ui.LayoutView(timeout=120)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Daily game limits"),
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=ctx.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="daily")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def daily(self, ctx: Context) -> None:
        """Claim 100 Coins once per UTC day, plus your daily streak bonus."""

        await self._claim(ctx, "daily", DAILY_COINS)

    @commands.hybrid_command(name="weekly")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def weekly(self, ctx: Context) -> None:
        """Claim 1,000 Coins once per UTC week, plus your weekly streak bonus."""

        await self._claim(ctx, "weekly", WEEKLY_COINS)

    @staticmethod
    def _work_easy_words() -> tuple[str, ...]:
        """Load a small, approachable word pool for the scramble task."""

        path = FILES_ROOT / "data" / "word-games.txt"
        try:
            words = tuple(
                dict.fromkeys(
                    word.strip().casefold()
                    for word in path.read_text(encoding="utf-8").splitlines()
                    if 5 <= len(word.strip()) <= 6 and word.strip().isalpha()
                )
            )
        except OSError:
            words = ()
        return words or (
            "apple",
            "beach",
            "bread",
            "chair",
            "cloud",
            "grape",
            "house",
            "lemon",
            "mouse",
            "plant",
            "river",
            "stone",
        )

    @staticmethod
    def _work_scramble(word: str) -> str:
        letters = list(word)
        for _ in range(16):
            random.shuffle(letters)
            scrambled = "".join(letters)
            if scrambled != word:
                return scrambled
        return word[1:] + word[:1]

    async def _finish_work_task(
        self,
        ctx: Context,
        token: str,
        task: str,
        success: bool,
        *,
        minimum: int,
        maximum: int,
        pay_on_failure: bool = True,
        success_bonus: int = 0,
        announce_failure: bool = True,
        separate_response: bool = False,
    ) -> None:
        """Settle one work task exactly once and report its reward."""

        if self._work_sessions.get(ctx.author.id) != token:
            return
        self._work_sessions.pop(ctx.author.id, None)
        for key in tuple(getattr(self, "_work_wordle_games", ())):
            if key[0] == ctx.author.id:
                self._work_wordle_games.pop(key, None)
        for key in tuple(getattr(self, "_work_math_tasks", ())):
            if key[0] == ctx.author.id:
                self._work_math_tasks.pop(key, None)

        async def send_result(content: str) -> None:
            sender: Callable[..., Awaitable[Any]] = ctx.send
            if separate_response:
                candidate = getattr(ctx, "send_new", None)
                if callable(candidate):
                    sender = cast(Callable[..., Awaitable[Any]], candidate)
            await sender(content, allowed_mentions=discord.AllowedMentions.none())

        if task == "click" and not success:
            await send_result(
                "You ran out of time before clicking 10 times, so you earned no Coins.",
            )
            return
        if not success and not pay_on_failure:
            if announce_failure:
                await send_result(
                    f"You did not complete the **{task}** task, so you earned no Coins.",
                )
            return
        amount = random.randint(minimum, maximum) if success else minimum
        if success:
            amount += max(0, success_bonus)
        try:
            wallet = await self.bot.currency.credit(
                ctx.author.id,
                amount,
                f"work_{task}",
                reference_key=f"work:{ctx.author.id}:{token}",
            )
        except Exception:
            self.bot.logger.exception("Failed to award work task Coins")
            await send_result(
                "The task was completed, but I couldn't add the Coins. Please try again later.",
            )
            return
        result = "completed" if success else "unsuccessfully finished"
        await send_result(
            f"You {result} the **{task}** task and earned **{amount:,} Coins**.\n"
            f"-# Wallet balance: {wallet.balance:,} Coins",
        )

    @commands.hybrid_command(name="work")
    @commands.cooldown(1, 5 * 60, commands.BucketType.user)
    @app_commands.checks.cooldown(1, 5 * 60)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def work(self, ctx: Context) -> None:
        """Complete a random task to earn Coins.

        Earnings:
        ```yaml
        Math: 100 - 150 Coins
        Quick job: 10 - 50 Coins
        Unscramble: 25 - 60 Coins
        Wordle: 50 - 100 Coins (+50 in hard mode)
        Click: 50 - 100 Coins
        ```
        """

        if ctx.author.id in self._work_sessions:
            await ctx.send(
                "You already have an active work task. Finish it before starting another.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        task = random.choice(("math", "coins", "unscramble", "wordle", "click"))
        token = uuid4().hex
        self._work_sessions[ctx.author.id] = token

        if task == "coins":
            amount = random.randint(10, 50)
            try:
                wallet = await self.bot.currency.credit(
                    ctx.author.id,
                    amount,
                    "work_coins",
                    reference_key=f"work:{ctx.author.id}:{token}",
                )
            finally:
                self._work_sessions.pop(ctx.author.id, None)
            await ctx.send(
                f"You did a quick job and earned **{amount:,} Coins**.\n"
                f"-# Wallet balance: {wallet.balance:,} Coins",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if task == "math":
            operation = random.choice(("+", "-", "×", "÷"))
            if operation == "+":
                left, right = random.randint(10, 75), random.randint(10, 75)
                answer = left + right
            elif operation in {"-", "−"}:
                left = random.randint(20, 75)
                right = random.randint(1, left)
                answer = left - right
            elif operation == "×":
                left, right = random.randint(2, 12), random.randint(2, 12)
                answer = left * right
            else:
                right, answer = random.randint(2, 12), random.randint(2, 20)
                left = right * answer
            question = f"Solve **{left} {operation} {right}** within 60 seconds."

            async def complete(success: bool) -> None:
                await self._finish_work_task(
                    ctx,
                    token,
                    task,
                    success,
                    minimum=100,
                    maximum=150,
                    pay_on_failure=False,
                    announce_failure=False,
                    separate_response=True,
                )

            math_view = WorkMathView(
                ctx.author.id,
                question,
                answer,
                complete,
                source_message_id=getattr(getattr(ctx, "message", None), "id", None),
            )
            self._work_math_tasks[(ctx.author.id, ctx.channel.id)] = math_view
            try:
                math_view.message = await ctx.send(
                    question,
                    view=math_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                self._work_math_tasks.pop((ctx.author.id, ctx.channel.id), None)
                math_view.stop()
                raise
            return

        if task == "unscramble":
            word = random.choice(self._work_easy_words())
            scrambled = self._work_scramble(word)
            await ctx.send(
                f"Unscramble **`{scrambled}`** within 60 seconds.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Keep the deadline fixed across attempts.  Waiting for another
            # message with a fresh 60-second timeout after every wrong guess
            # would let a user extend the task indefinitely.
            deadline = asyncio.get_running_loop().time() + 60

            def check(message: discord.Message) -> bool:
                return (
                    message.author.id == ctx.author.id
                    and message.channel.id == ctx.channel.id
                )

            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._finish_work_task(
                        ctx, token, task, False, minimum=25, maximum=60
                    )
                    break
                try:
                    response = await self.bot.wait_for(
                        "message", timeout=remaining, check=check
                    )
                except asyncio.TimeoutError:
                    await self._finish_work_task(
                        ctx, token, task, False, minimum=25, maximum=60
                    )
                    break
                if response.content.strip().casefold() == word:
                    await self._finish_work_task(
                        ctx, token, task, True, minimum=25, maximum=60
                    )
                    break
            return

        if task == "click":
            fun = self.bot.get_cog("Fun")
            click_total = 0
            click_total_getter = getattr(fun, "_click_total", None)
            if callable(click_total_getter):
                try:
                    get_total = cast(Callable[[], Awaitable[Any]], click_total_getter)
                    click_total = int(await get_total())
                except Exception:
                    self.bot.logger.debug(
                        "Could not read the click total for a work task",
                        exc_info=True,
                    )

            async def complete(success: bool) -> None:
                await self._finish_work_task(
                    ctx, token, task, success, minimum=50, maximum=100
                )

            async def record_work_click() -> int | None:
                recorder = getattr(fun, "_record_click", None)
                if callable(recorder):
                    try:
                        record = cast(
                            Callable[[int, int | None], Awaitable[Any]], recorder
                        )
                        return int(
                            await record(
                                ctx.author.id,
                                ctx.guild.id if ctx.guild is not None else None,
                            )
                        )
                    except Exception:
                        self.bot.logger.debug(
                            "Could not record the click made by a work task",
                            exc_info=True,
                        )
                return None

            click_view = WorkClickView(
                ctx.author.id,
                complete,
                on_click=record_work_click,
                total=click_total,
            )
            click_view.message = await ctx.send(
                view=click_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Use the same Wordle game/view as the normal command. This preserves
        # hard-mode validation, colourblind rendering, the board attachment,
        # and the standard modal interaction while the completion callback
        # settles the work reward.
        hard_mode, colourblind_mode = await get_wordle_settings(
            self.bot.pool, ctx.author.id
        )
        wordle_game = new_wordle_game(
            user_id=ctx.author.id,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            hard_mode=hard_mode,
            colourblind_mode=colourblind_mode,
        )

        async def complete(success: bool) -> None:
            await self._finish_work_task(
                ctx,
                token,
                task,
                success,
                minimum=50,
                maximum=100,
                success_bonus=50 if hard_mode else 0,
            )

        wordle_view = WorkWordleView(wordle_game, complete)
        wordle_game.view = wordle_view
        work_wordle_key = (ctx.author.id, ctx.channel.id)
        self._work_wordle_games[work_wordle_key] = wordle_game
        try:
            wordle_game.message = await ctx.send(
                view=wordle_view,
                file=wordle_view.board_file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._work_wordle_games.pop(work_wordle_key, None)
            wordle_view.stop()
            raise

    @commands.Cog.listener("on_message")
    async def work_wordle_listener(self, message: discord.Message) -> None:
        """Accept normal five-letter messages as guesses for work Wordle."""

        if is_legacy_instance(self.bot):
            return
        if message.author.bot:
            return
        games = getattr(self, "_work_wordle_games", {})
        game = games.get((message.author.id, message.channel.id))
        if game is None:
            return
        view = getattr(game, "view", None)
        if isinstance(view, WorkWordleView):
            await view.submit_message(message)

    @commands.Cog.listener("on_message")
    async def work_math_listener(self, message: discord.Message) -> None:
        """Accept typed answers for an active work math task."""

        if is_legacy_instance(self.bot):
            return
        if message.author.bot:
            return
        tasks = getattr(self, "_work_math_tasks", {})
        view = tasks.get((message.author.id, message.channel.id))
        if isinstance(view, WorkMathView):
            await view.submit_message(message)

    @commands.hybrid_group(name="shop", fallback="help")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def shop(self, ctx: Context) -> None:
        """Browse the Coins shop."""

        owned_badges, owned_titles, owned_colors = await self._shop_owned(ctx.author.id)
        owned_racing = await self._shop_racing_owned(ctx.author.id)
        await ctx.send(
            view=ShopLandingView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
                owned_badges=owned_badges,
                owned_titles=owned_titles,
                owned_colors=owned_colors,
                owned_racing=owned_racing,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shop.command(name="badges", aliases=("badge",))
    async def shop_badges(self, ctx: Context) -> None:
        """Show badges that can be purchased with Coins."""
        owned_badges, owned_titles, owned_colors = await self._shop_owned(ctx.author.id)
        owned_racing = await self._shop_racing_owned(ctx.author.id)
        await ctx.send(
            view=BadgeShopView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
                owned_badges=owned_badges,
                owned_titles=owned_titles,
                owned_colors=owned_colors,
                owned_racing=owned_racing,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shop.command(name="titles", aliases=("title",))
    async def shop_titles(self, ctx: Context) -> None:
        """Show titles that can be purchased with Coins."""
        owned_badges, owned_titles, owned_colors = await self._shop_owned(ctx.author.id)
        owned_racing = await self._shop_racing_owned(ctx.author.id)
        await ctx.send(
            view=TitleShopView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
                owned_badges=owned_badges,
                owned_titles=owned_titles,
                owned_colors=owned_colors,
                owned_racing=owned_racing,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shop.command(name="colors", aliases=("color", "colours", "colour"))
    async def shop_colors(self, ctx: Context) -> None:
        """Show colours that can be purchased with Coins."""
        owned_badges, owned_titles, owned_colors = await self._shop_owned(ctx.author.id)
        owned_racing = await self._shop_racing_owned(ctx.author.id)
        await ctx.send(
            view=ColorShopView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
                owned_badges=owned_badges,
                owned_titles=owned_titles,
                owned_colors=owned_colors,
                owned_racing=owned_racing,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shop.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    async def shop_racing_emoji(self, ctx: Context) -> None:
        """Show the Racing Emoji price categories."""

        owned_badges, owned_titles, owned_colors = await self._shop_owned(ctx.author.id)
        owned_racing = await self._shop_racing_owned(ctx.author.id)
        await ctx.send(
            view=RacingEmojiShopView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
                owned_racing=owned_racing,
                owned_badges=owned_badges,
                owned_titles=owned_titles,
                owned_colors=owned_colors,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shop.command(name="lottery", aliases=("ticket", "tickets"))
    async def shop_lottery(self, ctx: Context) -> None:
        """Explain lottery tickets and the hourly draw."""

        owned_badges, owned_titles, owned_colors = await self._shop_owned(ctx.author.id)
        owned_racing = await self._shop_racing_owned(ctx.author.id)
        await ctx.send(
            view=LotteryShopView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
                owned_badges=owned_badges,
                owned_titles=owned_titles,
                owned_colors=owned_colors,
                owned_racing=owned_racing,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shop.command(name="rings", aliases=("ring",))
    async def shop_rings(self, ctx: Context) -> None:
        """Show rings that can be purchased with Coins."""

        await ctx.send(
            view=RingShopView(
                accent_color=ctx.embedcolor,
                command_prefix=self._shop_prefix(ctx),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(name="purchase", aliases=("buy",), fallback="help")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def purchase(self, ctx: Context) -> None:
        """Purchase an item from the Coins shop."""

        await ctx.send(
            "Use `fish purchase badge <badge>`, `fish purchase title <title>`, "
            "`fish purchase color <color>`, or "
            "`fish purchase racing-emoji <emoji>`, or "
            "`fish purchase ring <ring>`, or "
            "`fish purchase ticket <amount>`. ",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @purchase.command(name="ticket", aliases=("tickets",))
    @app_commands.describe(amount=COIN_AMOUNT_DESCRIPTION)
    async def purchase_ticket(self, ctx: Context, *, amount: str = "1") -> None:
        """Buy one or more tickets for the current hourly lottery draw."""

        async with ctx.typing():
            try:
                parsed_amount = parse_coin_amount(amount)
                if parsed_amount == EVERYTHING_AMOUNT:
                    wallet = await self.bot.currency.get_wallet(ctx.author.id)
                    parsed_amount = int(wallet.balance) // LOTTERY_TICKET_PRICE
                    if parsed_amount <= 0:
                        raise InvalidAmount(
                            "Your wallet does not have enough Coins for a ticket."
                        )
                    confirmed = await ctx.prompt(
                        "Are you sure you want to buy as many tickets as possible?",
                        confirm_label="Yes",
                        cancel_label="No",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    if confirmed is None:
                        return
                result = await self.bot.currency.purchase_lottery_tickets(
                    ctx.author.id, int(parsed_amount)
                )
            except (CoinAmountError, InvalidAmount, InsufficientFunds) as error:
                raise commands.BadArgument(str(error)) from error
        tickets = list(result.tickets)
        await ctx.send(
            f"Bought **{len(tickets):,} {plural(len(tickets), False):lottery ticket}** for "
            f"**{len(tickets) * LOTTERY_TICKET_PRICE:,} Coins**.\n"
            f"-# *Prize pool: {result.prize_pool:,} Coins · "
            f"Wallet balance: {result.wallet_balance:,} Coins*",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @purchase.command(name="ring", aliases=("rings",))
    @app_commands.describe(ring="Ring ID, name, or emoji to purchase.")
    async def purchase_ring(self, ctx: Context, *, ring: str) -> None:
        """Purchase one ring; duplicate purchases increase its quantity."""

        offer = self._ring_offer(ring)
        if offer is None:
            raise commands.BadArgument(
                "That ring is not in the shop. Use `fish shop rings` to see "
                "the available rings."
            )
        try:
            wallet = await self.bot.currency.purchase_ring(
                ctx.author.id,
                offer.key,
                offer.price,
            )
        except InsufficientFunds as error:
            raise commands.BadArgument(
                f"You need **{error.required:,} Coins**, but only have "
                f"**{error.balance:,} Coins**."
            ) from error
        except RingNotOwned as error:
            raise commands.BadArgument(
                "That ring is not currently available."
            ) from error
        await ctx.send(
            f"Purchased {offer.emoji} **{offer.name}** for "
            f"**{offer.price:,} Coins**.\n"
            f"-# Wallet balance: {wallet.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _badge_offer(selector: str) -> BadgeOffer | None:
        normalized = selector.strip()
        normalized_key = re.sub(r"[\s_-]+", "-", normalized.casefold()).strip(":-")
        key = (
            "flag"
            if _unicode_flag(normalized)
            else _BADGE_OFFER_ALIASES.get(normalized_key)
        )
        offers = _badge_catalog_offers()
        if key is not None:
            return next((offer for offer in offers if offer.key == key), None)

        compact = re.sub(r"[^a-z0-9]+", "", normalized.casefold())
        for offer in offers:
            # Badge descriptions are display text (for example ``Fishie`` for
            # the ``fish`` selector), not aliases.  Keep that distinction so a
            # title/display name cannot accidentally purchase a different
            # badge; owner-created rows can still be addressed by key or
            # selector.
            candidates = (offer.key, offer.command_selector, offer.emoji)
            for candidate in candidates:
                candidate_key = re.sub(r"[\s_-]+", "-", candidate.casefold()).strip(
                    ":-"
                )
                candidate_compact = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
                if normalized_key == candidate_key or compact == candidate_compact:
                    return offer
        return None

    @staticmethod
    def _title_offer(selector: str) -> TitleOffer | None:
        normalized = selector.strip()
        normalized_key = re.sub(r"[\s_-]+", "-", normalized.casefold()).strip(":-")
        key = _TITLE_OFFER_ALIASES.get(normalized_key)
        offers = _title_catalog_offers()
        if key is not None:
            return next((offer for offer in offers if offer.key == key), None)

        # Titles added to the shop can be selected by their displayed name or
        # stable key without requiring another alias-map edit.  Compare both a
        # punctuation-preserving slug and a compact alphanumeric form so names
        # such as ``Magician's Red`` and ``magicians-red`` resolve alike.
        compact = re.sub(r"[^a-z0-9]+", "", normalized.casefold())

        # Prefer an exact selector/name match before falling back to compact
        # matching.  Several emoticon titles intentionally contain only
        # punctuation (``>_<``, ``@_@``, ``<3``, ``:3``), so their compact
        # forms are empty or collide with one another.
        for offer in offers:
            candidates = (offer.key, offer.command_selector, offer.description)
            for candidate in candidates:
                candidate_key = re.sub(r"[\s_-]+", "-", candidate.casefold()).strip(
                    ":-"
                )
                if normalized_key == candidate_key:
                    return offer

        if compact:
            for offer in offers:
                candidates = (offer.key, offer.command_selector, offer.description)
                for candidate in candidates:
                    candidate_compact = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
                    if candidate_compact and compact == candidate_compact:
                        return offer
        return None

    @staticmethod
    def _color_offer(selector: str) -> ColorOffer | None:
        normalized = selector.strip()
        normalized_key = re.sub(r"[\s_-]+", "-", normalized.casefold()).strip(":-")
        key = _COLOR_OFFER_ALIASES.get(normalized_key, normalized_key)
        return next(
            (
                offer
                for offer in _color_catalog_offers()
                if key
                in (offer.key, offer.command_selector, offer.description.casefold())
            ),
            None,
        )

    @staticmethod
    def _racing_emoji_info(
        selector: str, guild: discord.Guild | None
    ) -> tuple[str, str, int | None, bool, bool, str, int]:
        """Resolve and price a Unicode or current-server custom emoji.

        The returned tuple is ``(key, name, id, unicode, animated, display,
        price)``.  The category is resolved separately by the caller; keeping
        the parser synchronous makes it safe for slash and text command paths
        alike.
        """

        raw = str(selector).strip()
        if not raw:
            raise commands.BadArgument("Provide a Unicode or custom emoji.")
        partial = discord.PartialEmoji.from_str(raw)
        if partial.id is not None:
            if guild is None:
                raise commands.BadArgument(
                    "Custom racing emojis must be purchased in a server where Fishie is present."
                )
            source = guild.get_emoji(int(partial.id))
            if source is None:
                raise commands.BadArgument(
                    "That custom emoji must come from this server."
                )
            return (
                f"custom:{int(partial.id)}",
                str(partial.name or "emoji"),
                int(partial.id),
                False,
                bool(partial.animated),
                str(partial),
                RACING_EMOJI_CUSTOM_PRICE,
            )

        # Support Discord's familiar :emoji_name: input as a convenience.
        candidate = emoji_lib.emojize(raw, language="alias")
        tokens = emoji_lib.emoji_list(candidate)
        if len(tokens) != 1 or tokens[0].get("emoji") != candidate:
            raise commands.BadArgument("That is not a valid single emoji.")
        category = _racing_emoji_category(candidate)
        return (
            candidate,
            candidate,
            None,
            True,
            False,
            candidate,
            category.price,
        )

    @staticmethod
    def _racing_emoji_category_for_value(value: str) -> RacingEmojiCategory:
        return _racing_emoji_category(value)

    @staticmethod
    def _racing_emoji_markup(row: Any) -> str:
        """Render a persisted racing emoji row for a race or inventory."""

        emoji_id = Currency._badge_result_value(row, "emoji_id")
        if emoji_id:
            name = str(Currency._badge_result_value(row, "emoji_name", "emoji"))
            animated = (
                "a" if bool(Currency._badge_result_value(row, "animated")) else ""
            )
            return f"<{animated}:{name}:{int(emoji_id)}>"
        return str(
            Currency._badge_result_value(
                row, "display", Currency._badge_result_value(row, "emoji_name", "")
            )
        )

    async def _owned_racing_emoji(self, user_id: int, selector: str) -> Any:
        owned = getattr(self.bot.currency, "owned_racing_emojis", None)
        if not callable(owned):
            raise commands.BadArgument(
                "Racing emoji management is temporarily unavailable. Please try again later."
            )
        fetch = cast(Callable[[int], Awaitable[Any]], owned)
        rows = list(await fetch(int(user_id)))
        normalized = str(selector).strip()
        folded = normalized.casefold()
        matches: list[Any] = []
        for row in rows:
            key = str(self._badge_result_value(row, "emoji_key", ""))
            display = str(self._badge_result_value(row, "display", ""))
            name = str(self._badge_result_value(row, "emoji_name", ""))
            emoji_id = self._badge_result_value(row, "emoji_id")
            aliases = {key.casefold(), display.casefold(), name.casefold()}
            if emoji_id:
                aliases.update(
                    {
                        str(emoji_id),
                        self._racing_emoji_markup(row).casefold(),
                    }
                )
            if folded in aliases:
                matches.append(row)
        if len(matches) > 1:
            raise commands.BadArgument(
                "That selector matches multiple racing emojis; use the emoji itself."
            )
        if not matches:
            raise commands.BadArgument("You do not own that racing emoji.")
        return matches[0]

    @staticmethod
    def _parse_color_hex(value: str) -> str:
        value = value.strip()
        match = _HEX_COLOR_RE.fullmatch(value)
        if match is not None:
            normalized = match.group("hex").upper()
            if len(normalized) == 3:
                normalized = "".join(character * 2 for character in normalized)
            return f"#{normalized}"

        rgb_match = _RGB_COLOR_RE.fullmatch(value)
        if rgb_match is not None:
            channels = tuple(
                int(rgb_match.group(name)) for name in ("red", "green", "blue")
            )
            if all(0 <= channel <= 255 for channel in channels):
                return "#%02X%02X%02X" % channels
        raise commands.BadArgument(
            "Provide a hex or RGB colour such as `#5865F2` or `rgb(88, 101, 242)`."
        )

    async def _owned_color(self, user_id: int, selector: str) -> Any:
        """Resolve a colour selector against the user's active colours."""

        owned_colors = getattr(self.bot.currency, "owned_colors", None)
        if not callable(owned_colors):
            raise commands.BadArgument(
                "Colour management is temporarily unavailable. Please try again later."
            )
        fetch_colors = cast(Callable[[int], Awaitable[Any]], owned_colors)
        rows = list(await fetch_colors(int(user_id)))
        normalized = str(selector).strip()
        folded = normalized.casefold()
        offer = self._color_offer(normalized)
        offer_key = offer.key.casefold() if offer is not None else None
        parsed_hex: str | None = None
        try:
            parsed_hex = self._parse_color_hex(normalized).casefold()
        except commands.BadArgument:
            pass
        matches: list[Any] = []
        for row in rows:
            key = str(self._badge_result_value(row, "color_key", ""))
            hex_value = str(self._badge_result_value(row, "hex_value", ""))
            key_folded = key.casefold()
            hex_folded = hex_value.casefold()
            # Preset aliases only belong to the matching catalog key.  Adding
            # them to every row made a selector such as ``custom`` match all
            # of a user's colours and incorrectly report an ambiguity.
            aliases = {key_folded, hex_folded}
            if offer is not None and key_folded == offer.key.casefold():
                aliases.update(
                    {
                        offer.command_selector.casefold(),
                        offer.description.casefold(),
                    }
                )
            if folded in aliases or (
                parsed_hex is not None and parsed_hex == hex_folded
            ):
                matches.append(row)
            elif offer_key is not None and key_folded == offer_key:
                matches.append(row)
        if len(matches) > 1:
            raise commands.BadArgument(
                "That selector matches multiple colours; use the hex value."
            )
        if not matches:
            raise commands.BadArgument("You do not own that colour.")
        return matches[0]

    async def _refresh_color_cache(self, user_id: int) -> None:
        """Refresh the optional runtime colour cache after a mutation."""

        cache = getattr(self.bot, "db_cache", None)
        if cache is None:
            return
        owned_colors = getattr(self.bot.currency, "owned_colors", None)
        if not callable(owned_colors):
            return
        fetch_colors = cast(Callable[[int], Awaitable[Any]], owned_colors)
        rows = await fetch_colors(int(user_id))
        equipped = next(
            (
                row
                for row in rows
                if bool(self._badge_result_value(row, "active", True))
                and bool(self._badge_result_value(row, "equipped", False))
            ),
            None,
        )
        if equipped is None:
            refresh = getattr(cache, "refresh_user_color", None)
            if callable(refresh):
                refresh(int(user_id), None)
                return
            remove = getattr(cache, "remove_user_color", None)
            if callable(remove):
                remove(int(user_id))
            return
        refresh = getattr(cache, "refresh_user_color", None)
        if callable(refresh):
            refresh(
                int(user_id),
                str(self._badge_result_value(equipped, "color_key", "")),
                str(self._badge_result_value(equipped, "hex_value", "")),
            )
            return
        set_color = getattr(cache, "set_user_color", None)
        if callable(set_color):
            set_color(
                int(user_id),
                str(self._badge_result_value(equipped, "color_key", "")),
                str(self._badge_result_value(equipped, "hex_value", "")),
            )

    async def _owned_title(self, user_id: int, selector: str) -> Any:
        """Resolve a title selector against the user's active titles."""

        owned_titles = getattr(self.bot.currency, "owned_titles", None)
        if not callable(owned_titles):
            raise commands.BadArgument(
                "Title management is temporarily unavailable. Please try again later."
            )
        fetch_titles = cast(Callable[[int], Awaitable[Any]], owned_titles)
        rows = list(await fetch_titles(int(user_id)))
        normalized = str(selector).strip()
        folded = normalized.casefold()
        offer = self._title_offer(normalized)
        offer_key = offer.key.casefold() if offer is not None else None
        matches: list[Any] = []
        for row in rows:
            key = str(self._badge_result_value(row, "title_key", ""))
            text = str(self._badge_result_value(row, "text", ""))
            aliases = {key.casefold(), text.casefold()}
            # A catalog offer's aliases belong only to its matching ownership
            # row.  Applying them to every owned title made selectors such as
            # ``custom-title`` ambiguous when a user owned multiple titles.
            if offer is not None and key.casefold() == offer.key.casefold():
                aliases.update(
                    {
                        offer.command_selector.casefold(),
                        offer.description.casefold(),
                    }
                )
            if folded in aliases or (
                offer_key is not None and key.casefold() == offer_key
            ):
                matches.append(row)
        if len(matches) > 1:
            raise commands.BadArgument(
                "That selector matches multiple titles; use the title name."
            )
        if not matches:
            raise commands.BadArgument("You do not own that title.")
        return matches[0]

    @staticmethod
    def _title_result_value(result: Any, key: str, default: Any = None) -> Any:
        """Read a title row/result from either a mapping or dataclass."""

        if isinstance(result, Mapping):
            return result.get(key, default)
        try:
            return result[key]
        except (KeyError, IndexError, TypeError):
            return getattr(result, key, default)

    @staticmethod
    def _badge_result_value(result: Any, key: str, default: Any = None) -> Any:
        if isinstance(result, Mapping):
            return result.get(key, default)
        # asyncpg.Record supports item access but is not a Mapping and does
        # not expose columns as normal attributes.
        try:
            return result[key]
        except (KeyError, IndexError, TypeError):
            pass
        return getattr(result, key, default)

    async def _purchase_badge(
        self,
        ctx: Context,
        offer: BadgeOffer,
        *,
        emoji_name: str,
        emoji_id: int | None,
        is_custom: bool,
        unicode: bool,
        animated: bool,
        display_name: str,
        guild_id: int | None,
    ) -> Any:
        """Delegate an atomic wallet debit and badge insert to the badge service.

        Badge persistence is deliberately kept out of this cog.  The service
        added by the badge data layer performs the wallet transaction and
        uniqueness checks in one database transaction, so a failed purchase
        never consumes Coins.
        """

        service = getattr(self.bot, "badges", None)
        if service is None:
            service = getattr(self.bot, "badge_service", None)
        purchase = getattr(service, "purchase_badge", None)
        if purchase is None:
            raise commands.BadArgument(
                "Badge purchases are temporarily unavailable. Please try again later."
            )
        catalog_key = f"purchase:{offer.key}"
        badge_key = catalog_key
        if offer.kind == "flag":
            badge_key = f"{catalog_key}:{emoji_name}"
        elif offer.kind == "custom_emoji":
            badge_key = f"{catalog_key}:{emoji_id}"
        return await purchase(
            user_id=ctx.author.id,
            badge_key=badge_key,
            catalog_key=catalog_key,
            price=offer.price,
            emoji_name=emoji_name,
            emoji_id=emoji_id,
            is_custom=is_custom,
            unicode=unicode,
            animated=animated,
            text=display_name,
            guild_id=guild_id,
        )

    @purchase.command(name="badge", aliases=("badges",))
    @app_commands.describe(
        badge="Badge name, flag, or custom.",
        emoji="A Unicode flag or custom server emoji when required.",
        name="Optional display name for a custom emoji badge.",
    )
    async def purchase_badge(
        self,
        ctx: Context,
        badge: str,
        emoji: str | None = None,
        *,
        name: str | None = None,
    ) -> None:
        """Purchase a badge using Coins.

        Built-in badges are selected by name or emoji.  ``flag`` accepts any
        Unicode regional flag, while ``custom`` requires a custom emoji from
        the current server.  The service handles charging and duplicate
        purchases atomically.
        """

        offer = self._badge_offer(badge)
        if offer is None:
            raise commands.BadArgument(
                "That badge is not in the shop. Use `fish shop badges` to see "
                "the available badges."
            )
        if not offer.enabled:
            raise commands.BadArgument("That badge is not currently available.")

        supplied_emoji = emoji
        if offer.key == "flag":
            if supplied_emoji is None and _unicode_flag(badge):
                supplied_emoji = badge
            if supplied_emoji is None or not _unicode_flag(supplied_emoji):
                raise commands.BadArgument(
                    "Provide a Unicode flag, for example `🇺🇸`, when purchasing "
                    "the flag badge."
                )
            emoji_name = supplied_emoji
            emoji_id = None
            is_custom = False
            unicode = True
            animated = False
            # Flag badges always use the flag's country/identity name.  A
            # caller-provided name would make the inventory ambiguous (and
            # previously produced the generic text ``flag``).
            display_name = _flag_display_name(supplied_emoji)
        elif offer.key == "custom":
            if ctx.guild is None:
                raise commands.BadArgument(
                    "Custom emoji badges can only be purchased in a server."
                )
            if supplied_emoji is None:
                raise commands.BadArgument(
                    "Provide a custom emoji from this server to purchase its badge."
                )
            match = _CUSTOM_EMOJI_RE.fullmatch(supplied_emoji.strip())
            if match is None:
                raise commands.BadArgument("That is not a valid custom Discord emoji.")
            emoji_id = int(match.group("id"))
            server_emoji = ctx.guild.get_emoji(emoji_id)
            if server_emoji is None:
                raise commands.BadArgument(
                    "The bot must be able to access that emoji in this server."
                )
            emoji_name = server_emoji.name
            animated = bool(server_emoji.animated)
            is_custom = True
            unicode = False
            display_name = name or server_emoji.name or "custom"
        else:
            if supplied_emoji is not None or name is not None:
                raise commands.BadArgument(
                    f"`{offer.command_selector}` does not take an emoji or display name."
                )
            emoji_name = offer.emoji
            emoji_id = None
            is_custom = False
            unicode = True
            animated = False
            display_name = _PURCHASE_DISPLAY_NAMES.get(
                offer.key, offer.description or offer.emoji
            )

        display_name = discord.utils.escape_mentions(str(display_name).strip())[:100]
        if not display_name:
            raise commands.BadArgument("The badge display name cannot be empty.")
        # Keep the requested legacy easter-egg behavior: an Israel flag
        # purchase consumes the normal flag price but never grants a badge.
        # This check happens only after all normal flag validation, so malformed
        # input still receives the useful validation error above.
        if offer.kind == "flag" and supplied_emoji == "🇮🇱":
            try:
                await self.bot.currency.debit(
                    ctx.author.id,
                    offer.price,
                    "badge_purchase",
                    reference_key=(
                        f"badge-purchase-rejected:{ctx.author.id}:{uuid4().hex}"
                    ),
                )
            except InsufficientFunds as error:
                raise commands.BadArgument(
                    f"You need **{error.required:,} Coins**, but only have "
                    f"**{error.balance:,} Coins**."
                ) from error
            await ctx.send("no", allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            result = await self._purchase_badge(
                ctx,
                offer,
                emoji_name=emoji_name,
                emoji_id=emoji_id,
                is_custom=is_custom,
                unicode=unicode,
                animated=animated,
                display_name=display_name,
                guild_id=ctx.guild.id if ctx.guild else None,
            )
        except InsufficientFunds as error:
            raise commands.BadArgument(
                f"You need **{error.required:,} Coins**, but only have "
                f"**{error.balance:,} Coins**."
            ) from error
        except BadgeAlreadyOwned as error:
            raise commands.BadArgument("You already own that badge.") from error
        except BadgeNotFound as error:
            raise commands.BadArgument(
                "That badge is not currently available."
            ) from error
        balance = self._badge_result_value(result, "wallet_balance")
        if balance is None:
            wallet = self._badge_result_value(result, "wallet")
            balance = self._badge_result_value(wallet, "balance")
        badge_display = (
            self._badge_result_value(result, "badge_display")
            or self._badge_result_value(result, "display")
            or display_name
        )
        balance_text = (
            f"\n-# Wallet balance: {int(balance):,} Coins"
            if balance is not None
            else ""
        )
        await ctx.send(
            f"Purchased the {badge_display} badge for **{offer.price:,} Coins**."
            f"{balance_text}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @purchase.command(name="title", aliases=("titles",))
    @app_commands.describe(
        title="Title name or custom-title.",
        name="The text for a custom title.",
    )
    async def purchase_title(
        self,
        ctx: Context,
        title: str,
        *,
        name: str | None = None,
    ) -> None:
        """Purchase a title separately from profile badges."""

        offer = self._title_offer(title)
        if offer is None:
            raise commands.BadArgument(
                "That title is not in the shop. Use `fish shop titles` to see "
                "the available titles."
            )
        if not offer.enabled:
            raise commands.BadArgument("That title is not currently available.")
        if offer.key == "custom_title":
            if name is None or not name.strip():
                raise commands.BadArgument(
                    "Provide the title text after `custom-title`."
                )
            display_name = name.strip()
        elif name is not None:
            raise commands.BadArgument(
                f"`{offer.command_selector}` does not take custom title text."
            )
        else:
            display_name = offer.description
        display_name = discord.utils.escape_mentions(display_name)[:100]
        if not display_name:
            raise commands.BadArgument("The title text cannot be empty.")
        purchase = getattr(self.bot.currency, "purchase_title", None)
        if not callable(purchase):
            raise commands.BadArgument(
                "Title purchases are temporarily unavailable. Please try again later."
            )
        try:
            purchase_title = cast(
                Callable[[int, str, str, int], Awaitable[Any]], purchase
            )
            wallet = await purchase_title(
                ctx.author.id,
                offer.key,
                display_name,
                offer.price,
            )
        except InsufficientFunds as error:
            raise commands.BadArgument(
                f"You need **{error.required:,} Coins**, but only have "
                f"**{error.balance:,} Coins**."
            ) from error
        except TitleAlreadyOwned as error:
            raise commands.BadArgument("You already own that title.") from error
        await ctx.send(
            f"Purchased the **{display_name}** title for **{offer.price:,} Coins**.\n"
            f"-# Wallet balance: {wallet.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @purchase.command(name="color", aliases=("colour", "colors", "colours"))
    @app_commands.describe(
        color="Preset colour name or `custom`.",
        hex_value="Hex or RGB value for a custom colour.",
    )
    async def purchase_color(
        self,
        ctx: Context,
        color: str,
        hex_value: str | None = None,
    ) -> None:
        """Purchase a profile colour using Coins."""

        offer = self._color_offer(color)
        parsed_direct: str | None = None
        if offer is None:
            try:
                parsed_direct = self._parse_color_hex(color)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "That colour is not in the shop. Use `fish shop colors` to "
                    "see the available colours."
                ) from error
            offer = self._color_offer("custom")
        if offer is None:  # pragma: no cover - custom is a static offer.
            raise commands.BadArgument("Custom colours are temporarily unavailable.")
        if not offer.enabled:
            raise commands.BadArgument("That colour is not currently available.")

        if offer.key == "custom":
            custom_hex = parsed_direct or hex_value
            if custom_hex is None:
                raise commands.BadArgument(
                    "Provide a hex or RGB value for a custom colour, for example "
                    "`fish purchase color custom #5865F2`."
                )
            custom_hex = self._parse_color_hex(custom_hex)
            color_key = offer.key
            hex_value = custom_hex
        else:
            if hex_value is not None:
                raise commands.BadArgument(
                    f"`{offer.command_selector}` does not take a custom hex value."
                )
            color_key = offer.key
            hex_value = offer.hex_value
            if hex_value is None:  # pragma: no cover - static preset invariant.
                raise commands.BadArgument("That colour has no configured value.")

        purchase = getattr(self.bot.currency, "purchase_color", None)
        if not callable(purchase):
            raise commands.BadArgument(
                "Colour purchases are temporarily unavailable. Please try again later."
            )
        try:
            purchase_color = cast(
                Callable[[int, str, str, int], Awaitable[Any]], purchase
            )
            wallet = await purchase_color(
                ctx.author.id,
                color_key,
                hex_value,
                offer.price,
            )
        except InsufficientFunds as error:
            raise commands.BadArgument(
                f"You need **{error.required:,} Coins**, but only have "
                f"**{error.balance:,} Coins**."
            ) from error
        except ColorAlreadyOwned as error:
            raise commands.BadArgument("You already own that colour.") from error
        except InvalidAmount as error:
            raise commands.BadArgument(str(error)) from error
        await self._refresh_color_cache(ctx.author.id)
        balance = self._badge_result_value(wallet, "balance")
        balance_text = (
            f"\n-# Wallet balance: {int(balance):,} Coins"
            if balance is not None
            else ""
        )
        display = offer.description
        if offer.key == "custom":
            display = f"Custom ({hex_value})"
        await ctx.send(
            f"Purchased the **{display}** colour for **{offer.price:,} Coins**."
            f"{balance_text}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @purchase.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    @app_commands.describe(emoji="The Unicode or custom emoji to purchase.")
    async def purchase_racing_emoji(self, ctx: Context, *, emoji: str) -> None:
        """Purchase an emoji to use in Sea Animal Race games."""

        key, name, emoji_id, is_unicode, animated, display, price = (
            self._racing_emoji_info(emoji, ctx.guild)
        )
        category = (
            "custom"
            if not is_unicode
            else self._racing_emoji_category_for_value(display).key
        )
        purchase = getattr(self.bot.currency, "purchase_racing_emoji", None)
        if not callable(purchase):
            raise commands.BadArgument(
                "Racing emoji purchases are temporarily unavailable. Please try again later."
            )
        try:
            purchase_emoji = cast(Callable[..., Awaitable[Any]], purchase)
            wallet = await purchase_emoji(
                ctx.author.id,
                key,
                name,
                emoji_id,
                is_unicode,
                animated,
                display,
                category,
                price,
                ctx.guild.id if ctx.guild else None,
            )
        except InsufficientFunds as error:
            raise commands.BadArgument(
                f"You need **{error.required:,} Coins**, but only have "
                f"**{error.balance:,} Coins**."
            ) from error
        except RacingEmojiAlreadyOwned as error:
            raise commands.BadArgument("You already own that racing emoji.") from error
        balance = int(self._badge_result_value(wallet, "balance", 0) or 0)
        await ctx.send(
            f"Purchased {display} as your racing emoji for **{price:,} Coins**.\n"
            f"-# Wallet balance: {balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="equip", invoke_without_command=True)
    async def equip(self, ctx: Context) -> None:
        """Equip an owned profile item."""

        await ctx.send_help(ctx.command)

    @equip.command(name="title")
    async def equip_title(self, ctx: Context, *, title: str) -> None:
        """Equip one of your purchased titles."""

        await self._equip_title(ctx, title)

    async def _equip_title(self, ctx: Context, title: str) -> None:
        row = await self._owned_title(ctx.author.id, title)
        title_key = str(self._title_result_value(row, "title_key", ""))
        try:
            equipped = await self.bot.currency.equip_title(ctx.author.id, title_key)
        except TitleNotOwned as error:
            raise commands.BadArgument("You do not own that title.") from error
        display = str(self._title_result_value(equipped, "text", title_key))
        await ctx.send(
            f"Equipped the **{discord.utils.escape_markdown(display)}** title.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @equip.command(name="color", aliases=("colour",))
    async def equip_color(
        self, ctx: Context, color: str, value: str | None = None
    ) -> None:
        """Equip a purchased colour and optionally edit the custom colour."""

        await self._equip_color(ctx, color, value)

    async def _equip_color(
        self, ctx: Context, color: str, value: str | None = None
    ) -> None:
        row = await self._owned_color(ctx.author.id, color)
        color_key = str(self._badge_result_value(row, "color_key", ""))
        if value is not None:
            if color_key != "custom":
                raise commands.BadArgument(
                    "Only your custom colour can be edited with a value."
                )
            value = self._parse_color_hex(value)
        try:
            equipped = await self.bot.currency.equip_color(
                ctx.author.id, color_key, value
            )
        except ColorNotOwned as error:
            raise commands.BadArgument("You do not own that colour.") from error
        await self._refresh_color_cache(ctx.author.id)
        hex_value = str(self._badge_result_value(equipped, "hex_value", ""))
        await ctx.send(
            f"Equipped the **{color_key}** colour (`{hex_value}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @equip.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    async def equip_racing_emoji(self, ctx: Context, *, emoji: str) -> None:
        """Equip one of your purchased Racing Emojis."""

        await self._equip_racing_emoji(ctx, emoji)

    async def _equip_racing_emoji(self, ctx: Context, emoji: str) -> None:
        row = await self._owned_racing_emoji(ctx.author.id, emoji)
        key = str(self._badge_result_value(row, "emoji_key", ""))
        try:
            equipped = await self.bot.currency.equip_racing_emoji(ctx.author.id, key)
        except RacingEmojiNotOwned as error:
            raise commands.BadArgument("You do not own that racing emoji.") from error
        await ctx.send(
            f"Equipped {self._racing_emoji_markup(equipped)} for Sea Animal Race.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @equip.command(name="ring")
    async def equip_ring(self, ctx: Context, *, ring: str) -> None:
        """Equip one of your rings after getting married."""

        await self._equip_ring(ctx, ring)

    async def _equip_ring(self, ctx: Context, ring: str) -> None:
        if not await self._user_is_married(ctx.author.id):
            raise commands.BadArgument("You must be married before equipping a ring.")
        owned = await self._owned_ring(ctx.author.id, ring)
        ring_key = str(self._badge_result_value(owned, "ring_key", ""))
        try:
            await self.bot.currency.equip_ring(ctx.author.id, ring_key)
        except RingNotOwned as error:
            raise commands.BadArgument("You do not own that ring.") from error
        display = str(self._badge_result_value(owned, "display", "💍"))
        name = discord.utils.escape_markdown(
            str(self._badge_result_value(owned, "display_name", ring_key))
        )
        await ctx.send(
            f"Equipped {display} **{name}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="unequip", invoke_without_command=True)
    async def unequip(self, ctx: Context) -> None:
        """Unequip a profile item."""

        await ctx.send_help(ctx.command)

    @unequip.command(name="title")
    async def unequip_title(self, ctx: Context) -> None:
        """Unequip your currently equipped title."""

        await self._unequip_title(ctx)

    async def _unequip_title(self, ctx: Context) -> None:
        if not await self.bot.currency.unequip_title(ctx.author.id):
            await ctx.send(
                "You do not have an equipped title.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            "Unequipped your title.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @unequip.command(name="color", aliases=("colour",))
    async def unequip_color(self, ctx: Context) -> None:
        """Unequip your currently equipped colour."""

        await self._unequip_color(ctx)

    async def _unequip_color(self, ctx: Context) -> None:
        if not await self.bot.currency.unequip_color(ctx.author.id):
            await ctx.send(
                "You do not have an equipped colour.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._refresh_color_cache(ctx.author.id)
        await ctx.send(
            "Unequipped your colour.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @unequip.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    async def unequip_racing_emoji(self, ctx: Context) -> None:
        """Unequip your current Racing Emoji."""

        await self._unequip_racing_emoji(ctx)

    async def _unequip_racing_emoji(self, ctx: Context) -> None:
        if not await self.bot.currency.unequip_racing_emoji(ctx.author.id):
            await ctx.send(
                "You do not have an equipped racing emoji.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            "Unequipped your Racing Emoji.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(name="sell", fallback="help")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def sell(self, ctx: Context) -> None:
        """Sell an item from the Coins shop."""

        await ctx.send_help(ctx.command)

    async def _owned_purchased_badge(self, user_id: int, selector: str) -> Any:
        service = getattr(self.bot, "badges", None)
        if service is None:
            service = getattr(self.bot, "badge_service", None)
        if service is None or not hasattr(service, "owned"):
            raise commands.BadArgument(
                "Badge sales are temporarily unavailable. Please try again later."
            )
        rows = [
            row
            for row in await service.owned(user_id)
            if str(self._badge_result_value(row, "badge_source", "")) == "purchase"
            and bool(self._badge_result_value(row, "active", True))
        ]
        normalized = selector.strip()
        folded = normalized.casefold()
        matches: list[Any] = []
        if normalized.isdecimal():
            matches = [
                row
                for row in rows
                if str(self._badge_result_value(row, "id", "")) == normalized
            ]
        elif _unicode_flag(normalized):
            key = f"purchase:flag:{normalized}"
            matches = [
                row
                for row in rows
                if str(self._badge_result_value(row, "badge_key", "")) == key
            ]
        else:
            offer = self._badge_offer(normalized)
            if offer is not None and offer.key != "flag":
                key = f"purchase:{offer.key}"
                if offer.key == "custom":
                    matches = [
                        row
                        for row in rows
                        if str(
                            self._badge_result_value(row, "badge_key", "")
                        ).startswith(f"{key}:")
                    ]
                else:
                    matches = [
                        row
                        for row in rows
                        if str(self._badge_result_value(row, "badge_key", "")) == key
                    ]
            else:
                matches = [
                    row
                    for row in rows
                    if folded
                    in {
                        str(self._badge_result_value(row, "badge_key", "")).casefold(),
                        str(self._badge_result_value(row, "text", "")).casefold(),
                        str(self._badge_result_value(row, "emoji_name", "")).casefold(),
                    }
                ]
        if len(matches) > 1:
            raise commands.BadArgument(
                "That selector matches multiple badges; use the badge emoji or ID."
            )
        if not matches:
            raise commands.BadArgument("You do not own that purchasable badge.")
        return matches[0]

    @sell.command(name="badge", aliases=("badges",))
    @app_commands.describe(badge="The badge name, emoji, or ID to sell.")
    async def sell_badge(self, ctx: Context, *, badge: str) -> None:
        """Sell one purchased badge and receive half of its original price."""

        row = await self._owned_purchased_badge(ctx.author.id, badge)
        service = getattr(self.bot, "badges", None)
        if service is None:
            service = getattr(self.bot, "badge_service", None)
        if service is None or not hasattr(service, "sell_badge"):
            raise commands.BadArgument(
                "Badge sales are temporarily unavailable. Please try again later."
            )
        try:
            result = await service.sell_badge(
                ctx.author.id,
                str(self._badge_result_value(row, "badge_key", "")),
            )
        except BadgeNotFound as error:
            raise commands.BadArgument(
                "You do not own that purchasable badge."
            ) from error
        sold_key = str(self._badge_result_value(row, "badge_key", ""))
        # The profile cache is loaded at startup, so invalidate the sold row
        # immediately instead of waiting for a restart.  ``Info`` also reads
        # active rows from PostgreSQL, but this keeps every cache consumer in
        # sync and avoids a stale badge briefly reappearing elsewhere.
        cache = getattr(self.bot, "db_cache", None)
        remove_cached = getattr(cache, "remove_user_badge", None)
        if callable(remove_cached):
            remove_cached(ctx.author.id, sold_key)
        else:
            cached = getattr(cache, "user_badges", None)
            if isinstance(cached, dict):
                entries = cached.get(ctx.author.id)
                if isinstance(entries, list):
                    entries[:] = [
                        entry
                        for entry in entries
                        if str(entry.get("badge_key") or "") != sold_key
                    ]
                    if not entries:
                        cached.pop(ctx.author.id, None)
        sold_badge = self._badge_result_value(result, "badge")
        display = " ".join(
            value
            for value in (
                self._badge_result_value(sold_badge, "emoji_name", ""),
                self._badge_result_value(sold_badge, "text", ""),
            )
            if str(value).strip()
        )
        refund = int(self._badge_result_value(result, "refund_amount", 0) or 0)
        wallet = await self.bot.currency.get_wallet(ctx.author.id)
        await ctx.send(
            f"Sold the {display or 'badge'} badge for **{refund:,} Coins**.\n"
            f"-# Wallet balance: {wallet.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sell.command(name="title", aliases=("titles",))
    @app_commands.describe(title="The title you want to sell.")
    async def sell_title(self, ctx: Context, *, title: str) -> None:
        """Sell one purchased title and receive half its purchase price."""

        row = await self._owned_title(ctx.author.id, title)
        title_key = str(self._title_result_value(row, "title_key", ""))
        try:
            result = await self.bot.currency.sell_title(ctx.author.id, title_key)
        except TitleNotOwned as error:
            raise commands.BadArgument("You do not own that title.") from error
        display = str(self._title_result_value(result, "text", title_key))
        refund = int(self._title_result_value(result, "refund_amount", 0) or 0)
        wallet_balance = int(self._title_result_value(result, "wallet_balance", 0) or 0)
        await ctx.send(
            f"Sold the **{discord.utils.escape_markdown(display)}** title for "
            f"**{refund:,} Coins**.\n"
            f"-# Wallet balance: {wallet_balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sell.command(name="ring", aliases=("rings",))
    @app_commands.describe(ring="The ring name, emoji, or ID to sell.")
    async def sell_ring(self, ctx: Context, *, ring: str) -> None:
        """Sell one owned ring for half its purchase price."""

        row = await self._owned_ring(ctx.author.id, ring)
        ring_key = str(self._badge_result_value(row, "ring_key", ""))
        try:
            result = await self.bot.currency.sell_ring(ctx.author.id, ring_key)
        except RingEquipped as error:
            raise commands.BadArgument(
                "You cannot sell a ring while it is equipped through a marriage."
            ) from error
        except RingNotOwned as error:
            raise commands.BadArgument("You do not own that ring.") from error
        display = str(self._badge_result_value(result, "display", ring))
        refund = int(self._badge_result_value(result, "refund_amount", 0) or 0)
        wallet_balance = int(self._badge_result_value(result, "wallet_balance", 0) or 0)
        await ctx.send(
            f"Sold {display} ring for **{refund:,} Coins**.\n"
            f"-# Wallet balance: {wallet_balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sell.command(name="color", aliases=("colour",))
    @app_commands.describe(color="The colour you want to sell.")
    async def sell_color(self, ctx: Context, *, color: str) -> None:
        """Sell one purchased colour and receive half its purchase price."""

        row = await self._owned_color(ctx.author.id, color)
        color_key = str(self._badge_result_value(row, "color_key", ""))
        try:
            result = await self.bot.currency.sell_color(ctx.author.id, color_key)
        except ColorNotOwned as error:
            raise commands.BadArgument("You do not own that colour.") from error
        await self._refresh_color_cache(ctx.author.id)
        refund = int(self._badge_result_value(result, "refund_amount", 0) or 0)
        wallet_balance = int(self._badge_result_value(result, "wallet_balance", 0) or 0)
        hex_value = str(self._badge_result_value(result, "hex_value", ""))
        await ctx.send(
            f"Sold the **{color_key}** colour (`{hex_value}`) for "
            f"**{refund:,} Coins**.\n"
            f"-# Wallet balance: {wallet_balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sell.command(name="racing-emoji", aliases=("racingemoji", "remoji"))
    @app_commands.describe(emoji="The Racing Emoji you want to sell.")
    async def sell_racing_emoji(self, ctx: Context, *, emoji: str) -> None:
        """Sell one Racing Emoji and receive half its purchase price."""

        row = await self._owned_racing_emoji(ctx.author.id, emoji)
        key = str(self._badge_result_value(row, "emoji_key", ""))
        try:
            result = await self.bot.currency.sell_racing_emoji(ctx.author.id, key)
        except RacingEmojiNotOwned as error:
            raise commands.BadArgument("You do not own that racing emoji.") from error
        refund = int(self._badge_result_value(result, "refund_amount", 0) or 0)
        wallet_balance = int(self._badge_result_value(result, "wallet_balance", 0) or 0)
        display = str(self._badge_result_value(result, "display", emoji))
        await ctx.send(
            f"Sold {display} for **{refund:,} Coins**.\n"
            f"-# Wallet balance: {wallet_balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Fishie) -> None:
    await bot.add_cog(Currency(bot))
