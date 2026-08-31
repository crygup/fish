from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from extensions.fun.race import (
    RACE_COLUMNS,
    RACE_ROWS,
    RaceGame,
    RaceParticipant,
    injury_roll,
    movement_success,
    race_grid,
)
from utils.emojis import race_animals, race_blue_square


def test_race_uses_five_lanes_and_six_columns() -> None:
    game = RaceGame(
        ctx=None,  # type: ignore[arg-type]
        host_id=1,
        channel_id=2,
        guild_id=None,
        players=[
            RaceParticipant(1, "one", race_animals[0], 100),
            RaceParticipant(2, "two", race_animals[1], 100),
        ],
    )
    rows = race_grid(game).splitlines()
    assert len(rows) == RACE_ROWS
    assert all(len(row) == len(race_blue_square) * RACE_COLUMNS for row in rows)
    assert rows[0].startswith(race_animals[0])
    assert rows[1].startswith(race_animals[1])


def test_solo_human_gets_requested_move_odds() -> None:
    rng = random.Random(2)
    human_moves = sum(
        movement_success(solo_human=True, is_bot=False, rng=rng) for _ in range(10_000)
    )
    assert 5_200 < human_moves < 5_800


def test_solo_human_injury_odds_are_five_percent() -> None:
    rng = random.Random(4)
    injuries = sum(
        injury_roll(solo_human=True, is_bot=False, rng=rng) for _ in range(10_000)
    )
    assert 400 < injuries < 600


class _FakeCurrency:
    def __init__(
        self,
        emojis: dict[int, dict[str, object]],
        balances: dict[int, int],
        owned: dict[int, list[dict[str, object]]] | None = None,
    ):
        self.emojis = emojis
        self.balances = balances
        self.owned = owned or {}

    async def equipped_racing_emoji(self, user_id: int):
        return self.emojis.get(user_id)

    async def get_wallet(self, user_id: int):
        return SimpleNamespace(balance=self.balances.get(user_id, 0))

    async def owned_racing_emojis(self, user_id: int):
        return self.owned.get(user_id, [])


@pytest.mark.asyncio
async def test_richer_duplicate_owner_gets_racing_emoji_and_reserves_sea_animal() -> (
    None
):
    from extensions.fun.race import SeaAnimalRaceCommands

    emoji = race_animals[8]
    bot = SimpleNamespace(
        currency=_FakeCurrency(
            {
                1: {
                    "emoji_name": emoji,
                    "emoji_id": None,
                    "category": "sea_animal",
                },
                2: {
                    "emoji_name": emoji,
                    "emoji_id": None,
                    "category": "sea_animal",
                },
            },
            {1: 100, 2: 500},
        ),
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )
    cog = SeaAnimalRaceCommands.__new__(SeaAnimalRaceCommands)
    cog.bot = bot
    game = RaceGame(
        ctx=None,  # type: ignore[arg-type]
        host_id=1,
        channel_id=2,
        guild_id=None,
        players=[
            RaceParticipant(1, "poor", race_blue_square, 100),
            RaceParticipant(2, "rich", race_blue_square, 100),
            RaceParticipant(-1, "Bot 1", race_blue_square, 0, True, 50),
        ],
    )

    await cog._assign_racing_emojis(game)

    assert game.players[1].animal == emoji
    assert game.players[0].animal != emoji
    assert game.players[2].animal != emoji


@pytest.mark.asyncio
async def test_custom_racing_emoji_renders_and_duplicate_ties_are_deterministic() -> (
    None
):
    from extensions.fun.race import SeaAnimalRaceCommands

    bot = SimpleNamespace(
        currency=_FakeCurrency(
            {
                1: {"emoji_name": "sail", "emoji_id": 123, "animated": True},
                2: {"emoji_name": "sail", "emoji_id": 123, "animated": True},
            },
            {1: 100, 2: 100},
        ),
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )
    cog = SeaAnimalRaceCommands.__new__(SeaAnimalRaceCommands)
    cog.bot = bot
    game = RaceGame(
        ctx=None,  # type: ignore[arg-type]
        host_id=1,
        channel_id=2,
        guild_id=None,
        players=[
            RaceParticipant(1, "first", race_blue_square, 100),
            RaceParticipant(2, "second", race_blue_square, 100),
        ],
    )

    await cog._assign_racing_emojis(game)

    assert game.players[0].animal == "<a:sail:123>"
    assert game.players[1].animal != "<a:sail:123>"


@pytest.mark.asyncio
async def test_un_equipped_owned_sea_animal_is_reserved_from_fallback_lanes() -> None:
    from extensions.fun.race import SeaAnimalRaceCommands

    reserved = race_animals[0]
    bot = SimpleNamespace(
        currency=_FakeCurrency(
            {},
            {1: 100},
            {
                1: [
                    {
                        "emoji_name": reserved,
                        "emoji_id": None,
                        "category": "sea",
                    }
                ]
            },
        ),
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )
    cog = SeaAnimalRaceCommands.__new__(SeaAnimalRaceCommands)
    cog.bot = bot
    game = RaceGame(
        ctx=None,  # type: ignore[arg-type]
        host_id=1,
        channel_id=2,
        guild_id=None,
        players=[
            RaceParticipant(1, "owner", race_blue_square, 100),
            RaceParticipant(-1, "Bot 1", race_blue_square, 0, True, 50),
        ],
    )

    await cog._assign_racing_emojis(game)

    assert all(participant.animal != reserved for participant in game.players)
