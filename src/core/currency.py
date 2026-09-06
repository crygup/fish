from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

MAX_COIN_BALANCE = 9_000_000_000_000_000_000
# Wagers are reserved from the wallet immediately and can be paid out only
# once. Keep the minimum and payout guards in one place for all wager games.
# Wagers have a shared minimum.  There is no artificial maximum: a user's
# available wallet balance (and the BIGINT/payout overflow guards) is the
# effective ceiling.
MIN_WAGER_STAKE = 10
# Kept as a compatibility export for integrations that imported the former
# ceiling. A value of ``None`` documents that wagers are no longer capped.
# ``None`` means wager validation is limited by the wallet balance rather than
# an artificial global ceiling.  Individual callers may still pass a
# ``max_stake`` when a game needs one.
MAX_WAGER_STAKE: int | None = None
MAX_WAGER_PAYOUT = 1_000_000_000_000_000_000

LOTTERY_TICKET_PRICE = 100
LOTTERY_STARTING_POOL = 1_000
MAX_LOTTERY_TICKETS_PER_PURCHASE = 1_000

# ``everything`` is kept as a distinct value instead of being converted to a
# number at parse time.  Callers must confirm it and resolve it against the
# user's current wallet, which prevents a stale balance from being wagered.
EVERYTHING_AMOUNT = "everything"

# Keep this wording next to the parser so every application-command option
# advertises the same accepted input.  Slash command options must remain
# strings: Discord validates an integer option before our parser can see it,
# which would reject useful forms such as ``1k`` or ``everything``.
COIN_AMOUNT_DESCRIPTION = (
    "Coin amount (e.g. 1,000, 1k, 1.1k, 1m, 1 hundred thousand, $1, or everything)."
)
OPTIONAL_COIN_AMOUNT_DESCRIPTION = "Optional Coin amount (e.g. 1,000, 1k, 1.1k, 1m, 1 hundred thousand, $1, or everything)."


class CoinAmountError(ValueError):
    """Raised when a user supplied Coin amount cannot be parsed."""


_COIN_SUFFIXES: dict[str, Decimal] = {
    "k": Decimal(1_000),
    "thousand": Decimal(1_000),
    "m": Decimal(1_000_000),
    "mil": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
    "bil": Decimal(1_000_000_000),
    "billion": Decimal(1_000_000_000),
}
_COIN_NUMBER_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>k|thousand|m|mil|million|b|bil|billion)?$",
    re.IGNORECASE,
)
_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_SCALES: dict[str, int] = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
}


def _parse_coin_words(text: str) -> Decimal | None:
    """Parse a small human-readable number such as ``1 hundred thousand``."""

    tokens = [token for token in re.split(r"[\s-]+", text) if token]
    if not tokens:
        return None
    # Numeric prefixes are useful in the common ``1 hundred thousand`` form.
    # Word numbers are accepted as a convenience too (``one thousand``).
    current = Decimal(0)
    total = Decimal(0)
    seen = False
    for token in tokens:
        if token.isdigit() or re.fullmatch(r"\d+\.\d+", token):
            value = Decimal(token)
            current += value
            seen = True
            continue
        word_value = _NUMBER_WORDS.get(token)
        if word_value is not None:
            current += Decimal(word_value)
            seen = True
            continue
        scale = _NUMBER_SCALES.get(token)
        if scale is None:
            return None
        seen = True
        if scale == 100:
            # ``one hundred`` and ``one hundred thousand``.
            current = (current or Decimal(1)) * scale
        else:
            total += (current or Decimal(1)) * scale
            current = Decimal(0)
    return total + current if seen else None


