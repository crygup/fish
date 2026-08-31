from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from discord.ext import commands

from core.currency import (
    MAX_WAGER_PAYOUT,
    BalanceOverflow,
    ClaimResult,
    CurrencyService,
    GamblingStats,
    InvalidAmount,
    Wallet,
    claim_period_start,
    next_claim_reset,
)
from extensions.currency import (
    PURCHASABLE_BADGES,
    PURCHASABLE_COLORS,
    PURCHASABLE_TITLES,
    BadgeShopView,
    ColorShopView,
    Currency,
    LotteryShopView,
    RacingEmojiShopView,
    ShopLandingView,
    TitleOffer,
    TitleShopView,
    _racing_emoji_category,
    _unicode_flag,
)
from extensions.currency.work import WorkClickView, WorkMathView


class MemoryConnection:
    def __init__(self) -> None:
        self.wallets: dict[int, int] = {}
        self.rewards: dict[tuple[int, str, object], int] = {}
        self.transactions: list[tuple[int, int, str]] = []
        self.gambling_stats: dict[int, dict[str, int]] = {}

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        if normalized.startswith("INSERT INTO currency_wagers"):
            return {
                "id": 1,
                "user_id": int(args[0]),
                "source": str(args[1]),
                "stake": int(args[2]),
                "status": "open",
                "payout": 0,
            }
        if "FROM currency_wallets" in normalized:
            user_id = int(args[0])
            return {"user_id": user_id, "balance": self.wallets[user_id]}
        if "FROM currency_daily_rewards" in normalized:
            key = (int(args[0]), str(args[1]), args[2])
            amount = self.rewards.get(key)
            return None if amount is None else {"amount": amount}
        if "FROM currency_gambling_stats" in normalized:
            stats = self.gambling_stats.get(int(args[0]))
            return None if stats is None else {"user_id": int(args[0]), **stats}
        raise AssertionError(f"Unexpected fetchrow query: {normalized}")

    async def execute(self, query: str, *args: Any) -> str:
        normalized = " ".join(query.split())
        if normalized.startswith("INSERT INTO currency_wallets"):
            self.wallets.setdefault(int(args[0]), 0)
        elif normalized.startswith("INSERT INTO currency_daily_rewards"):
            key = (int(args[0]), str(args[1]), args[2])
            self.rewards[key] = self.rewards.get(key, 0) + int(args[3])
        elif normalized.startswith("UPDATE currency_wallets"):
            if "balance = balance - $2" in normalized:
                self.wallets[int(args[0])] -= int(args[1])
            else:
                self.wallets[int(args[0])] += int(args[1])
        elif normalized.startswith("INSERT INTO currency_transactions"):
            self.transactions.append((int(args[0]), int(args[1]), str(args[2])))
        elif normalized.startswith("INSERT INTO currency_gambling_stats"):
            user_id = int(args[0])
            stats = self.gambling_stats.setdefault(
                user_id,
                {
                    "total_wagered": 0,
                    "total_earned": 0,
                    "total_lost": 0,
                    "wins": 0,
                    "losses": 0,
                },
            )
            if "total_wagered" in normalized:
                stats["total_wagered"] += int(args[1])
            elif "total_earned" in normalized:
                stats["total_earned"] += int(args[1])
                stats["wins"] += 1
            else:
                stats["total_lost"] += int(args[1])
                stats["losses"] += 1
        else:
            raise AssertionError(f"Unexpected execute query: {normalized}")
        return "OK"


