import pytest

from extensions.fun.bot_participants import bot_wagers
from extensions.fun.luckyroll import (
    LUCKY_ROLL_DEFAULT_BID,
    LUCKY_ROLL_MAX_BID,
    LUCKY_ROLL_MAX_PLAYERS,
    LUCKY_ROLL_MIN_BID,
    LUCKY_ROLL_MIN_HUMANS,
    LUCKY_ROLL_TIE_DISPLAY_SECONDS,
    LuckyRollGame,
    LuckyRollParticipant,
    _participant_lines,
)


class _UpperBoundRng:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def randint(self, low: int, high: int) -> int:
        self.calls.append((low, high))
        return high


def test_lucky_roll_limits_and_defaults() -> None:
    assert LUCKY_ROLL_MIN_BID == 10
    assert LUCKY_ROLL_MAX_BID == 10_000
    assert LUCKY_ROLL_DEFAULT_BID == 100
    assert LUCKY_ROLL_MAX_PLAYERS == 6
    assert LUCKY_ROLL_MIN_HUMANS == 1
    assert LUCKY_ROLL_TIE_DISPLAY_SECONDS >= 2


def test_lucky_roll_strikes_eliminated_rows() -> None:
    game = LuckyRollGame(
        ctx=None,  # type: ignore[arg-type]
        host_id=1,
        channel_id=2,
        guild_id=None,
        players=[
            LuckyRollParticipant(
                1, "winner", 100, current_roll=6, stopped=True, final_roll=6
            ),
            LuckyRollParticipant(
                2,
                "loser",
                100,
                current_roll=2,
                stopped=True,
                final_roll=2,
                eliminated=True,
            ),
        ],
    )
    lines = _participant_lines(game)
    assert lines[0] == "winner - 6️⃣"
    assert lines[1] == "~~loser - 2️⃣~~"


@pytest.mark.parametrize(
    ("human_count", "percentage"),
    ((1, 80), (2, 60), (3, 40), (4, 30), (5, 10), (6, 10)),
)
def test_lucky_roll_bot_wagers_scale_with_human_count(
    human_count: int, percentage: int
) -> None:
    rng = _UpperBoundRng()
    highest_bid = 1_000

    wagers = bot_wagers(
        [highest_bid, *([LUCKY_ROLL_MIN_BID] * (human_count - 1))],
        2,
        rng=rng,
        minimum=LUCKY_ROLL_MIN_BID,
    )

    expected_cap = highest_bid * percentage // 100
    expected_low = LUCKY_ROLL_MIN_BID
    expected_high = max(expected_low, expected_cap)
    assert wagers == [expected_high, expected_high]
    assert rng.calls == [
        (expected_low, expected_high),
        (expected_low, expected_high),
    ]


def test_lucky_roll_bot_wagers_keep_multi_human_lower_bound() -> None:
    rng = _UpperBoundRng()

    wagers = bot_wagers(
        [1_000, 250],
        1,
        rng=rng,
        minimum=LUCKY_ROLL_MIN_BID,
    )

    assert wagers == [600]
    assert rng.calls == [(250, 600)]


def test_lucky_roll_bot_wagers_never_exceed_scaled_cap_when_humans_match() -> None:
    rng = _UpperBoundRng()

    wagers = bot_wagers(
        [1_000] * 5,
        2,
        rng=rng,
        minimum=LUCKY_ROLL_MIN_BID,
    )

    # Five human players cap bots at 10% of the highest human wager.  The
    # lowest human wager must not override that cap when it is larger.
    assert wagers == [100, 100]
    assert rng.calls == [(100, 100), (100, 100)]