def parse_coin_amount(value: object) -> int | str:
    """Parse a user-facing Coin amount into whole Coins.

    Supported forms include comma separated numbers, suffixes (``1k``,
    ``1.1m``, ``1 mil``), simple number words (``1 hundred thousand``), and a
    leading dollar sign.  ``everything`` is returned as
    :data:`EVERYTHING_AMOUNT` so command handlers can confirm before reading
    the caller's wallet.  This parser deliberately performs no balance or
    minimum checks; those remain operation-specific.
    """

    if isinstance(value, bool):
        raise CoinAmountError("Enter a valid whole-number Coin amount.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CoinAmountError("Enter a valid whole-number Coin amount.")
        number = Decimal(str(value))
        if number != number.to_integral_value():
            raise CoinAmountError("Coin amounts must resolve to whole Coins.")
        return int(number)

    text = str(value or "").strip().casefold()
    if not text:
        raise CoinAmountError("Enter a valid Coin amount.")
    if text in {EVERYTHING_AMOUNT, "all", "max", "maximum"}:
        return EVERYTHING_AMOUNT
    text = text.replace(",", "").replace("$", "").strip()
    match = _COIN_NUMBER_RE.fullmatch(text)
    if match:
        try:
            number = Decimal(match.group("number"))
        except InvalidOperation as error:  # pragma: no cover - regex guards it.
            raise CoinAmountError("Enter a valid Coin amount.") from error
        suffix = match.group("suffix")
        if suffix:
            number *= _COIN_SUFFIXES[suffix.casefold()]
        if number != number.to_integral_value():
            number = number.to_integral_value(rounding=ROUND_HALF_UP)
        return int(number)
    words = _parse_coin_words(text)
    if words is not None:
        if words != words.to_integral_value():
            words = words.to_integral_value(rounding=ROUND_HALF_UP)
        return int(words)
    raise CoinAmountError(
        "Enter a valid Coin amount (for example `1,000`, `1k`, or `everything`)."
    )


# Keep a descriptive alias for callers that expose the parser outside of the
# Coins category (for example wager and ticket commands).
parse_currency_amount = parse_coin_amount


def lottery_starting_pool(total_tickets: int = 0) -> int:
    """Return the seed pool for a round, scaled by ticket activity.

    A round starts at 1,000 Coins.  Every ticket adds three extra seed Coins,
    on top of its normal 100-Coin ticket contribution.  The bonus is
    intentionally uncapped so the starting contribution continues to scale
    with participation without changing ticket odds or the hourly draw.
    """

    try:
        count = max(0, int(total_tickets))
    except (TypeError, ValueError):
        count = 0
    return LOTTERY_STARTING_POOL + count * 3


ClaimType = Literal["daily", "weekly"]
WagerStatus = Literal["open", "lost", "cashed_out"]


class CurrencyError(Exception):
    """Base exception for expected wallet operations."""


class InvalidAmount(CurrencyError):
    """Raised when a coin amount is outside an operation's limits."""


class InsufficientFunds(CurrencyError):
    def __init__(self, balance: int, required: int) -> None:
        self.balance = int(balance)
        self.required = int(required)
        super().__init__(
            f"Wallet has {self.balance:,} Coins but {self.required:,} are required."
        )


class TitleAlreadyOwned(CurrencyError):
    """Raised when a user already owns an active title."""


class TitleNotOwned(CurrencyError):
    """Raised when a user selects a title they do not currently own."""


class ColorAlreadyOwned(CurrencyError):
    """Raised when a user already owns an active colour."""


class ColorNotOwned(CurrencyError):
    """Raised when a user selects a colour they do not currently own."""


class RacingEmojiAlreadyOwned(CurrencyError):
    """Raised when a user already owns an active racing emoji."""


class RacingEmojiNotOwned(CurrencyError):
    """Raised when a user selects a racing emoji they do not own."""


class RingNotOwned(CurrencyError):
    """Raised when a user selects a ring they do not own."""


class RingEquipped(CurrencyError):
    """Raised when a user tries to sell a currently equipped ring."""


class BalanceOverflow(CurrencyError):
    """Raised rather than allowing a wallet to overflow PostgreSQL BIGINT."""


class WagerNotFound(CurrencyError):
    """Raised when a wager reservation no longer exists."""


@dataclass(frozen=True, slots=True)
class Wallet:
    user_id: int
    balance: int


@dataclass(frozen=True, slots=True)
class ClaimResult:
    wallet: Wallet
    claim_type: ClaimType
    period_start: date
    claimed: bool
    base_amount: int
    bonus_amount: int
    streak: int = 0
    streak_bonus_amount: int = 0

    @property
    def amount(self) -> int:
        return self.base_amount + self.bonus_amount + self.streak_bonus_amount


@dataclass(frozen=True, slots=True)
class Wager:
    id: int
    user_id: int
    source: str
    stake: int
    status: WagerStatus
    payout: int


@dataclass(frozen=True, slots=True)
class WagerSettlement:
    wager: Wager
    settled: bool
    balance: int | None = None


@dataclass(frozen=True, slots=True)
class CurrencyTransfer:
    """Result of an atomic user-to-user Coins transfer.

    Both wallet updates and their corresponding ledger entries are committed
    in one database transaction, so callers never need to compensate for a
    debit that succeeded without a matching credit (or vice versa).
    """

    sender: Wallet
    recipient: Wallet
    amount: int


@dataclass(frozen=True, slots=True)
class LotteryTicket:
    """One ticket in an open lottery round."""

    id: int
    user_id: int
    ticket_digits: str


@dataclass(frozen=True, slots=True)
class LotteryStatus:
    """Current hourly lottery information for display."""

    round_start: datetime
    prize_pool: int
    total_tickets: int = 0
    user_tickets: int = 0

    @property
    def chance_percent(self) -> float:
        if self.total_tickets <= 0:
            return 0.0
        return self.user_tickets / self.total_tickets * 100


@dataclass(frozen=True, slots=True)
class LotteryPurchase:
    """Result of atomically buying tickets and reserving their Coins."""

    round_start: datetime
    tickets: tuple[str, ...]
    prize_pool: int
    wallet_balance: int


@dataclass(frozen=True, slots=True)
class LotteryDraw:
    """Committed result of one hourly lottery draw."""

    round_start: datetime
    prize_pool: int
    tickets: tuple[LotteryTicket, ...]
    winning_ticket: LotteryTicket | None
    drawn: bool


@dataclass(frozen=True, slots=True)
class TitleSale:
    """Result of selling one owned title."""

    title_key: str
    text: str
    refund_amount: int
    wallet_balance: int

    def __getitem__(self, key: str) -> Any:
        if key == "wallet_balance":
            return self.wallet_balance
        if key == "refund_amount":
            return self.refund_amount
        if key == "title_key":
            return self.title_key
        if key == "text":
            return self.text
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class ColorSale:
    """Result of selling one owned colour."""

    color_key: str
    hex_value: str
    refund_amount: int
    wallet_balance: int

    def __getitem__(self, key: str) -> Any:
        if key == "wallet_balance":
            return self.wallet_balance
        if key == "refund_amount":
            return self.refund_amount
        if key == "color_key":
            return self.color_key
        if key == "hex_value":
            return self.hex_value
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class RacingEmojiSale:
    """Result of selling one owned racing emoji."""

    emoji_key: str
    display: str
    refund_amount: int
    wallet_balance: int

    def __getitem__(self, key: str) -> Any:
        if key == "emoji_key":
            return self.emoji_key
        if key == "display":
            return self.display
        if key == "refund_amount":
            return self.refund_amount
        if key == "wallet_balance":
            return self.wallet_balance
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class RingTransfer:
    """One ring unit transferred between two inventories."""

    sender_id: int
    recipient_id: int
    ring_key: str
    recipient_quantity: int
    recipient_equipped_count: int


@dataclass(frozen=True, slots=True)
class RingSale:
    """Result of selling one owned ring unit."""

    ring_key: str
    display: str
    refund_amount: int
    wallet_balance: int
    remaining_quantity: int

    def __getitem__(self, key: str) -> Any:
        if key == "ring_key":
            return self.ring_key
        if key == "display":
            return self.display
        if key == "refund_amount":
            return self.refund_amount
        if key == "wallet_balance":
            return self.wallet_balance
        if key in {"remaining_quantity", "quantity"}:
            return self.remaining_quantity
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class GamblingStats:
    """Durable totals for a user's currency wagers.

    ``total_wagered`` is the amount reserved from the wallet, while
    ``total_earned`` is the amount paid back on settled winning wagers and
    ``total_lost`` is the stake from settled losing wagers.  Keeping both the
    amounts and outcome counts lets callers calculate win rates and net
    returns later without changing the wager history or exposing anything in
    the current commands.
    """

    user_id: int
    total_wagered: int = 0
    total_earned: int = 0
    total_lost: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def wagers(self) -> int:
        return self.wins + self.losses

    @property
    def win_percentage(self) -> float:
        return (self.wins / self.wagers * 100) if self.wagers else 0.0

    @property
    def net(self) -> int:
        return self.total_earned - self.total_lost


@dataclass(frozen=True, slots=True)
class CurrencyEarningSource:
    """Positive and negative wallet activity grouped by transaction source."""

    source: str
    earned: int = 0
    lost: int = 0

    @property
    def net(self) -> int:
        return self.earned - self.lost


@dataclass(frozen=True, slots=True)
class CurrencyEarnings:
    """A user's complete Coins earnings summary."""

    user_id: int
    earned: int = 0
    lost: int = 0
    sources: tuple[CurrencyEarningSource, ...] = ()

    @property
    def net(self) -> int:
        return self.earned - self.lost


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def lottery_period_start(now: datetime | None = None) -> datetime:
    """Return the current UTC hour used by the hourly lottery."""

    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def claim_period_start(claim_type: ClaimType, now: datetime | None = None) -> date:
    """Return the UTC calendar bucket for a recurring currency claim."""

    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    day = current.astimezone(timezone.utc).date()
    if claim_type == "daily":
        return day
    if claim_type == "weekly":
        # Reputation weeks and currency weeks both reset on Sunday at 00:00 UTC.
        return day - timedelta(days=(day.weekday() + 1) % 7)
    raise ValueError(f"Unknown claim type: {claim_type}")


def next_claim_reset(claim_type: ClaimType, now: datetime | None = None) -> datetime:
    current = now or utc_now()
    period = claim_period_start(claim_type, current)
    days = 1 if claim_type == "daily" else 7
    return datetime.combine(
        period + timedelta(days=days), datetime.min.time(), tzinfo=timezone.utc
    )


def _wallet(row: Any) -> Wallet:
    return Wallet(user_id=int(row["user_id"]), balance=int(row["balance"]))


def _wager(row: Any) -> Wager:
    return Wager(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        source=str(row["source"]),
        stake=int(row["stake"]),
        status=row["status"],
        payout=int(row["payout"]),
    )


def _gambling_stats(row: Any) -> GamblingStats:
    return GamblingStats(
        user_id=int(row["user_id"]),
        total_wagered=int(row["total_wagered"]),
        total_earned=int(row["total_earned"]),
        total_lost=int(row["total_lost"]),
        wins=int(row["wins"]),
        losses=int(row["losses"]),
    )


class CurrencyService:
    """Atomic persistence operations for Fishie's global Coins economy.

    Every balance-changing operation first locks the user's wallet row.  This
    makes simultaneous commands, game rewards, and wager settlements serialize
    without relying on an in-process lock that would be lost across bot workers.
    """

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self._click_reward_flusher: Callable[[int | None], Awaitable[None]] | None = (
            None
        )
        self._click_reward_flush_lock = asyncio.Lock()

    def set_click_reward_flusher(
        self, flusher: Callable[[int | None], Awaitable[None]] | None
    ) -> None:
        """Register the in-process click reward cache, when the Fun cog loads.

        The hook is optional so the service remains usable in migrations, tests,
        and workers that do not load the Fun extension.  A lock serializes
        concurrent wallet operations so each request sees its own pending
        click rewards.
        """

        self._click_reward_flusher = flusher

    async def _flush_click_rewards(self, user_id: int | None = None) -> None:
        flusher = self._click_reward_flusher
        if flusher is None:
            return
        # Serialize per-command flushes.  A single global "active" boolean
        # would let a concurrent wallet request skip its own pending rewards
        # while another user's flush was in progress.
        async with self._click_reward_flush_lock:
            flusher = self._click_reward_flusher
            if flusher is not None:
                await flusher(user_id)

    @staticmethod
    async def _locked_wallet(connection: Any, user_id: int) -> Wallet:
        await connection.execute(
            """
            INSERT INTO currency_wallets(user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
        )
        row = await connection.fetchrow(
            """
            SELECT user_id, balance
            FROM currency_wallets
            WHERE user_id = $1
            FOR UPDATE
            """,
            user_id,
        )
        if row is None:  # pragma: no cover - the INSERT makes this unreachable.
            raise RuntimeError("Failed to create currency wallet")
        return _wallet(row)

    async def get_wallet(self, user_id: int) -> Wallet:
        """Get a wallet, creating an empty one when it does not exist."""

        await self._flush_click_rewards(int(user_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                return await self._locked_wallet(connection, int(user_id))

    async def purchase_lottery_tickets(
        self,
        user_id: int,
        amount: int = 1,
        *,
        now: datetime | None = None,
    ) -> LotteryPurchase:
        """Buy unique tickets for the current hourly lottery round.

        Ticket creation, the wallet debit, and the prize-pool increase are one
        transaction.  This keeps a concurrent purchase from observing a
        partially-created set of tickets or spending Coins without receiving
        tickets.
        """

        user_id = int(user_id)
        await self._flush_click_rewards(user_id)
        if not 1 <= int(amount) <= MAX_LOTTERY_TICKETS_PER_PURCHASE:
            raise InvalidAmount(
                "You can purchase between 1 and "
                f"{MAX_LOTTERY_TICKETS_PER_PURCHASE:,} tickets at a time."
            )
        amount = int(amount)
        cost = amount * LOTTERY_TICKET_PRICE
        period = lottery_period_start(now)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, user_id)
                if wallet.balance < cost:
                    raise InsufficientFunds(wallet.balance, cost)
                await connection.execute(
                    """
                    INSERT INTO lottery_rounds(round_start, prize_pool)
                    VALUES ($1, $2)
                    ON CONFLICT (round_start) DO NOTHING
                    """,
                    period,
                    lottery_starting_pool(),
                )
                round_row = await connection.fetchrow(
                    """
                    SELECT round_start, prize_pool, status
                    FROM lottery_rounds
                    WHERE round_start = $1
                    FOR UPDATE
                    """,
                    period,
                )
                if round_row is None or str(round_row["status"]) != "open":
                    raise InvalidAmount("This lottery round has already closed.")
                used_rows = await connection.fetch(
                    """
                    SELECT ticket_digits
                    FROM lottery_tickets
                    WHERE round_start = $1
                    """,
                    period,
                )
                used = {str(row["ticket_digits"]) for row in used_rows}
                previous_ticket_count = len(used)
                pool_bonus = lottery_starting_pool(previous_ticket_count + amount) - (
                    lottery_starting_pool(previous_ticket_count)
                )
                pool_increment = cost + pool_bonus
                digits: list[str] = []
                while len(digits) < amount:
                    candidate = f"{secrets.randbelow(1_000_000):06d}"
                    if candidate in used:
                        continue
                    used.add(candidate)
                    digits.append(candidate)
                for ticket_digits in digits:
                    await connection.execute(
                        """
                        INSERT INTO lottery_tickets(round_start, user_id, ticket_digits)
                        VALUES ($1, $2, $3)
                        """,
                        period,
                        user_id,
                        ticket_digits,
                    )
                updated_round = await connection.fetchrow(
                    """
                    UPDATE lottery_rounds
                    SET prize_pool = prize_pool + $2
                    WHERE round_start = $1
                      AND status = 'open'
                      AND prize_pool <= $3
                    RETURNING prize_pool
                    """,
                    period,
                    pool_increment,
                    MAX_COIN_BALANCE - pool_increment,
                )
                if updated_round is None:
                    raise BalanceOverflow("This lottery prize pool is too large.")
                wallet_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1 AND balance >= $2
                    RETURNING user_id, balance
                    """,
                    user_id,
                    cost,
                )
                if wallet_row is None:
                    raise InsufficientFunds(wallet.balance, cost)
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(user_id, amount, source)
                    VALUES ($1, $2, 'lottery_ticket')
                    """,
                    user_id,
                    -cost,
                )
                return LotteryPurchase(
                    round_start=period,
                    tickets=tuple(digits),
                    prize_pool=int(updated_round["prize_pool"]),
                    wallet_balance=int(wallet_row["balance"]),
                )

    async def get_lottery_status(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> LotteryStatus:
        """Return the current round's pool, ticket counts, and chance."""

        period = lottery_period_start(now)
        row = await self.pool.fetchrow(
            """
            SELECT prize_pool
            FROM lottery_rounds
            WHERE round_start = $1 AND status = 'open'
            """,
            period,
        )
        total = await self.pool.fetchval(
            """
            SELECT COUNT(*)
            FROM lottery_tickets
            WHERE round_start = $1
            """,
            period,
        )
        user_total = await self.pool.fetchval(
            """
            SELECT COUNT(*)
            FROM lottery_tickets
            WHERE round_start = $1 AND user_id = $2
            """,
            period,
            int(user_id),
        )
        return LotteryStatus(
            round_start=period,
            prize_pool=(
                int(row["prize_pool"])
                if row is not None
                else lottery_starting_pool(int(total or 0))
            ),
            total_tickets=int(total or 0),
            user_tickets=int(user_total or 0),
        )

    async def get_lottery_ticket_count(self, user_id: int) -> int:
        """Return the user's active tickets in the current round."""

        return int(
            await self.pool.fetchval(
                """
                SELECT COUNT(*)
                FROM lottery_tickets
                WHERE round_start = $1 AND user_id = $2
                """,
                lottery_period_start(),
                int(user_id),
            )
            or 0
        )

    async def draw_lottery_round(
        self,
        round_start: datetime,
    ) -> LotteryDraw | None:
        """Draw and settle one round exactly once under a row lock."""

        period = lottery_period_start(round_start)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                round_row = await connection.fetchrow(
                    """
                    SELECT round_start, prize_pool, status
                    FROM lottery_rounds
                    WHERE round_start = $1
                    FOR UPDATE
                    """,
                    period,
                )
                if round_row is None or str(round_row["status"]) != "open":
                    return None
                ticket_rows = await connection.fetch(
                    """
                    SELECT id, user_id, ticket_digits
                    FROM lottery_tickets
                    WHERE round_start = $1
                    ORDER BY id
                    """,
                    period,
                )
                tickets = tuple(
                    LotteryTicket(
                        id=int(row["id"]),
                        user_id=int(row["user_id"]),
                        ticket_digits=str(row["ticket_digits"]),
                    )
                    for row in ticket_rows
                )
                if not tickets:
                    await connection.execute(
                        """
                        UPDATE lottery_rounds
                        SET status = 'no_winner', drawn_at = now()
                        WHERE round_start = $1 AND status = 'open'
                        """,
                        period,
                    )
                    return LotteryDraw(
                        round_start=period,
                        prize_pool=int(round_row["prize_pool"]),
                        tickets=(),
                        winning_ticket=None,
                        drawn=True,
                    )
                winning_ticket = secrets.choice(tickets)
                prize_pool = int(round_row["prize_pool"])
                wallet = await self._locked_wallet(connection, winning_ticket.user_id)
                if prize_pool > MAX_COIN_BALANCE - wallet.balance:
                    raise BalanceOverflow(
                        "This lottery prize would exceed the wallet limit."
                    )
                wallet_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance + $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING balance
                    """,
                    winning_ticket.user_id,
                    prize_pool,
                )
                if wallet_row is None:  # pragma: no cover - locked wallet exists.
                    raise RuntimeError("Failed to credit the lottery winner")
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, 'lottery_win', $3)
                    """,
                    winning_ticket.user_id,
                    prize_pool,
                    f"lottery:{period.isoformat()}",
                )
                await connection.execute(
                    """
                    UPDATE lottery_rounds
                    SET status = 'drawn', winning_ticket = $2,
                        winner_user_id = $3, drawn_at = now()
                    WHERE round_start = $1 AND status = 'open'
                    """,
                    period,
                    winning_ticket.ticket_digits,
                    winning_ticket.user_id,
                )
                await connection.execute(
                    "DELETE FROM lottery_tickets WHERE round_start = $1", period
                )
                return LotteryDraw(
                    round_start=period,
                    prize_pool=prize_pool,
                    tickets=tickets,
                    winning_ticket=winning_ticket,
                    drawn=True,
                )

    async def get_profile_description(self, user_id: int) -> str | None:
        """Return the user's saved profile description, if they have one."""

        value = await self.pool.fetchval(
            "SELECT description FROM user_profiles WHERE user_id = $1",
            int(user_id),
        )
        text = str(value or "").strip()
        return text or None

    async def set_profile_description(
        self, user_id: int, description: str | None
    ) -> str | None:
        """Create, replace, or clear the user's profile description.

        Keeping this operation in the currency/profile service means both the
        text command and the nested profile app command use the same length and
        trimming rules.  An empty value removes the row instead of storing a
        meaningless blank description.
        """

        text = str(description or "").strip()
        if len(text) > 500:
            raise ValueError("Profile descriptions must be 500 characters or fewer.")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                if not text:
                    await connection.execute(
                        "DELETE FROM user_profiles WHERE user_id = $1",
                        int(user_id),
                    )
                    return None
                await connection.execute(
                    """
                    INSERT INTO user_profiles(user_id, description, updated_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (user_id) DO UPDATE
                    SET description = EXCLUDED.description,
                        updated_at = now()
                    """,
                    int(user_id),
                    text,
                )
        return text

    async def get_gambling_stats(self, user_id: int) -> GamblingStats:
        """Return persisted wagering totals without creating a wallet.

        The stats table is intentionally separate from transaction history so
        reporting can remain cheap as the transaction log grows.  Users who
        have never wagered simply receive an all-zero result.
        """

        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT user_id, total_wagered, total_earned, total_lost,
                       wins, losses
                FROM currency_gambling_stats
                WHERE user_id = $1
                """,
                int(user_id),
            )
        if row is None:
            return GamblingStats(user_id=int(user_id))
        return _gambling_stats(row)

    async def get_total_game_losses(self) -> int:
        """Return the total Coins lost in settled games across all users.

        This is a house-facing display metric, rather than a wallet balance.
        It intentionally reads only game outcome tables: regular wager losses
        are kept in ``currency_gambling_stats`` while the race and Lucky Roll
        games maintain their own loss totals.  Purchases, sales, developer
        adjustments, transfers, and other wallet activity are therefore not
        included.
        """

        row = await self.pool.fetchrow("""
            SELECT
                COALESCE((
                    SELECT SUM(total_lost)
                    FROM currency_gambling_stats
                ), 0)
                + COALESCE((
                    SELECT SUM(coins_lost)
                    FROM sea_animal_race_stats
                ), 0)
                + COALESCE((
                    SELECT SUM(coins_lost)
                    FROM lucky_roll_stats
                ), 0) AS total_lost
            """)
        return int(row["total_lost"] or 0) if row is not None else 0

    async def get_earnings(self, user_id: int) -> CurrencyEarnings:
        """Return all recorded Coins earnings and losses for a user.

        The transaction ledger is the source of truth here rather than the
        wallet balance: purchases, wagers, refunds, claims, and game rewards
        can all change a balance, and grouping by ``source`` lets the command
        explain where those changes came from.  Wager stakes are escrowed
        before a game starts, so they are deliberately omitted from the
        earnings view until the wager settles.  A settled wager contributes
        only its net result (payout minus stake): a 100-Coin stake that pays
        150 is +50 earned, while a losing 100-Coin stake is 100 lost.  This
        keeps the earnings view from presenting the same stake as both a loss
        and part of a later payout.

        Race and Lucky Roll use their older debit/credit pair rather than the
        durable ``currency_wagers`` table.  Their wager, refund, and win rows
        are therefore folded into one net result per game as well.
        """

        user_id = int(user_id)
        await self._flush_click_rewards(user_id)
        rows = await self.pool.fetch(
            """
            WITH direct_game_net AS (
                SELECT
                    CASE
                        WHEN source LIKE 'sea_animal_race_%'
                            THEN 'sea_animal_race_net'
                        WHEN source LIKE 'luckyroll_%'
                            THEN 'luckyroll_net'
                    END AS source,
                    COALESCE(SUM(amount), 0) AS net
                FROM currency_transactions
                WHERE user_id = $1
                  AND (
                      source LIKE 'sea_animal_race_%'
                      OR source LIKE 'luckyroll_%'
                  )
                GROUP BY 1
            ),
            direct_game AS (
                SELECT
                    source,
                    GREATEST(net, 0)::BIGINT AS earned,
                    GREATEST(-net, 0)::BIGINT AS lost
                FROM direct_game_net
            ),
            settled_wagers AS (
                SELECT
                    'wager_cashout:' || source AS source,
                    COALESCE(SUM(GREATEST(payout - stake, 0)), 0)::BIGINT
                        AS earned,
                    COALESCE(SUM(GREATEST(stake - payout, 0)), 0)::BIGINT
                        AS lost
                FROM currency_wagers
                WHERE user_id = $1
                  AND status <> 'open'
                GROUP BY source
            ),
            regular AS (
                SELECT
                    source,
                    COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0)::BIGINT
                        AS earned,
                    COALESCE(SUM(-amount) FILTER (WHERE amount < 0), 0)::BIGINT
                        AS lost
                FROM currency_transactions
                WHERE user_id = $1
                  AND source NOT LIKE 'wager_stake:%'
                  AND source NOT LIKE 'wager_cashout:%'
                  AND source NOT LIKE 'sea_animal_race_%'
                  AND source NOT LIKE 'luckyroll_%'
                GROUP BY source
            ),
            combined AS (
                SELECT source, earned, lost FROM regular
                UNION ALL
                SELECT source, earned, lost FROM settled_wagers
                UNION ALL
                SELECT source, earned, lost FROM direct_game
            )
            SELECT
                source,
                SUM(earned)::BIGINT AS earned,
                SUM(lost)::BIGINT AS lost
            FROM combined
            GROUP BY source
            HAVING SUM(earned) <> 0 OR SUM(lost) <> 0
            ORDER BY (SUM(earned) + SUM(lost)) DESC, source ASC
            """,
            user_id,
        )
        earned = sum(int(row["earned"] or 0) for row in rows)
        lost = sum(int(row["lost"] or 0) for row in rows)
        return CurrencyEarnings(
            user_id=user_id,
            earned=earned,
            lost=lost,
            sources=tuple(
                CurrencyEarningSource(
                    source=str(row["source"]),
                    earned=int(row["earned"] or 0),
                    lost=int(row["lost"] or 0),
                )
                for row in rows
            ),
        )

    async def award_birthday(self, user_id: int) -> bool:
        """Credit a birthday once per calendar year elapsed, atomically."""
        await self._flush_click_rewards(int(user_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                # The wallet lock serializes awards across both bot instances.
                eligible = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM user_birthdays b
                        LEFT JOIN birthday_rewards r USING (user_id)
                        WHERE b.user_id = $1
                          AND b.month = EXTRACT(MONTH FROM now() AT TIME ZONE 'UTC')
                          AND b.day = EXTRACT(DAY FROM now() AT TIME ZONE 'UTC')
                          AND (r.last_awarded_on IS NULL OR
                               r.last_awarded_on + INTERVAL '1 year' <=
                               (now() AT TIME ZONE 'UTC')::date)
                    )
                    """,
                    user_id,
                )
                if not eligible:
                    return False
                if 50_000 > MAX_COIN_BALANCE - wallet.balance:
                    raise BalanceOverflow("This credit would exceed the wallet limit.")
                await connection.execute(
                    "UPDATE currency_wallets SET balance = balance + 50000, updated_at = now() WHERE user_id = $1",
                    user_id,
                )
                await connection.execute(
                    "INSERT INTO currency_transactions (user_id, amount, source) VALUES ($1, 50000, 'birthday')",
                    user_id,
                )
                await connection.execute(
                    """
                    INSERT INTO birthday_rewards (user_id, last_awarded_on)
                    VALUES ($1, (now() AT TIME ZONE 'UTC')::date)
                    ON CONFLICT (user_id) DO UPDATE SET last_awarded_on = EXCLUDED.last_awarded_on
                    """,
                    user_id,
                )
                return True

    async def credit(
        self,
        user_id: int,
        amount: int,
        source: str,
        *,
        reference_key: str | None = None,
    ) -> Wallet:
        await self._flush_click_rewards(int(user_id))
        if amount <= 0:
            raise InvalidAmount("A credit must be at least 1 Coin.")
        if not source:
            raise ValueError("Currency transaction source cannot be empty")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                if amount > MAX_COIN_BALANCE - wallet.balance:
                    raise BalanceOverflow("This credit would exceed the wallet limit.")
                row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance + $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    user_id,
                    amount,
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, $3, $4)
                    """,
                    user_id,
                    amount,
                    source,
                    reference_key,
                )
                return _wallet(row)

    async def debit(
        self,
        user_id: int,
        amount: int,
        source: str,
        *,
        reference_key: str | None = None,
    ) -> Wallet:
        await self._flush_click_rewards(int(user_id))
        if amount <= 0:
            raise InvalidAmount("A debit must be at least 1 Coin.")
        if not source:
            raise ValueError("Currency transaction source cannot be empty")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                if wallet.balance < amount:
                    raise InsufficientFunds(wallet.balance, amount)
                row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    user_id,
                    amount,
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, $3, $4)
                    """,
                    user_id,
                    -amount,
                    source,
                    reference_key,
                )
                return _wallet(row)

    async def transfer(
        self,
        sender_id: int,
        recipient_id: int,
        amount: int,
        *,
        source: str = "give",
        reference_key: str | None = None,
    ) -> CurrencyTransfer:
        """Move Coins between two users atomically.

        The wallet rows are locked in ID order.  That ordering is important
        because a transfer from A to B can run concurrently with a transfer
        from B to A; acquiring locks in caller order would allow those two
        requests to deadlock.  The sender is debited and the recipient is
        credited before either ledger entry is committed.
        """

        sender_id = int(sender_id)
        recipient_id = int(recipient_id)
        amount = int(amount)
        if sender_id == recipient_id:
            raise InvalidAmount("You cannot give Coins to yourself.")
        if amount <= 0:
            raise InvalidAmount("You must give at least 1 Coin.")
        if not source:
            raise ValueError("Currency transaction source cannot be empty")

        # Click rewards are held in-process and must be visible before we
        # validate either balance.  Flush each distinct account once before
        # taking database row locks.
        await self._flush_click_rewards(sender_id)
        await self._flush_click_rewards(recipient_id)
        reference = reference_key or f"transfer:{secrets.token_hex(12)}"
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallets: dict[int, Wallet] = {}
                for user_id in sorted((sender_id, recipient_id)):
                    wallets[user_id] = await self._locked_wallet(connection, user_id)
                sender = wallets[sender_id]
                recipient = wallets[recipient_id]
                if sender.balance < amount:
                    raise InsufficientFunds(sender.balance, amount)
                if amount > MAX_COIN_BALANCE - recipient.balance:
                    raise BalanceOverflow(
                        "This transfer would exceed the recipient's wallet limit."
                    )

                sender_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1 AND balance >= $2
                    RETURNING user_id, balance
                    """,
                    sender_id,
                    amount,
                )
                if sender_row is None:
                    # The sender row was locked above, so this is defensive;
                    # retain the same public exception as debit().
                    raise InsufficientFunds(sender.balance, amount)
                recipient_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance + $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    recipient_id,
                    amount,
                )
                if recipient_row is None:  # pragma: no cover - locked row exists.
                    raise RuntimeError("Failed to update recipient currency wallet")
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, $3, $4), ($5, $6, $7, $4)
                    """,
                    sender_id,
                    -amount,
                    f"{source}_sent",
                    reference,
                    recipient_id,
                    amount,
                    f"{source}_received",
                )
                return CurrencyTransfer(
                    sender=_wallet(sender_row),
                    recipient=_wallet(recipient_row),
                    amount=amount,
                )

    async def purchase_title(
        self,
        user_id: int,
        title_key: str,
        text: str,
        price: int,
    ) -> Wallet:
        """Debit Coins and grant a title in one transaction."""

        await self._flush_click_rewards(int(user_id))
        title_key = str(title_key).strip()
        text = str(text).strip()
        if not title_key or not text:
            raise ValueError("A title key and display text are required.")
        if price <= 0:
            raise InvalidAmount("A title price must be positive.")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                existing = await connection.fetchrow(
                    """
                    SELECT active
                    FROM user_titles
                    WHERE user_id = $1 AND title_key = $2
                    FOR UPDATE
                    """,
                    int(user_id),
                    title_key,
                )
                if existing is not None and bool(existing["active"]):
                    raise TitleAlreadyOwned(title_key)
                if wallet.balance < price:
                    raise InsufficientFunds(wallet.balance, price)
                has_equipped = bool(
                    await connection.fetchval(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM user_titles
                            WHERE user_id = $1 AND active AND equipped
                        )
                        """,
                        int(user_id),
                    )
                )
                row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    int(user_id),
                    int(price),
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, 'title_purchase', $3)
                    """,
                    int(user_id),
                    -int(price),
                    f"title:{int(user_id)}:{title_key}",
                )
                await connection.execute(
                    """
                    INSERT INTO user_titles(
                        user_id, title_key, text, purchase_price, active, equipped
                    ) VALUES ($1, $2, $3, $4, TRUE, $5)
                    ON CONFLICT (user_id, title_key) DO UPDATE SET
                        text = EXCLUDED.text,
                        purchase_price = EXCLUDED.purchase_price,
                        active = TRUE,
                        equipped = CASE
                            WHEN user_titles.equipped THEN TRUE
                            ELSE EXCLUDED.equipped
                        END,
                        purchased_at = now()
                    """,
                    int(user_id),
                    title_key,
                    text,
                    int(price),
                    not has_equipped,
                )
                return _wallet(row)

    async def owned_titles(self, user_id: int) -> list[Any]:
        """Return all active titles owned by a user, newest first."""

        return list(
            await self.pool.fetch(
                """
                SELECT user_id, title_key, text, purchase_price, active,
                       equipped, purchased_at
                FROM user_titles
                WHERE user_id = $1 AND active
                ORDER BY purchased_at DESC, title_key ASC
                """,
                int(user_id),
            )
        )

    async def equip_title(self, user_id: int, title_key: str) -> Any:
        """Equip one owned title and unequip the user's previous title."""

        await self._flush_click_rewards(int(user_id))
        title_key = str(title_key).strip()
        if not title_key:
            raise TitleNotOwned(title_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                title = await connection.fetchrow(
                    """
                    SELECT user_id, title_key, text, purchase_price, active,
                           equipped, purchased_at
                    FROM user_titles
                    WHERE user_id = $1 AND title_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    title_key,
                )
                if title is None:
                    raise TitleNotOwned(title_key)
                await connection.execute(
                    """
                    UPDATE user_titles
                    SET equipped = FALSE
                    WHERE user_id = $1 AND active AND equipped
                    """,
                    int(user_id),
                )
                equipped = await connection.fetchrow(
                    """
                    UPDATE user_titles
                    SET equipped = TRUE
                    WHERE user_id = $1 AND title_key = $2 AND active
                    RETURNING user_id, title_key, text, purchase_price, active,
                              equipped, purchased_at
                    """,
                    int(user_id),
                    title_key,
                )
                if equipped is None:  # pragma: no cover - row is locked above.
                    raise TitleNotOwned(title_key)
                return equipped

    async def unequip_title(self, user_id: int) -> bool:
        """Unequip the user's current title, if one is equipped."""

        await self._flush_click_rewards(int(user_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE user_titles
                    SET equipped = FALSE
                    WHERE user_id = $1 AND active AND equipped
                    RETURNING title_key
                    """,
                    int(user_id),
                )
                return row is not None

    async def sell_title(self, user_id: int, title_key: str) -> TitleSale:
        """Deactivate one owned title and refund half its purchase price."""

        await self._flush_click_rewards(int(user_id))
        title_key = str(title_key).strip()
        if not title_key:
            raise TitleNotOwned(title_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                title = await connection.fetchrow(
                    """
                    SELECT title_key, text, purchase_price, active, equipped
                    FROM user_titles
                    WHERE user_id = $1 AND title_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    title_key,
                )
                if title is None:
                    raise TitleNotOwned(title_key)
                purchase_price = int(title["purchase_price"])
                refund_amount = purchase_price // 2
                updated = await connection.fetchrow(
                    """
                    UPDATE user_titles
                    SET active = FALSE, equipped = FALSE
                    WHERE user_id = $1 AND title_key = $2 AND active
                    RETURNING title_key, text, purchase_price
                    """,
                    int(user_id),
                    title_key,
                )
                if updated is None:  # pragma: no cover - row is locked above.
                    raise TitleNotOwned(title_key)
                wallet = await self._locked_wallet(connection, int(user_id))
                if refund_amount:
                    if refund_amount > MAX_COIN_BALANCE - wallet.balance:
                        raise BalanceOverflow(
                            "This refund would exceed the wallet limit."
                        )
                    wallet_row = await connection.fetchrow(
                        """
                        UPDATE currency_wallets
                        SET balance = balance + $2, updated_at = now()
                        WHERE user_id = $1
                        RETURNING user_id, balance
                        """,
                        int(user_id),
                        refund_amount,
                    )
                    if wallet_row is None:  # pragma: no cover - UPDATE is valid.
                        raise BalanceOverflow("This refund could not be applied.")
                    wallet = _wallet(wallet_row)
                    await connection.execute(
                        """
                        INSERT INTO currency_transactions(
                            user_id, amount, source, reference_key
                        ) VALUES ($1, $2, 'title_sale', $3)
                        """,
                        int(user_id),
                        refund_amount,
                        f"title-sale:{int(user_id)}:{title_key}",
                    )
                return TitleSale(
                    title_key=str(updated["title_key"]),
                    text=str(updated["text"]),
                    refund_amount=refund_amount,
                    wallet_balance=wallet.balance,
                )

    async def purchase_color(
        self,
        user_id: int,
        color_key: str,
        hex_value: str,
        price: int,
    ) -> Wallet:
        """Debit Coins and grant a profile colour atomically.

        ``color_key`` is a stable catalog key (``normal``, ``white``,
        ``black``, or ``custom``); the resolved hex value is stored on the
        ownership row so custom colours remain stable if catalog defaults are
        later changed.
        """

        await self._flush_click_rewards(int(user_id))
        color_key = str(color_key).strip()
        hex_value = str(hex_value).strip().upper()
        if not color_key or not hex_value:
            raise ValueError("A colour key and hex value are required.")
        if price <= 0:
            raise InvalidAmount("A colour price must be positive.")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                existing = await connection.fetchrow(
                    """
                    SELECT active
                    FROM user_colors
                    WHERE user_id = $1 AND color_key = $2
                    FOR UPDATE
                    """,
                    int(user_id),
                    color_key,
                )
                if existing is not None and bool(existing["active"]):
                    raise ColorAlreadyOwned(color_key)
                if wallet.balance < price:
                    raise InsufficientFunds(wallet.balance, price)
                has_equipped = bool(
                    await connection.fetchval(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM user_colors
                            WHERE user_id = $1 AND active AND equipped
                        )
                        """,
                        int(user_id),
                    )
                )
                wallet_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    int(user_id),
                    int(price),
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, 'color_purchase', $3)
                    """,
                    int(user_id),
                    -int(price),
                    f"color:{int(user_id)}:{color_key}",
                )
                await connection.execute(
                    """
                    INSERT INTO user_colors(
                        user_id, color_key, hex_value, purchase_price,
                        active, equipped
                    ) VALUES ($1, $2, $3, $4, TRUE, $5)
                    ON CONFLICT (user_id, color_key) DO UPDATE SET
                        hex_value = EXCLUDED.hex_value,
                        purchase_price = EXCLUDED.purchase_price,
                        active = TRUE,
                        equipped = CASE
                            WHEN user_colors.equipped THEN TRUE
                            ELSE EXCLUDED.equipped
                        END,
                        purchased_at = now()
                    """,
                    int(user_id),
                    color_key,
                    hex_value,
                    int(price),
                    not has_equipped,
                )
                return _wallet(wallet_row)

    async def purchase_custom_color(
        self, user_id: int, hex_value: str, price: int = 25_000
    ) -> Wallet:
        """Purchase the user's custom colour using the catalog custom key."""

        return await self.purchase_color(user_id, "custom", hex_value, price)

    async def owned_colors(self, user_id: int) -> list[Any]:
        """Return all active colours owned by a user, newest first."""

        return list(
            await self.pool.fetch(
                """
                SELECT user_id, color_key, hex_value, purchase_price,
                       active, equipped, purchased_at
                FROM user_colors
                WHERE user_id = $1 AND active
                ORDER BY purchased_at DESC, color_key ASC
                """,
                int(user_id),
            )
        )

    async def equip_color(
        self, user_id: int, color_key: str, hex_value: str | None = None
    ) -> Any:
        """Equip one owned colour and optionally update a custom colour."""

        await self._flush_click_rewards(int(user_id))
        color_key = str(color_key).strip()
        if not color_key:
            raise ColorNotOwned(color_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                color = await connection.fetchrow(
                    """
                    SELECT user_id, color_key, hex_value, purchase_price,
                           active, equipped, purchased_at
                    FROM user_colors
                    WHERE user_id = $1 AND color_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    color_key,
                )
                if color is None:
                    raise ColorNotOwned(color_key)
                if hex_value is not None:
                    hex_value = str(hex_value).strip().upper()
                    await connection.execute(
                        """
                        UPDATE user_colors
                        SET hex_value = $3
                        WHERE user_id = $1 AND color_key = $2 AND active
                        """,
                        int(user_id),
                        color_key,
                        hex_value,
                    )
                await connection.execute(
                    """
                    UPDATE user_colors
                    SET equipped = FALSE
                    WHERE user_id = $1 AND active AND equipped
                    """,
                    int(user_id),
                )
                equipped = await connection.fetchrow(
                    """
                    UPDATE user_colors
                    SET equipped = TRUE
                    WHERE user_id = $1 AND color_key = $2 AND active
                    RETURNING user_id, color_key, hex_value, purchase_price,
                              active, equipped, purchased_at
                    """,
                    int(user_id),
                    color_key,
                )
                if equipped is None:  # pragma: no cover - row is locked above.
                    raise ColorNotOwned(color_key)
                return equipped

    async def unequip_color(self, user_id: int) -> bool:
        """Unequip the user's current colour, if one is equipped."""

        await self._flush_click_rewards(int(user_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE user_colors
                    SET equipped = FALSE
                    WHERE user_id = $1 AND active AND equipped
                    RETURNING color_key
                    """,
                    int(user_id),
                )
                return row is not None

    async def sell_color(self, user_id: int, color_key: str) -> ColorSale:
        """Deactivate one owned colour and refund half its purchase price."""

        await self._flush_click_rewards(int(user_id))
        color_key = str(color_key).strip()
        if not color_key:
            raise ColorNotOwned(color_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                color = await connection.fetchrow(
                    """
                    SELECT color_key, hex_value, purchase_price, active, equipped
                    FROM user_colors
                    WHERE user_id = $1 AND color_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    color_key,
                )
                if color is None:
                    raise ColorNotOwned(color_key)
                purchase_price = int(color["purchase_price"])
                refund_amount = purchase_price // 2
                updated = await connection.fetchrow(
                    """
                    UPDATE user_colors
                    SET active = FALSE, equipped = FALSE
                    WHERE user_id = $1 AND color_key = $2 AND active
                    RETURNING color_key, hex_value, purchase_price
                    """,
                    int(user_id),
                    color_key,
                )
                if updated is None:  # pragma: no cover - row is locked above.
                    raise ColorNotOwned(color_key)
                wallet = await self._locked_wallet(connection, int(user_id))
                if refund_amount:
                    if refund_amount > MAX_COIN_BALANCE - wallet.balance:
                        raise BalanceOverflow(
                            "This refund would exceed the wallet limit."
                        )
                    wallet_row = await connection.fetchrow(
                        """
                        UPDATE currency_wallets
                        SET balance = balance + $2, updated_at = now()
                        WHERE user_id = $1
                        RETURNING user_id, balance
                        """,
                        int(user_id),
                        refund_amount,
                    )
                    if wallet_row is None:  # pragma: no cover - UPDATE is valid.
                        raise BalanceOverflow("This refund could not be applied.")
                    wallet = _wallet(wallet_row)
                    await connection.execute(
                        """
                        INSERT INTO currency_transactions(
                            user_id, amount, source, reference_key
                        ) VALUES ($1, $2, 'color_sale', $3)
                        """,
                        int(user_id),
                        refund_amount,
                        f"color-sale:{int(user_id)}:{color_key}",
                    )
                return ColorSale(
                    color_key=str(updated["color_key"]),
                    hex_value=str(updated["hex_value"]),
                    refund_amount=refund_amount,
                    wallet_balance=wallet.balance,
                )

    async def purchase_racing_emoji(
        self,
        user_id: int,
        emoji_key: str,
        emoji_name: str,
        emoji_id: int | None,
        unicode: bool,
        animated: bool,
        display: str,
        category: str,
        price: int,
        guild_id: int | None = None,
    ) -> Wallet:
        """Purchase a racing emoji and make it active atomically.

        Racing emojis are user-defined (including arbitrary Unicode and
        server custom emojis), so unlike badges they do not have a finite
        catalog row.  ``emoji_key`` is the stable identity supplied by the
        command: the exact Unicode sequence for Unicode emojis or
        ``custom:<emoji id>`` for custom emojis.
        """

        await self._flush_click_rewards(int(user_id))
        values = {
            "emoji_key": str(emoji_key).strip(),
            "emoji_name": str(emoji_name).strip(),
            "display": str(display).strip(),
            "category": str(category).strip(),
        }
        if not all(values.values()):
            raise ValueError("A racing emoji identity and display are required.")
        if price <= 0:
            raise InvalidAmount("A racing emoji price must be positive.")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                existing = await connection.fetchrow(
                    """
                    SELECT active
                    FROM user_racing_emojis
                    WHERE user_id = $1 AND emoji_key = $2
                    FOR UPDATE
                    """,
                    int(user_id),
                    values["emoji_key"],
                )
                if existing is not None and bool(existing["active"]):
                    raise RacingEmojiAlreadyOwned(values["emoji_key"])
                if wallet.balance < price:
                    raise InsufficientFunds(wallet.balance, price)
                wallet_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    int(user_id),
                    int(price),
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, 'racing_emoji_purchase', $3)
                    """,
                    int(user_id),
                    -int(price),
                    f"racing-emoji:{int(user_id)}:{values['emoji_key']}",
                )
                has_equipped = bool(
                    await connection.fetchval(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM user_racing_emojis
                            WHERE user_id = $1 AND active AND equipped
                        )
                        """,
                        int(user_id),
                    )
                )
                await connection.execute(
                    """
                    INSERT INTO user_racing_emojis(
                        user_id, emoji_key, emoji_name, emoji_id, unicode,
                        animated, display, category, purchase_price,
                        purchase_guild_id, active, equipped
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,TRUE,$11)
                    ON CONFLICT (user_id, emoji_key) DO UPDATE SET
                        emoji_name = EXCLUDED.emoji_name,
                        emoji_id = EXCLUDED.emoji_id,
                        unicode = EXCLUDED.unicode,
                        animated = EXCLUDED.animated,
                        display = EXCLUDED.display,
                        category = EXCLUDED.category,
                        purchase_price = EXCLUDED.purchase_price,
                        purchase_guild_id = EXCLUDED.purchase_guild_id,
                        active = TRUE,
                        equipped = CASE
                            WHEN user_racing_emojis.equipped THEN TRUE
                            ELSE EXCLUDED.equipped
                        END,
                        purchased_at = now()
                    """,
                    int(user_id),
                    values["emoji_key"],
                    values["emoji_name"],
                    emoji_id,
                    bool(unicode),
                    bool(animated),
                    values["display"],
                    values["category"],
                    int(price),
                    guild_id,
                    not has_equipped,
                )
                return _wallet(wallet_row)

    async def ring_catalog(self, *, enabled_only: bool = False) -> list[Any]:
        """Return rings in the shop catalog, ordered by price then name."""

        where = "WHERE enabled" if enabled_only else ""
        return list(await self.pool.fetch(f"""
                SELECT ring_key, display_name, emoji_name, emoji_id, unicode,
                       animated, display, price, enabled, created_at
                FROM ring_catalog
                {where}
                ORDER BY price DESC, display_name ASC
                """))

    async def purchase_ring(self, user_id: int, ring_key: str, price: int) -> Wallet:
        """Debit Coins and add one unit to a stackable ring inventory."""

        await self._flush_click_rewards(int(user_id))
        ring_key = str(ring_key).strip()
        if not ring_key:
            raise RingNotOwned(ring_key)
        if int(price) <= 0:
            raise InvalidAmount("A ring price must be positive.")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                catalog = await connection.fetchrow(
                    """
                    SELECT ring_key, price, enabled
                    FROM ring_catalog
                    WHERE ring_key = $1
                    FOR SHARE
                    """,
                    ring_key,
                )
                if catalog is None or not bool(catalog["enabled"]):
                    raise RingNotOwned(ring_key)
                catalog_price = int(catalog["price"])
                if catalog_price != int(price):
                    # Never trust a stale command-side price when debiting a
                    # wallet. The database catalog is authoritative.
                    price = catalog_price
                wallet = await self._locked_wallet(connection, int(user_id))
                if wallet.balance < int(price):
                    raise InsufficientFunds(wallet.balance, int(price))
                wallet_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    int(user_id),
                    int(price),
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, 'ring_purchase', $3)
                    """,
                    int(user_id),
                    -int(price),
                    f"ring:{int(user_id)}:{ring_key}:{secrets.token_hex(12)}",
                )
                await connection.execute(
                    """
                    INSERT INTO user_rings(
                        user_id, ring_key, quantity, equipped_count
                    ) VALUES ($1, $2, 1, 0)
                    ON CONFLICT (user_id, ring_key) DO UPDATE SET
                        quantity = user_rings.quantity + 1,
                        updated_at = now()
                    """,
                    int(user_id),
                    ring_key,
                )
                return _wallet(wallet_row)

    async def owned_rings(self, user_id: int) -> list[Any]:
        """Return a user's positive ring stacks with catalog metadata."""

        return list(
            await self.pool.fetch(
                """
                SELECT rings.user_id, rings.ring_key, rings.quantity,
                       rings.equipped_count, rings.purchased_at, rings.updated_at,
                       catalog.display_name, catalog.emoji_name, catalog.emoji_id,
                       catalog.unicode, catalog.animated, catalog.display,
                       catalog.price, catalog.enabled
                FROM user_rings AS rings
                JOIN ring_catalog AS catalog USING (ring_key)
                WHERE rings.user_id = $1 AND rings.quantity > 0
                ORDER BY rings.updated_at DESC, catalog.display_name ASC
                """,
                int(user_id),
            )
        )

    async def sell_ring(self, user_id: int, ring_key: str) -> RingSale:
        """Sell one owned ring unit for half its purchase price.

        A ring that is currently equipped cannot be sold.  This also covers
        the ring automatically equipped for a marriage, which must remain in
        the user's inventory until the marriage ends.
        """

        await self._flush_click_rewards(int(user_id))
        ring_key = str(ring_key).strip()
        if not ring_key:
            raise RingNotOwned(ring_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                ring = await connection.fetchrow(
                    """
                    SELECT rings.ring_key, catalog.display, rings.quantity,
                           rings.equipped_count, catalog.price
                    FROM user_rings AS rings
                    JOIN ring_catalog AS catalog USING (ring_key)
                    WHERE rings.user_id = $1 AND rings.ring_key = $2
                      AND rings.quantity > 0
                    FOR UPDATE
                    """,
                    int(user_id),
                    ring_key,
                )
                if ring is None:
                    raise RingNotOwned(ring_key)
                if int(ring["equipped_count"] or 0) > 0:
                    raise RingEquipped(ring_key)
                refund_amount = int(ring["price"]) // 2
                updated = await connection.fetchrow(
                    """
                    UPDATE user_rings
                    SET quantity = quantity - 1, updated_at = now()
                    WHERE user_id = $1 AND ring_key = $2
                      AND quantity > 0 AND equipped_count = 0
                    RETURNING ring_key, quantity
                    """,
                    int(user_id),
                    ring_key,
                )
                if updated is None:  # pragma: no cover - row is locked above.
                    raise RingEquipped(ring_key)
                wallet = await self._locked_wallet(connection, int(user_id))
                if refund_amount > MAX_COIN_BALANCE - wallet.balance:
                    raise BalanceOverflow("This refund would exceed the wallet limit.")
                wallet_row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance + $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    int(user_id),
                    refund_amount,
                )
                if wallet_row is None:  # pragma: no cover - locked row exists.
                    raise BalanceOverflow("This refund could not be applied.")
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, 'ring_sale', $3)
                    """,
                    int(user_id),
                    refund_amount,
                    f"ring-sale:{int(user_id)}:{ring_key}:{secrets.token_hex(12)}",
                )
                return RingSale(
                    ring_key=str(updated["ring_key"]),
                    display=str(ring["display"]),
                    refund_amount=refund_amount,
                    wallet_balance=int(wallet_row["balance"]),
                    remaining_quantity=int(updated["quantity"]),
                )

    async def equip_ring(self, user_id: int, ring_key: str) -> Any:
        """Equip one unit of an owned ring stack.

        Marriage eligibility is a social rule and is deliberately checked at
        the command/service boundary before this inventory mutation. Keeping
        this operation generic lets proposal acceptance equip the transferred
        ring in the same transaction as the marriage update.
        """

        await self._flush_click_rewards(int(user_id))
        ring_key = str(ring_key).strip()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                owned = await connection.fetchrow(
                    """
                    SELECT quantity
                    FROM user_rings
                    WHERE user_id = $1 AND ring_key = $2 AND quantity > 0
                    FOR UPDATE
                    """,
                    int(user_id),
                    ring_key,
                )
                if owned is None:
                    raise RingNotOwned(ring_key)
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET equipped_count = 0, updated_at = now()
                    WHERE user_id = $1 AND equipped_count > 0
                    """,
                    int(user_id),
                )
                equipped = await connection.fetchrow(
                    """
                    UPDATE user_rings
                    SET equipped_count = 1, updated_at = now()
                    WHERE user_id = $1 AND ring_key = $2 AND quantity > 0
                    RETURNING user_id, ring_key, quantity, equipped_count,
                              purchased_at, updated_at
                    """,
                    int(user_id),
                    ring_key,
                )
                if equipped is None:  # pragma: no cover - row is locked above.
                    raise RingNotOwned(ring_key)
                return equipped

    async def unequip_ring(self, user_id: int) -> bool:
        """Unequip a user's current ring without changing ownership."""

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE user_rings
                    SET equipped_count = 0, updated_at = now()
                    WHERE user_id = $1 AND equipped_count > 0
                    RETURNING ring_key
                    """,
                    int(user_id),
                )
                return row is not None

    async def transfer_ring(
        self,
        sender_id: int,
        recipient_id: int,
        ring_key: str,
        *,
        equip_recipient: bool = True,
    ) -> RingTransfer:
        """Atomically transfer one ring unit, optionally equipping it.

        Proposal acceptance uses this after confirming that the sender owns at
        least two rings overall. The database locks inventories in stable user
        order so simultaneous proposals cannot duplicate a ring.
        """

        sender_id = int(sender_id)
        recipient_id = int(recipient_id)
        ring_key = str(ring_key).strip()
        if sender_id == recipient_id or not ring_key:
            raise RingNotOwned(ring_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                # Wallet rows are guaranteed before inserting a recipient's
                # first inventory row. Locking in ID order prevents deadlocks.
                for user_id in sorted((sender_id, recipient_id)):
                    await self._locked_wallet(connection, user_id)
                sender = await connection.fetchrow(
                    """
                    SELECT quantity, equipped_count
                    FROM user_rings
                    WHERE user_id = $1 AND ring_key = $2 AND quantity > 0
                    FOR UPDATE
                    """,
                    sender_id,
                    ring_key,
                )
                if sender is None:
                    raise RingNotOwned(ring_key)
                new_sender_quantity = int(sender["quantity"]) - 1
                new_sender_equipped = min(
                    int(sender["equipped_count"]), new_sender_quantity
                )
                await connection.execute(
                    """
                    UPDATE user_rings
                    SET quantity = $3, equipped_count = $4, updated_at = now()
                    WHERE user_id = $1 AND ring_key = $2
                    """,
                    sender_id,
                    ring_key,
                    new_sender_quantity,
                    new_sender_equipped,
                )
                if equip_recipient:
                    await connection.execute(
                        """
                        UPDATE user_rings
                        SET equipped_count = 0, updated_at = now()
                        WHERE user_id = $1 AND equipped_count > 0
                        """,
                        recipient_id,
                    )
                recipient = await connection.fetchrow(
                    """
                    INSERT INTO user_rings(
                        user_id, ring_key, quantity, equipped_count
                    ) VALUES ($1, $2, 1, $3)
                    ON CONFLICT (user_id, ring_key) DO UPDATE SET
                        quantity = user_rings.quantity + 1,
                        equipped_count = CASE
                            WHEN $3 > 0 THEN 1
                            ELSE user_rings.equipped_count
                        END,
                        updated_at = now()
                    RETURNING quantity, equipped_count
                    """,
                    recipient_id,
                    ring_key,
                    1 if equip_recipient else 0,
                )
                return RingTransfer(
                    sender_id=sender_id,
                    recipient_id=recipient_id,
                    ring_key=ring_key,
                    recipient_quantity=int(recipient["quantity"]),
                    recipient_equipped_count=int(recipient["equipped_count"]),
                )

    async def owned_racing_emojis(self, user_id: int) -> list[Any]:
        """Return all active racing emojis owned by a user."""

        return list(
            await self.pool.fetch(
                """
                SELECT user_id, emoji_key, emoji_name, emoji_id, unicode,
                       animated, display, category, purchase_price,
                       purchase_guild_id, active, equipped, purchased_at
                FROM user_racing_emojis
                WHERE user_id = $1 AND active
                ORDER BY purchased_at DESC, emoji_key ASC
                """,
                int(user_id),
            )
        )

    async def equipped_racing_emoji(self, user_id: int) -> Any | None:
        """Return a user's equipped racing emoji, if they have one."""

        return await self.pool.fetchrow(
            """
            SELECT user_id, emoji_key, emoji_name, emoji_id, unicode,
                   animated, display, category, purchase_price,
                   purchase_guild_id, active, equipped, purchased_at
            FROM user_racing_emojis
            WHERE user_id = $1 AND active AND equipped
            LIMIT 1
            """,
            int(user_id),
        )

    async def equip_racing_emoji(self, user_id: int, emoji_key: str) -> Any:
        """Equip one owned racing emoji and unequip the previous one."""

        await self._flush_click_rewards(int(user_id))
        emoji_key = str(emoji_key).strip()
        if not emoji_key:
            raise RacingEmojiNotOwned(emoji_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                owned = await connection.fetchrow(
                    """
                    SELECT user_id, emoji_key, emoji_name, emoji_id, unicode,
                           animated, display, category, purchase_price,
                           purchase_guild_id, active, equipped, purchased_at
                    FROM user_racing_emojis
                    WHERE user_id = $1 AND emoji_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    emoji_key,
                )
                if owned is None:
                    raise RacingEmojiNotOwned(emoji_key)
                await connection.execute(
                    """
                    UPDATE user_racing_emojis
                    SET equipped = FALSE
                    WHERE user_id = $1 AND active AND equipped
                    """,
                    int(user_id),
                )
                equipped = await connection.fetchrow(
                    """
                    UPDATE user_racing_emojis
                    SET equipped = TRUE
                    WHERE user_id = $1 AND emoji_key = $2 AND active
                    RETURNING user_id, emoji_key, emoji_name, emoji_id, unicode,
                              animated, display, category, purchase_price,
                              purchase_guild_id, active, equipped, purchased_at
                    """,
                    int(user_id),
                    emoji_key,
                )
                if equipped is None:  # pragma: no cover - row locked above.
                    raise RacingEmojiNotOwned(emoji_key)
                return equipped

    async def unequip_racing_emoji(self, user_id: int) -> bool:
        """Unequip a user's current racing emoji, if one is equipped."""

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE user_racing_emojis
                    SET equipped = FALSE
                    WHERE user_id = $1 AND active AND equipped
                    RETURNING emoji_key
                    """,
                    int(user_id),
                )
                return row is not None

    async def sell_racing_emoji(self, user_id: int, emoji_key: str) -> RacingEmojiSale:
        """Deactivate one racing emoji and refund half its purchase price."""

        await self._flush_click_rewards(int(user_id))
        emoji_key = str(emoji_key).strip()
        if not emoji_key:
            raise RacingEmojiNotOwned(emoji_key)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT emoji_key, display, purchase_price
                    FROM user_racing_emojis
                    WHERE user_id = $1 AND emoji_key = $2 AND active
                    FOR UPDATE
                    """,
                    int(user_id),
                    emoji_key,
                )
                if row is None:
                    raise RacingEmojiNotOwned(emoji_key)
                purchase_price = int(row["purchase_price"])
                refund_amount = purchase_price // 2
                updated = await connection.fetchrow(
                    """
                    UPDATE user_racing_emojis
                    SET active = FALSE, equipped = FALSE
                    WHERE user_id = $1 AND emoji_key = $2 AND active
                    RETURNING emoji_key, display
                    """,
                    int(user_id),
                    emoji_key,
                )
                if updated is None:  # pragma: no cover - row is locked above.
                    raise RacingEmojiNotOwned(emoji_key)
                wallet = await self._locked_wallet(connection, int(user_id))
                if refund_amount:
                    if refund_amount > MAX_COIN_BALANCE - wallet.balance:
                        raise BalanceOverflow(
                            "This refund would exceed the wallet limit."
                        )
                    wallet_row = await connection.fetchrow(
                        """
                        UPDATE currency_wallets
                        SET balance = balance + $2, updated_at = now()
                        WHERE user_id = $1
                        RETURNING user_id, balance
                        """,
                        int(user_id),
                        refund_amount,
                    )
                    if wallet_row is None:  # pragma: no cover - UPDATE is valid.
                        raise BalanceOverflow("This refund could not be applied.")
                    wallet = _wallet(wallet_row)
                    await connection.execute(
                        """
                        INSERT INTO currency_transactions(
                            user_id, amount, source, reference_key
                        ) VALUES ($1, $2, 'racing_emoji_sale', $3)
                        """,
                        int(user_id),
                        refund_amount,
                        f"racing-emoji-sale:{int(user_id)}:{emoji_key}",
                    )
                return RacingEmojiSale(
                    emoji_key=str(updated["emoji_key"]),
                    display=str(updated["display"]),
                    refund_amount=refund_amount,
                    wallet_balance=wallet.balance,
                )

    async def claim(
        self,
        user_id: int,
        claim_type: ClaimType,
        base_amount: int,
        bonus_amount: int = 0,
        *,
        streak_bonus_per_period: int = 0,
        now: datetime | None = None,
    ) -> ClaimResult:
        await self._flush_click_rewards(int(user_id))
        if base_amount <= 0 or bonus_amount < 0 or streak_bonus_per_period < 0:
            raise InvalidAmount("Claim amounts cannot be negative or empty.")
        period_start = claim_period_start(claim_type, now)
        previous_period = period_start - timedelta(
            days=1 if claim_type == "daily" else 7
        )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                previous = await connection.fetchrow(
                    """
                    SELECT streak
                    FROM currency_claims
                    WHERE user_id = $1 AND claim_type = $2 AND period_start = $3
                    """,
                    user_id,
                    claim_type,
                    previous_period,
                )
                streak = int(previous["streak"]) + 1 if previous else 1
                streak_bonus_amount = streak * streak_bonus_per_period
                amount = base_amount + bonus_amount + streak_bonus_amount
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO currency_claims(
                        user_id, claim_type, period_start, base_amount,
                        bonus_amount, streak, streak_bonus_amount
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (user_id, claim_type, period_start) DO NOTHING
                    RETURNING base_amount, bonus_amount, streak, streak_bonus_amount
                    """,
                    user_id,
                    claim_type,
                    period_start,
                    base_amount,
                    bonus_amount,
                    streak,
                    streak_bonus_amount,
                )
                if inserted is None:
                    previous = await connection.fetchrow(
                        """
                        SELECT base_amount, bonus_amount, streak, streak_bonus_amount
                        FROM currency_claims
                        WHERE user_id = $1 AND claim_type = $2 AND period_start = $3
                        """,
                        user_id,
                        claim_type,
                        period_start,
                    )
                    return ClaimResult(
                        wallet=wallet,
                        claim_type=claim_type,
                        period_start=period_start,
                        claimed=False,
                        base_amount=int(previous["base_amount"]),
                        bonus_amount=int(previous["bonus_amount"]),
                        streak=int(previous["streak"]),
                        streak_bonus_amount=int(previous["streak_bonus_amount"]),
                    )
                if amount > MAX_COIN_BALANCE - wallet.balance:
                    raise BalanceOverflow("This claim would exceed the wallet limit.")
                row = await connection.fetchrow(
                    """
                    UPDATE currency_wallets
                    SET balance = balance + $2, updated_at = now()
                    WHERE user_id = $1
                    RETURNING user_id, balance
                    """,
                    user_id,
                    amount,
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, $3, $4)
                    """,
                    user_id,
                    amount,
                    f"claim_{claim_type}",
                    f"claim:{claim_type}:{period_start.isoformat()}",
                )
                return ClaimResult(
                    wallet=_wallet(row),
                    claim_type=claim_type,
                    period_start=period_start,
                    claimed=True,
                    base_amount=base_amount,
                    bonus_amount=bonus_amount,
                    streak=streak,
                    streak_bonus_amount=streak_bonus_amount,
                )

    async def award_daily_capped(
        self,
        user_id: int,
        amount: int,
        daily_cap: int,
        source: str,
        *,
        now: datetime | None = None,
    ) -> int:
        """Credit up to a source's remaining UTC-day allowance."""

        await self._flush_click_rewards(int(user_id))
        if amount <= 0 or daily_cap <= 0:
            raise InvalidAmount("Reward amount and daily cap must be positive.")
        if not source:
            raise ValueError("Daily reward source cannot be empty")
        period_start = claim_period_start("daily", now)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                row = await connection.fetchrow(
                    """
                    SELECT amount
                    FROM currency_daily_rewards
                    WHERE user_id = $1 AND source = $2 AND period_start = $3
                    """,
                    user_id,
                    source,
                    period_start,
                )
                already_awarded = int(row["amount"]) if row is not None else 0
                awarded = min(amount, max(daily_cap - already_awarded, 0))
                if awarded == 0:
                    return 0
                if awarded > MAX_COIN_BALANCE - wallet.balance:
                    raise BalanceOverflow("This reward would exceed the wallet limit.")
                await connection.execute(
                    """
                    INSERT INTO currency_daily_rewards(
                        user_id, source, period_start, amount
                    ) VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, source, period_start) DO UPDATE
                    SET amount = currency_daily_rewards.amount + EXCLUDED.amount,
                        updated_at = now()
                    """,
                    user_id,
                    source,
                    period_start,
                    awarded,
                )
                await connection.execute(
                    """
                    UPDATE currency_wallets
                    SET balance = balance + $2, updated_at = now()
                    WHERE user_id = $1
                    """,
                    user_id,
                    awarded,
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(user_id, amount, source)
                    VALUES ($1, $2, $3)
                    """,
                    user_id,
                    awarded,
                    f"game_reward:{source}",
                )
                return awarded

    async def open_wager(
        self,
        user_id: int,
        stake: int,
        source: str = "higher_lower",
        *,
        max_stake: int | None = None,
    ) -> Wager:
        await self._flush_click_rewards(int(user_id))
        if max_stake is not None and max_stake < MIN_WAGER_STAKE:
            raise InvalidAmount(
                f"A wager maximum must be at least {MIN_WAGER_STAKE} Coins."
            )
        if stake < MIN_WAGER_STAKE or (max_stake is not None and stake > max_stake):
            if max_stake is None:
                raise InvalidAmount(
                    f"A wager must be at least {MIN_WAGER_STAKE} Coins."
                )
            raise InvalidAmount(
                f"A wager must be between {MIN_WAGER_STAKE} and {max_stake:,} Coins."
            )
        if not source:
            raise ValueError("Wager source cannot be empty")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                wallet = await self._locked_wallet(connection, int(user_id))
                if wallet.balance < stake:
                    raise InsufficientFunds(wallet.balance, stake)
                row = await connection.fetchrow(
                    """
                    INSERT INTO currency_wagers(user_id, source, stake)
                    VALUES ($1, $2, $3)
                    RETURNING id, user_id, source, stake, status, payout
                    """,
                    user_id,
                    source,
                    stake,
                )
                wager = _wager(row)
                await connection.execute(
                    """
                    UPDATE currency_wallets
                    SET balance = balance - $2, updated_at = now()
                    WHERE user_id = $1
                    """,
                    user_id,
                    stake,
                )
                await connection.execute(
                    """
                    INSERT INTO currency_transactions(
                        user_id, amount, source, reference_key
                    ) VALUES ($1, $2, $3, $4)
                    """,
                    user_id,
                    -stake,
                    f"wager_stake:{source}",
                    f"wager:{wager.id}:stake",
                )
                await connection.execute(
                    """
                    INSERT INTO currency_gambling_stats(
                        user_id, total_wagered
                    ) VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE
                    SET total_wagered = currency_gambling_stats.total_wagered
                        + EXCLUDED.total_wagered,
                        updated_at = now()
                    """,
                    user_id,
                    stake,
                )
                return wager

    async def settle_wager(
        self,
        wager_id: int,
        payout: int,
        *,
        track_stats: bool = True,
    ) -> WagerSettlement:
        """Settle one reserved wager exactly once.

        ``track_stats`` is disabled only for crash-recovery refunds.  Those
        refunds return the reserved stake without representing either a win
        or a loss, so counting them would skew the user's gambling totals.
        """

        if payout < 0 or payout > MAX_WAGER_PAYOUT:
            raise InvalidAmount(
                f"A wager payout must be between 0 and {MAX_WAGER_PAYOUT:,} Coins."
            )
        # Resolve the owner before opening the settlement transaction.  The
        # click-reward flusher uses its own wallet transaction; running it
        # after locking the wager row could deadlock against a concurrent
        # wallet operation.
        if self._click_reward_flusher is not None:
            wager_user_id = await self.pool.fetchval(
                "SELECT user_id FROM currency_wagers WHERE id = $1", wager_id
            )
            if wager_user_id is not None:
                await self._flush_click_rewards(int(wager_user_id))
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT id, user_id, source, stake, status, payout
                    FROM currency_wagers
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    wager_id,
                )
                if row is None:
                    raise WagerNotFound(f"Wager {wager_id} was not found.")
                wager = _wager(row)
                if wager.status != "open":
                    return WagerSettlement(wager=wager, settled=False)

                status: WagerStatus = "cashed_out" if payout else "lost"
                balance: int | None = None
                if payout:
                    wallet = await self._locked_wallet(connection, wager.user_id)
                    if payout > MAX_COIN_BALANCE - wallet.balance:
                        raise BalanceOverflow(
                            "This payout would exceed the wallet limit."
                        )
                    wallet_row = await connection.fetchrow(
                        """
                        UPDATE currency_wallets
                        SET balance = balance + $2, updated_at = now()
                        WHERE user_id = $1
                          AND balance <= $3
                        RETURNING user_id, balance
                        """,
                        wager.user_id,
                        payout,
                        MAX_COIN_BALANCE - payout,
                    )
                    if wallet_row is None:
                        raise BalanceOverflow(
                            "This payout would exceed the wallet limit."
                        )
                    balance = int(wallet_row["balance"])
                    await connection.execute(
                        """
                        INSERT INTO currency_transactions(
                            user_id, amount, source, reference_key
                        ) VALUES ($1, $2, $3, $4)
                        """,
                        wager.user_id,
                        payout,
                        f"wager_cashout:{wager.source}",
                        f"wager:{wager.id}:cashout",
                    )

                settled_row = await connection.fetchrow(
                    """
                    UPDATE currency_wagers
                    SET status = $2, payout = $3, settled_at = now()
                    WHERE id = $1
                    RETURNING id, user_id, source, stake, status, payout
                    """,
                    wager.id,
                    status,
                    payout,
                )
                if track_stats and status == "cashed_out":
                    await connection.execute(
                        """
                        INSERT INTO currency_gambling_stats(
                            user_id, total_earned, wins
                        ) VALUES ($1, $2, 1)
                        ON CONFLICT (user_id) DO UPDATE
                        SET total_earned = currency_gambling_stats.total_earned
                            + EXCLUDED.total_earned,
                            wins = currency_gambling_stats.wins + 1,
                            updated_at = now()
                        """,
                        wager.user_id,
                        payout,
                    )
                elif track_stats:
                    await connection.execute(
                        """
                        INSERT INTO currency_gambling_stats(
                            user_id, total_lost, losses
                        ) VALUES ($1, $2, 1)
                        ON CONFLICT (user_id) DO UPDATE
                        SET total_lost = currency_gambling_stats.total_lost
                            + EXCLUDED.total_lost,
                            losses = currency_gambling_stats.losses + 1,
                            updated_at = now()
                        """,
                        wager.user_id,
                        wager.stake,
                    )
                return WagerSettlement(
                    wager=_wager(settled_row), settled=True, balance=balance
                )

    async def recover_stale_wagers(
        self,
        *,
        older_than: timedelta = timedelta(minutes=15),
        now: datetime | None = None,
        exclude_wager_ids: Collection[int] = (),
    ) -> int:
        """Refund open wagers whose in-memory game can no longer be active.

        The age threshold protects live views during an extension reload. Each
        refund is still settled under the wager row lock, so a concurrent guess,
        cashout, timeout, or second recovery pass cannot credit it twice.
        """

        if older_than < timedelta():
            raise ValueError("Stale wager age cannot be negative")
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - older_than
        rows = await self.pool.fetch(
            """
            SELECT id, stake
            FROM currency_wagers
            WHERE status = 'open' AND created_at <= $1
            ORDER BY id
            """,
            cutoff,
        )
        excluded = {int(wager_id) for wager_id in exclude_wager_ids}
        recovered = 0
        for row in rows:
            if int(row["id"]) in excluded:
                continue
            result = await self.settle_wager(
                int(row["id"]), int(row["stake"]), track_stats=False
            )
            recovered += int(result.settled)
        return recovered


async def award_daily_capped_coins(
    pool: Any,
    user_id: int,
    amount: int,
    daily_cap: int,
    source: str,
) -> int:
    """Game-friendly wrapper for an atomic, UTC-day-capped reward."""

    return await CurrencyService(pool).award_daily_capped(
        user_id, amount, daily_cap, source
    )