class MemoryPool:
    def __init__(self) -> None:
        self.connection = MemoryConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class WagerMemoryConnection(MemoryConnection):
    def __init__(self) -> None:
        super().__init__()
        self.wagers: dict[int, dict[str, Any]] = {}

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        if normalized.startswith("INSERT INTO currency_wagers"):
            row = {
                "id": 1,
                "user_id": int(args[0]),
                "source": str(args[1]),
                "stake": int(args[2]),
                "status": "open",
                "payout": 0,
            }
            self.wagers[1] = row
            return row.copy()
        if "FROM currency_wagers" in normalized:
            row = self.wagers.get(int(args[0]))
            return None if row is None else row.copy()
        if normalized.startswith("UPDATE currency_wallets"):
            user_id, payout, maximum_before_payout = map(int, args)
            assert "balance <= $3" in normalized
            if self.wallets[user_id] > maximum_before_payout:
                return None
            self.wallets[user_id] += payout
            return {"user_id": user_id, "balance": self.wallets[user_id]}
        if normalized.startswith("UPDATE currency_wagers"):
            wager_id, status, payout = int(args[0]), str(args[1]), int(args[2])
            row = self.wagers[wager_id]
            row.update(status=status, payout=payout)
            return row.copy()
        return await super().fetchrow(query, *args)


class ClaimMemoryConnection(MemoryConnection):
    def __init__(self) -> None:
        super().__init__()
        self.claims: dict[tuple[int, str, object], dict[str, int]] = {}

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT streak FROM currency_claims"):
            row = self.claims.get((int(args[0]), str(args[1]), args[2]))
            return None if row is None else {"streak": row["streak"]}
        if normalized.startswith("INSERT INTO currency_claims"):
            key = (int(args[0]), str(args[1]), args[2])
            if key in self.claims:
                return None
            row = {
                "base_amount": int(args[3]),
                "bonus_amount": int(args[4]),
                "streak": int(args[5]),
                "streak_bonus_amount": int(args[6]),
            }
            self.claims[key] = row
            return row.copy()
        if normalized.startswith("SELECT base_amount, bonus_amount, streak"):
            row = self.claims[(int(args[0]), str(args[1]), args[2])]
            return row.copy()
        if normalized.startswith("UPDATE currency_wallets SET balance = balance +"):
            user_id, amount = int(args[0]), int(args[1])
            self.wallets[user_id] += amount
            return {"user_id": user_id, "balance": self.wallets[user_id]}
        return await super().fetchrow(query, *args)


def test_currency_claim_periods_use_utc_day_and_sunday_week() -> None:
    now = datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc)  # Monday
    assert claim_period_start("daily", now).isoformat() == "2026-08-24"
    assert claim_period_start("weekly", now).isoformat() == "2026-08-23"
    assert next_claim_reset("daily", now).isoformat() == "2026-08-25T00:00:00+00:00"
    assert next_claim_reset("weekly", now).isoformat() == "2026-08-30T00:00:00+00:00"


async def test_daily_capped_reward_lazily_creates_wallet_and_stops_at_cap() -> None:
    pool = MemoryPool()
    service = CurrencyService(pool)
    now = datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc)

    awards = [
        await service.award_daily_capped(42, 10, 100, "game_easy", now=now)
        for _ in range(11)
    ]

    assert awards == ([10] * 10) + [0]
    assert pool.connection.wallets[42] == 100
    assert len(pool.connection.transactions) == 10


async def test_daily_claims_add_a_streak_bonus_and_reset_after_a_missed_day() -> None:
    pool = MemoryPool()
    pool.connection = ClaimMemoryConnection()
    service = CurrencyService(pool)
    first_day = datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc)
    second_day = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)
    missed_day = datetime(2026, 8, 27, 2, 30, tzinfo=timezone.utc)

    first = await service.claim(
        42, "daily", 100, streak_bonus_per_period=10, now=first_day
    )
    second = await service.claim(
        42, "daily", 100, streak_bonus_per_period=10, now=second_day
    )
    missed = await service.claim(
        42, "daily", 100, streak_bonus_per_period=10, now=missed_day
    )

    assert (first.streak, first.streak_bonus_amount, first.amount) == (1, 10, 110)
    assert (second.streak, second.streak_bonus_amount, second.amount) == (2, 20, 120)
    assert (missed.streak, missed.streak_bonus_amount, missed.amount) == (1, 10, 110)
    assert pool.connection.wallets[42] == 340


