from datetime import datetime, timezone
from io import BytesIO

from extensions.fun.wordle import (
    WordleBoardView,
    WordleGame,
    evaluate_guess,
    hard_mode_error,
    message_guess,
    new_wordle_game,
    render_wordle_board,
)


async def _unused_guess(*_args: object) -> None:
    pass


def test_repeated_letters_follow_wordle_two_pass_rules() -> None:
    assert evaluate_guess("cigar", "array") == (
        "absent",
        "present",
        "absent",
        "correct",
        "absent",
    )
    assert evaluate_guess("abbey", "cabba") == (
        "absent",
        "present",
        "correct",
        "present",
        "absent",
    )


def test_game_records_attempt_and_completes_on_sixth_guess() -> None:
    game = new_wordle_game(user_id=1, channel_id=2, answer="cigar")
    words = {"cigar", "arise", "array", "abbey", "sugar", "reach"}
    for _ in range(5):
        game.submit("arise", words)
    assert game.result is None
    game.submit("array", words)
    assert game.result == "lost"
    assert game.attempts == 6


def test_hard_mode_keeps_correct_letters_and_revealed_letters() -> None:
    game = new_wordle_game(
        user_id=1,
        channel_id=2,
        answer="cigar",
        hard_mode=True,
    )
    words = {"cigar", "arise", "cairn", "caper"}
    game.submit("arise", words)
    assert hard_mode_error(game.guesses, "caper") is not None
    # ``cairn`` keeps the revealed A and I and is not rejected by hard mode.
    assert hard_mode_error(game.guesses, "cairn") is None


def test_hard_mode_heading_omits_colourblind_label() -> None:
    game = WordleGame(
        user_id=1,
        channel_id=2,
        answer="cigar",
        hard_mode=True,
        colourblind_mode=True,
    )
    view = WordleBoardView(game, _unused_guess)

    assert "## Wordle · Hard mode" in view.display.content
    assert "Colourblind mode" not in view.display.content


def test_message_guess_is_limited_to_owner_and_channel() -> None:
    game = WordleGame(user_id=1, channel_id=20, answer="cigar")

    class Message:
        def __init__(self, author_id: int, channel_id: int, content: str) -> None:
            self.author = type("Author", (), {"id": author_id})()
            self.channel = type("Channel", (), {"id": channel_id})()
            self.content = content

    assert message_guess(Message(2, 20, "cigar"), game) is None
    assert message_guess(Message(1, 21, "cigar"), game) is None
    assert message_guess(Message(1, 20, "four"), game) is None
    assert message_guess(Message(1, 20, "cigar"), game) == "cigar"


def test_wordle_board_is_a_png_stream() -> None:
    game = WordleGame(
        user_id=1,
        channel_id=2,
        answer="cigar",
        started_at=datetime.now(timezone.utc),
    )
    game.submit("arise", {"cigar", "arise"})
    stream = render_wordle_board(game)
    assert isinstance(stream, BytesIO)
    assert stream.read(8) == b"\x89PNG\r\n\x1a\n"
