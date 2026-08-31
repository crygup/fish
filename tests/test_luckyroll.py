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