async def test_claim_command_applies_daily_streak_bonus() -> None:
    result = ClaimResult(
        wallet=Wallet(user_id=42, balance=130),
        claim_type="daily",
        period_start=claim_period_start("daily"),
        claimed=True,
        base_amount=100,
        bonus_amount=0,
        streak=3,
        streak_bonus_amount=30,
    )
    claim = AsyncMock(return_value=result)
    cog = Currency.__new__(Currency)
    cog.bot = cast(
        Any,
        SimpleNamespace(
            currency=SimpleNamespace(claim=claim),
        ),
    )
    ctx = cast(Any, SimpleNamespace(author=SimpleNamespace(id=42), send=AsyncMock()))

    await cog._claim(ctx, "daily", 100)

    claim.assert_awaited_once_with(42, "daily", 100, 0, streak_bonus_per_period=10)
    assert "130 Coins" in ctx.send.await_args.args[0]
    assert "+30 daily streak bonus" in ctx.send.await_args.args[0]


async def test_wager_optional_maximum_is_enforced_only_when_requested() -> None:
    service = CurrencyService(SimpleNamespace())
    with pytest.raises(InvalidAmount, match="between 10 and 10,000"):
        await service.open_wager(42, 10_001, max_stake=10_000)
    with pytest.raises(InvalidAmount, match="at least 10"):
        await service.open_wager(42, 9)
    with pytest.raises(InvalidAmount):
        await service.settle_wager(1, MAX_WAGER_PAYOUT + 1)


async def test_open_wager_records_a_typed_negative_transaction_amount() -> None:
    pool = MemoryPool()
    pool.connection.wallets[42] = 250

    wager = await CurrencyService(pool).open_wager(42, 100)

    assert wager.stake == 100
    assert pool.connection.wallets[42] == 150
    assert pool.connection.transactions == [(42, -100, "wager_stake:higher_lower")]


async def test_open_wager_allows_the_new_10000_coin_cap() -> None:
    pool = MemoryPool()
    pool.connection.wallets[42] = 10_000

    wager = await CurrencyService(pool).open_wager(42, 10_000)

    assert wager.stake == 10_000
    assert pool.connection.wallets[42] == 0
    assert pool.connection.transactions == [(42, -10_000, "wager_stake:higher_lower")]


async def test_cash_out_uses_a_precomputed_overflow_limit() -> None:
    pool = MemoryPool()
    pool.connection = WagerMemoryConnection()
    pool.connection.wallets[42] = 500
    wager = await CurrencyService(pool).open_wager(42, 100)

    result = await CurrencyService(pool).settle_wager(wager.id, 225)

    assert result.settled
    assert result.wager.payout == 225
    assert result.balance == 625
    assert pool.connection.transactions == [
        (42, -100, "wager_stake:higher_lower"),
        (42, 225, "wager_cashout:higher_lower"),
    ]


async def test_gambling_stats_record_stake_and_settlement_outcome() -> None:
    pool = MemoryPool()
    pool.connection = WagerMemoryConnection()
    pool.connection.wallets[42] = 500
    service = CurrencyService(pool)

    wager = await service.open_wager(42, 100)
    await service.settle_wager(wager.id, 0)

    stats = await service.get_gambling_stats(42)
    assert stats == GamblingStats(
        user_id=42,
        total_wagered=100,
        total_earned=0,
        total_lost=100,
        wins=0,
        losses=1,
    )
    assert stats.wagers == 1
    assert stats.win_percentage == 0


async def test_gambling_stats_default_to_zero_without_a_wallet() -> None:
    stats = await CurrencyService(MemoryPool()).get_gambling_stats(42)

    assert stats == GamblingStats(user_id=42)


def test_currency_commands_are_a_separate_hybrid_category_with_aliases() -> None:
    assert str(Currency.emoji) == "🪙"
    assert Currency.wallet.aliases == ("balance", "bal", "coins")
    assert "buy" in Currency.purchase.aliases
    assert Currency.daily.with_app_command
    assert Currency.weekly.with_app_command


