import datetime
from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.logging.commands import ActivityPageSource
from extensions.moderation.logger import LOGGER_EVENTS, Logger
from utils.activities import (
    activity_details,
    activity_identity,
    activity_matches,
    activity_snapshot,
    loggable_activities,
)


def test_activity_details_include_discord_application_metadata() -> None:
    started = discord.utils.utcnow() - datetime.timedelta(minutes=5)
    activity = discord.Activity(
        name="Example Game",
        application_id=1234,
        details="Ranked",
        state="In a party",
        timestamps={"start": int(started.timestamp() * 1000)},
        party={"id": "party", "size": [2, 4]},
    )
    details = dict(activity_details(activity))
    assert details["Application ID"] == "1234"
    assert details["Details"] == "Ranked"
    assert details["State"] == "In a party"
    assert details["Party"] == "ID: party • Size: 2/4"
    assert "Started" in details


def test_activity_matching_and_identity_are_stable() -> None:
    first = discord.Game("Minecraft")
    second = discord.Game("Minecraft")
    assert activity_identity(first) == activity_identity(second)
    assert activity_snapshot(first) == activity_snapshot(second)
    assert activity_matches(first, "mine")


def test_activity_snapshot_ignores_progress_timestamps() -> None:
    first = discord.Game("Minecraft", timestamps={"start": 1000})
    second = discord.Game("Minecraft", timestamps={"start": 2000})

    assert activity_snapshot(first) == activity_snapshot(second)


def test_custom_statuses_are_not_server_activity_log_events() -> None:
    custom = discord.CustomActivity(name="hello")
    game = discord.Game("Example")
    assert loggable_activities((custom, game)) == (game,)
    assert "activity" in LOGGER_EVENTS


async def test_activity_pages_show_details_and_members() -> None:
    activity = discord.Activity(
        name="Example",
        application_id=123,
        type=discord.ActivityType.playing,
    )
    members = [
        SimpleNamespace(mention="<@1>"),
        SimpleNamespace(mention="<@2>"),
    ]
    source = ActivityPageSource(
        cast(Any, [(activity, members)]),
        color=discord.Color.blurple(),
    )
    embed = await source.format_page(
        cast(Any, SimpleNamespace(current_page=0)),
        cast(Any, (activity, members)),
    )
    assert embed.title == "Playing • Example"
    assert any(field.name == "Application ID" for field in embed.fields)
    assert embed.fields[-1].value == "<@1>\n<@2>"


async def test_activity_logger_ignores_status_only_presence_updates() -> None:
    logger = Logger()
    emissions = []
    logger.bot = cast(
        Any,
        SimpleNamespace(
            db_cache=SimpleNamespace(user_tracking_opted_out=lambda *_args: False)
        ),
    )

    async def emit(*args, **kwargs):
        emissions.append((args, kwargs))

    logger._emit_logger = emit
    game = discord.Game("Example")
    member = SimpleNamespace(
        activities=(game,),
        guild=SimpleNamespace(),
        mention="<@1>",
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
        id=1,
    )
    await logger.logger_activity_update(cast(Any, member), cast(Any, member))
    assert emissions == []

    after = SimpleNamespace(
        activities=(discord.Game("Another Game"),),
        guild=member.guild,
        mention=member.mention,
        display_avatar=member.display_avatar,
        id=member.id,
    )
    await logger.logger_activity_update(cast(Any, member), cast(Any, after))
    assert emissions[0][0][1] == "activity"


async def test_activity_logger_ignores_state_changes_for_the_same_game() -> None:
    logger = Logger()
    emissions = []
    logger.bot = cast(
        Any,
        SimpleNamespace(
            db_cache=SimpleNamespace(user_tracking_opted_out=lambda *_args: False)
        ),
    )

    async def emit(*args, **kwargs):
        emissions.append((args, kwargs))

    logger._emit_logger = emit
    guild = SimpleNamespace()
    before = SimpleNamespace(
        activities=(
            SimpleNamespace(
                type=discord.ActivityType.playing,
                name="Fortnite",
                application_id=123,
                details="15 players left",
            ),
        ),
        guild=guild,
        id=1,
    )
    after = SimpleNamespace(
        activities=(
            SimpleNamespace(
                type=discord.ActivityType.playing,
                name="Fortnite",
                application_id=123,
                details="14 players left",
            ),
        ),
        guild=guild,
        id=1,
    )

    await logger.logger_activity_update(cast(Any, before), cast(Any, after))

    assert emissions == []


async def test_activity_logger_handles_missing_application_ids() -> None:
    logger = Logger()
    emissions = []
    logger.bot = cast(
        Any,
        SimpleNamespace(
            db_cache=SimpleNamespace(user_tracking_opted_out=lambda *_args: False)
        ),
    )

    async def emit(*args, **kwargs):
        emissions.append((args, kwargs))

    logger._emit_logger = emit
    guild = SimpleNamespace()
    before = SimpleNamespace(
        activities=(
            SimpleNamespace(
                type=discord.ActivityType.playing,
                name="Fortnite",
                application_id=None,
            ),
            SimpleNamespace(
                type=discord.ActivityType.playing,
                name="Fortnite",
                application_id=42,
            ),
        ),
        guild=guild,
        id=1,
    )
    after = SimpleNamespace(
        activities=before.activities,
        guild=guild,
        id=1,
    )

    await logger.logger_activity_update(cast(Any, before), cast(Any, after))

    assert emissions == []


async def test_activity_logger_respects_personal_tracking_settings() -> None:
    logger = Logger()
    emissions = []
    logger.bot = cast(
        Any,
        SimpleNamespace(
            db_cache=SimpleNamespace(user_tracking_opted_out=lambda *_args: True)
        ),
    )

    async def emit(*args, **kwargs):
        emissions.append((args, kwargs))

    logger._emit_logger = emit
    before = SimpleNamespace(
        activities=(discord.Game("Before"),),
        guild=SimpleNamespace(),
        id=1,
    )
    after = SimpleNamespace(
        activities=(discord.Game("After"),),
        guild=before.guild,
        id=before.id,
    )
    await logger.logger_activity_update(cast(Any, before), cast(Any, after))
    assert emissions == []
