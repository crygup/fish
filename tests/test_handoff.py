from __future__ import annotations

import asyncio
from types import SimpleNamespace

# Test doubles supply only the Discord/service fields exercised by each test.
from typing import cast
from urllib.parse import parse_qs, urlsplit

import discord

from core import Fishie
from core.handoff import (
    HandoffNoticeThrottle,
    guild_install_url,
    handoff_notice,
    is_handoff_exempt_command,
    is_legacy_instance,
    profile_install_url,
)
from extensions.events.auto_download import AutoDownload


def _bot(*, legacy: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        is_legacy_bot=legacy,
        bot_permissions=discord.Permissions(send_messages=True),
        config={"ids": {}},
    )


def test_legacy_detection_and_owner_recovery_exemptions() -> None:
    assert is_legacy_instance(_bot(legacy=True))
    assert not is_legacy_instance(_bot(legacy=False))

    owner_command = SimpleNamespace(qualified_name="dev video block", binding=None)
    assert is_handoff_exempt_command(owner_command)

    regular_command = SimpleNamespace(qualified_name="download", binding=None)
    assert not is_handoff_exempt_command(regular_command)


def test_profile_install_url_is_user_install_only() -> None:
    query = parse_qs(urlsplit(profile_install_url()).query)
    assert query["client_id"] == ["1537535633038381190"]
    assert query["scope"] == ["applications.commands"]
    assert query["integration_type"] == ["1"]


def test_guild_install_urls_include_commands_and_requested_permissions() -> None:
    bot = _bot()
    guild = discord.Object(id=123)
    full = parse_qs(urlsplit(guild_install_url(bot, guild)).query)
    none = parse_qs(
        urlsplit(
            guild_install_url(bot, guild, permissions=discord.Permissions.none())
        ).query
    )

    assert full["guild_id"] == ["123"]
    assert full["scope"] == ["bot applications.commands"]
    assert full["permissions"] == [str(discord.Permissions(send_messages=True).value)]
    assert none["permissions"] == ["0"]


def test_handoff_notice_contains_deadline_transfer_and_three_links() -> None:
    notice = handoff_notice(_bot(), discord.Object(id=123))
    assert "September 9" in notice
    assert "transfer seamlessly" in notice
    assert notice.count("https://discord.com/oauth2/authorize?") == 3
    assert "Add Fishie to your profile" in notice
    assert "Add Fishie to this server" in notice
    assert "Add Fishie with no permissions" in notice


def test_handoff_notice_throttle_is_per_user_and_channel() -> None:
    async def scenario() -> None:
        throttle = HandoffNoticeThrottle(ttl=60)
        assert await throttle.allow(1, 2)
        assert not await throttle.allow(1, 2)
        assert await throttle.allow(1, 3)
        assert await throttle.allow(2, 2)

    asyncio.run(scenario())


def test_legacy_auto_download_only_sends_a_throttled_notice() -> None:
    class Channel:
        id = 123
        guild = SimpleNamespace(id=456)

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, content: str, **_kwargs: object) -> None:
            self.sent.append(content)

    channel = Channel()
    message = SimpleNamespace(
        channel=channel,
        guild=channel.guild,
        author=SimpleNamespace(id=789, bot=False),
        content="https://youtu.be/abcdefghijk",
    )
    bot = _bot(legacy=True)
    bot.db_cache = SimpleNamespace(auto_downloads={channel.id})
    event = object.__new__(AutoDownload)
    event.bot = cast("Fishie", bot)

    async def scenario() -> None:
        await event.auto_download(cast("discord.Message", message))
        await event.auto_download(cast("discord.Message", message))

    asyncio.run(scenario())
    assert len(channel.sent) == 1
    assert "September 9" in channel.sent[0]