def test_badge_shop_offers_and_aliases() -> None:
    keys = {offer.key for offer in PURCHASABLE_BADGES}
    assert {
        "fish",
        "amulet",
        "flag",
        "alien",
        "custom",
        "smiling_imp",
        "imp",
        "seal",
        "cat",
        "dog",
        "mouse",
        "hamster",
        "rabbit",
        "fox",
    } <= keys
    amulet = Currency._badge_offer("nazar")
    assert amulet is not None and amulet.key == "amulet"
    amulet = Currency._badge_offer("🪬")
    assert amulet is not None and amulet.key == "amulet"
    assert Currency._badge_offer("fishie") is None
    hamsa = Currency._title_offer("hamsa")
    assert hamsa is None
    title = Currency._title_offer("custom title")
    assert title is not None and title.key == "custom_title"
    title = Currency._title_offer("custom")
    assert title is not None and title.key == "custom_title"
    flag = Currency._badge_offer("🇺🇸")
    assert flag is not None and flag.key == "flag"
    assert _unicode_flag("🇺🇸")
    assert _unicode_flag("🏳️‍🌈")
    assert not _unicode_flag("🐟")


def test_shop_catalog_comes_from_editable_json_seed() -> None:
    from utils.paths import FILES_ROOT

    catalog = FILES_ROOT / "data" / "shop_catalog.json"
    document = json.loads(catalog.read_text(encoding="utf-8"))
    assert {entry["key"] for entry in document["badges"]} == {
        offer.key for offer in PURCHASABLE_BADGES
    }
    assert {entry["key"] for entry in document["titles"]} == {
        offer.key for offer in PURCHASABLE_TITLES
    }
    assert all("enabled" in entry for entry in document["badges"])
    assert all("category" in entry for entry in document["titles"])


def test_badge_shop_is_components_v2_and_sorted_by_price() -> None:
    import discord

    view = BadgeShopView()
    assert isinstance(view, discord.ui.LayoutView)
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    displays = [
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    assert displays[0] == (
        "## Badge shop\nSpend Coins to add one of these badges to your profile."
    )
    assert displays[-1] == "Purchase one with `fish purchase badge <badge>`."
    assert "Custom Discord emoji (`custom`) · **10,000,000,000 Coins**" in displays[1]
    assert "Any Unicode flag (`flag`) · **10,000 Coins**" in displays[1]
    assert "🦊 Fox (`fox`) · **15,000 Coins**" in displays[1]


def test_title_shop_is_separate_and_sorted_by_price() -> None:
    import discord

    assert {offer.key for offer in PURCHASABLE_TITLES} >= {
        "custom_title",
        "fishie",
        "dr_pepper",
        "monarch",
        "mudae_enjoyer",
        "epic",
        "six_seven",
        "cool",
        "bot",
        "fan",
        "player",
        "star_platinum",
        "light_rod",
        "made_in_heaven",
        "pride",
        "reigen_arataka",
        "22",
        "wingstop_enjoyer",
        "oaf",
        "shingeki_no_kyojin",
        "the_dark_knight",
        "poor",
        "0_0",
        "greater_less",
        "less_three",
    }
    assert Currency._title_offer("fishie").price == 500_000_000
    view = TitleShopView()
    assert isinstance(view, discord.ui.LayoutView)
    container = view.children[0]
    displays = [
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    assert displays[0] == "## Title shop\nSpend Coins to add a title to your profile."
    assert "Custom Title (`custom-title`) · **1,000,000,000 Coins**" in displays[1]
    assert "-# Page 1/6" in displays[1]
    assert displays[-1] == "Purchase one with `fish purchase title <title>`."


def test_title_shop_resolves_display_names_and_categories() -> None:
    assert Currency._title_offer("Magician's Red").key == "magicians_red"
    assert Currency._title_offer("light-rod").price == 25_000_000
    assert Currency._title_offer("Soft & Wet").key == "soft_wet"
    assert {offer.category for offer in PURCHASABLE_TITLES} == {
        "Misc titles",
        "JoJo Stands",
        "Sins",
        "Emoticons",
    }
    assert Currency._title_offer(">_<").key == "greater_less"
    assert Currency._title_offer(">_>").key == "underscore_greater"
    assert Currency._title_offer("<3").key == "less_three"
    assert Currency._title_offer(":3").key == "colon_three"


def test_title_shop_pages_keep_fullest_page_first() -> None:
    offers = tuple(
        TitleOffer(
            key=f"title-{index}",
            price=1,
            description=f"Title {index}",
        )
        for index in range(49)
    )

    assert [len(page) for page in TitleShopView._pages(offers[:16])] == [10, 6]
    assert [len(page) for page in TitleShopView._pages(offers[:31])] == [15, 10, 6]
    assert [len(page) for page in TitleShopView._pages(offers)] == [15, 14, 10, 10]


def test_title_shop_escapes_emoticon_names_but_not_selectors() -> None:
    import discord

    view = TitleShopView()
    view.category_index = next(
        index
        for index, (category, _offers) in enumerate(view._categories())
        if category == "Emoticons"
    )
    view._render()
    container = view.children[0]
    displays = [
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]

    assert r">\_< (`>_<`)" in displays[1]
    assert r"<3 (`<3`)" in displays[1]
    assert r"@\_@ (`@_@`)" in displays[1]


def test_base_shop_explains_purchase_and_has_category_selector() -> None:
    import discord

    view = ShopLandingView(command_prefix="fish ")
    assert isinstance(view, discord.ui.LayoutView)
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    displays = [
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    assert displays[0] == "## Coin shop"
    assert displays[1] == (
        "Spend Coins on profile badges, titles, colors, and lottery tickets.\n\n"
        "Buy items with `fish purchase <category> <item>`\n\n"
        "Choose a category below to browse the available items."
    )
    assert isinstance(view.children[1], discord.ui.ActionRow)
    selector = view.children[1].children[0]
    assert isinstance(selector, discord.ui.Select)
    assert [option.value for option in selector.options] == [
        "badges",
        "titles",
        "colors",
        "lottery",
        "racing-emoji",
    ]


def test_shop_prefix_can_render_slash_commands() -> None:
    import discord

    view = ShopLandingView(command_prefix="/")
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    displays = [
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    assert "Buy items with `/purchase <category> <item>`" in displays[1]


def test_lottery_shop_explains_ticket_price_and_draw() -> None:
    import discord

    view = LotteryShopView(command_prefix="fish ")
    assert isinstance(view, discord.ui.LayoutView)
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "## Lottery shop" in text
    assert "100 Coins" in text
    assert "starting at 1,000" in text
    assert "Every hour a ticket is selected" in text
    assert "fish purchase ticket <amount>" in text


def test_color_shop_contains_presets_and_custom_price_order() -> None:
    import discord

    assert {offer.key for offer in PURCHASABLE_COLORS} == {
        "red",
        "orange",
        "yellow",
        "green",
        "blue",
        "purple",
        "pink",
        "gray",
        "white",
        "black",
        "custom",
    }
    assert (
        next(offer for offer in PURCHASABLE_COLORS if offer.key == "custom").price
        == 25_000
    )
    assert (
        next(offer for offer in PURCHASABLE_COLORS if offer.key == "white").price
        == 10_000
    )
    assert (
        next(offer for offer in PURCHASABLE_COLORS if offer.key == "red").price == 5_000
    )
    view = ColorShopView()
    assert isinstance(view, discord.ui.LayoutView)
    container = view.children[0]
    displays = [
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    assert displays[0] == "## Color shop\nSpend Coins to add a profile color."
    assert displays[1].startswith("Custom (`custom`)")
    assert displays[1].index("25,000") < displays[1].index("5,000")


def test_color_hex_parser_accepts_short_and_prefixed_values() -> None:
    assert Currency._parse_color_hex("#abc") == "#AABBCC"
    assert Currency._parse_color_hex("0x123456") == "#123456"
    assert Currency._parse_color_hex("rgb(255, 165, 0)") == "#FFA500"
    assert Currency._parse_color_hex("0, 128, 255") == "#0080FF"


async def test_owned_custom_color_selector_only_matches_custom_row() -> None:
    cog = Currency.__new__(Currency)
    cog.bot = cast(
        Any,
        SimpleNamespace(
            currency=SimpleNamespace(
                owned_colors=AsyncMock(
                    return_value=[
                        {"color_key": "custom", "hex_value": "#C0AFF3"},
                        {"color_key": "red", "hex_value": "#FF0000"},
                    ]
                )
            )
        ),
    )

    selected = await cog._owned_color(42, "custom")

    assert selected["color_key"] == "custom"


def test_racing_emoji_categories_and_price_buckets() -> None:
    assert _racing_emoji_category("🐟").key == "sea"
    assert _racing_emoji_category("😀").key == "face"
    assert _racing_emoji_category("❤️").key == "heart"
    assert _racing_emoji_category("🐶").key == "animal"
    assert _racing_emoji_category("🍕").key == "food"
    assert _racing_emoji_category("⭐").key == "misc"
    with pytest.raises(commands.BadArgument):
        _racing_emoji_category("🟦")


def test_racing_emoji_shop_is_components_v2() -> None:
    import discord

    view = RacingEmojiShopView(command_prefix="fish ")
    assert isinstance(view, discord.ui.LayoutView)
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "## Racing Emoji shop" in text
    assert "### Custom · **1,000,000 Coins**" in text
    assert "### Sea animals · **50,000 Coins**" in text
    assert "Purchase one with `fish purchase racing-emoji <emoji>`." in text


async def test_israel_flag_purchase_charges_wallet_without_granting_badge() -> None:
    debit = AsyncMock()
    purchase = AsyncMock()
    send = AsyncMock()
    cog = Currency.__new__(Currency)
    cog.bot = cast(
        Any,
        SimpleNamespace(
            currency=SimpleNamespace(debit=debit),
            badges=SimpleNamespace(purchase_badge=purchase),
        ),
    )
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=42),
            guild=None,
            send=send,
        ),
    )

    await Currency.purchase_badge.callback(cog, ctx, "flag", "🇮🇱")

    debit.assert_awaited_once()
    assert debit.await_args.args[:3] == (42, 10_000, "badge_purchase")
    purchase.assert_not_awaited()
    assert send.await_args.args[0] == "no"


async def test_sell_badge_rejects_stat_badges() -> None:
    service = SimpleNamespace(
        owned=AsyncMock(
            return_value=[
                {
                    "badge_source": "stat",
                    "badge_key": "stat:richest",
                    "active": True,
                }
            ]
        ),
        sell_badge=AsyncMock(),
    )
    cog = Currency.__new__(Currency)
    cog.bot = cast(Any, SimpleNamespace(badges=service))
    ctx = cast(Any, SimpleNamespace(author=SimpleNamespace(id=42)))

    with pytest.raises(commands.BadArgument, match="purchasable badge"):
        await Currency.sell_badge.callback(cog, ctx, badge="richest")
    service.sell_badge.assert_not_awaited()


def test_balance_overflow_is_a_currency_error() -> None:
    assert issubclass(BalanceOverflow, Exception)


def test_work_has_separate_five_minute_text_and_app_cooldowns() -> None:
    command = Currency.work
    assert command._buckets._cooldown is not None
    assert command._buckets._cooldown.rate == 1
    assert command._buckets._cooldown.per == 300
    assert command.app_command is not None
    assert len(command.app_command.checks) == 1


async def test_work_click_records_each_click_once() -> None:
    on_click = AsyncMock()
    on_complete = AsyncMock()
    view = WorkClickView(42, on_complete, on_click=on_click)

    def interaction() -> Any:
        return SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

    for _ in range(10):
        await view._click(interaction())

    assert on_click.await_count == 10
    assert on_complete.await_args.args == (True,)
    assert view.clicks == 10


async def test_work_math_accepts_subtraction_and_retries_wrong_answers(
    monkeypatch,
) -> None:
    import extensions.currency as currency_module

    choices = iter(("math", "−"))
    numbers = iter((20, 7, 100))
    monkeypatch.setattr(currency_module.random, "choice", lambda _values: next(choices))
    monkeypatch.setattr(
        currency_module.random, "randint", lambda _low, _high: next(numbers)
    )

    author = SimpleNamespace(id=42)
    channel = SimpleNamespace(id=7)
    wrong = SimpleNamespace(
        author=author,
        channel=channel,
        content="14",
        reply=AsyncMock(),
    )
    correct = SimpleNamespace(
        author=author,
        channel=channel,
        content="13",
        reply=AsyncMock(),
    )
    bot = SimpleNamespace(
        currency=SimpleNamespace(credit=AsyncMock(return_value=Wallet(42, 13))),
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )
    ctx = cast(
        Any,
        SimpleNamespace(
            author=author,
            channel=channel,
            send=AsyncMock(),
        ),
    )
    cog = Currency.__new__(Currency)
    cog.bot = cast(Any, bot)
    cog._work_sessions = {}
    cog._work_math_tasks = {}

    await cast(Any, Currency.work.callback)(cog, ctx)

    view = ctx.send.await_args.kwargs["view"]
    assert isinstance(view, WorkMathView)
    assert view.question.startswith("Solve **20 − 7**")
    await view.submit_message(wrong)
    assert not view.finished
    await view.submit_message(correct)
    bot.currency.credit.assert_awaited_once()
    wrong.reply.assert_not_awaited()
    assert view.finished


async def test_work_math_modal_wrong_answer_is_ephemeral_only() -> None:
    import discord

    complete = AsyncMock()
    view = WorkMathView(42, "Solve **2 + 2** within 60 seconds.", 4, complete)
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(response=response)

    await view.submit_interaction(interaction, "3")

    response.send_message.assert_awaited_once()
    assert response.send_message.await_args.args == ("Incorrect!",)
    assert response.send_message.await_args.kwargs["ephemeral"] is True
    assert response.send_message.await_args.kwargs["allowed_mentions"].everyone is False
    complete.assert_not_awaited()
    assert not view.finished


async def test_work_math_timeout_keeps_equation_and_reveals_answer() -> None:
    complete = AsyncMock()
    view = WorkMathView(
        42,
        "Solve **20 − 7** within 60 seconds.",
        13,
        complete,
        source_message_id=100,
    )
    prompt = SimpleNamespace(edit=AsyncMock())
    view.message = cast(Any, prompt)

    # The command that created the task is not an answer.
    command_message = SimpleNamespace(
        id=100,
        author=SimpleNamespace(id=42),
        content="fish work",
    )
    await view.submit_message(command_message)
    assert not view.finished

    await view.on_timeout()

    prompt.edit.assert_awaited_once()
    assert "Times up!" in prompt.edit.await_args.kwargs["content"]
    assert "20 − 7" in prompt.edit.await_args.kwargs["content"]
    assert "13" in prompt.edit.await_args.kwargs["content"]
    complete.assert_awaited_once_with(False)


async def test_work_unscramble_allows_wrong_attempts_until_correct(monkeypatch) -> None:
    import extensions.currency as currency_module

    choices = iter(("unscramble", "apple"))
    monkeypatch.setattr(currency_module.random, "choice", lambda _values: next(choices))

    author = SimpleNamespace(id=42)
    channel = SimpleNamespace(id=7)
    wrong = SimpleNamespace(author=author, channel=channel, content="banana")
    correct = SimpleNamespace(author=author, channel=channel, content="APPLE")
    bot = SimpleNamespace(
        wait_for=AsyncMock(side_effect=(wrong, correct)),
        currency=SimpleNamespace(credit=AsyncMock(return_value=Wallet(42, 25))),
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )
    ctx = cast(
        Any,
        SimpleNamespace(
            author=author,
            channel=channel,
            send=AsyncMock(),
        ),
    )
    cog = Currency.__new__(Currency)
    cog.bot = cast(Any, bot)
    cog._work_sessions = {}
    cog._work_wordle_games = {}
    cog._work_math_tasks = {}

    await cast(Any, Currency.work.callback)(cog, ctx)

    assert bot.wait_for.await_count == 2
    bot.currency.credit.assert_awaited_once()
    assert bot.currency.credit.await_args.args[2] == "work_unscramble"
