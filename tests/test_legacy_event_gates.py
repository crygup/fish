from __future__ import annotations

import asyncio
from types import SimpleNamespace

# Test doubles supply only the Discord/service fields exercised by each test.
from typing import cast

import discord

from extensions.events.auto_reactions import Reactions as AutoReactions
from extensions.events.boards import BoardEvents
from extensions.events.corn import CornReacts
from extensions.events.pokemon import Pokemon
from extensions.events.reactions import ReactionLogs
from extensions.events.statuses import StatusCog
from extensions.events.xp import XPCog
from extensions.logging.avatars import Avatars
from extensions.logging.guild import Guild
from extensions.logging.users import User
from extensions.moderation.custom_roles import CustomRoles
from extensions.moderation.honeypot import Honeypot
from extensions.moderation.protection import Protection
from extensions.moderation.snipe import Snipe
from extensions.tools.reputation import Reputation


def _legacy_cog(cls):
    cog = object.__new__(cls)
    cog.bot = SimpleNamespace(
        is_legacy_bot=True,
        is_new_bot=False,
    )
    return cog


def _assert_returns(coro):
    asyncio.run(coro)


def test_tracking_and_automation_events_are_read_only_on_legacy() -> None:
    """The retiring process must not write or react while both apps overlap."""

    message = SimpleNamespace()
    payload = SimpleNamespace()
    member = SimpleNamespace()
    guild = SimpleNamespace()

    _assert_returns(
        XPCog.xp_message(_legacy_cog(XPCog), cast("discord.Message", message))
    )
    _assert_returns(
        ReactionLogs.on_reaction_log(
            _legacy_cog(ReactionLogs), cast("discord.RawReactionActionEvent", payload)
        )
    )
    _assert_returns(
        CornReacts.on_corn_react(
            _legacy_cog(CornReacts), cast("discord.RawReactionActionEvent", payload)
        )
    )
    _assert_returns(
        AutoReactions.reaction_message(
            _legacy_cog(AutoReactions), cast("discord.Message", message)
        )
    )
    _assert_returns(
        Pokemon.on_pokemon(_legacy_cog(Pokemon), cast("discord.Message", message))
    )
    _assert_returns(
        StatusCog._on_presence_update(
            _legacy_cog(StatusCog),
            cast("discord.Member", member),
            cast("discord.Member", member),
        )
    )
    _assert_returns(
        User.username_update(
            _legacy_cog(User),
            cast("discord.User", member),
            cast("discord.User", member),
        )
    )
    _assert_returns(
        Avatars.user_update(
            _legacy_cog(Avatars),
            cast("discord.User", member),
            cast("discord.User", member),
        )
    )
    _assert_returns(
        Guild.icon_update(
            _legacy_cog(Guild),
            cast("discord.Guild", guild),
            cast("discord.Guild", guild),
        )
    )


def test_moderation_and_board_events_are_read_only_on_legacy() -> None:
    message = SimpleNamespace()
    payload = SimpleNamespace()
    member = SimpleNamespace()
    guild = SimpleNamespace()

    _assert_returns(
        Snipe.snipe_message(_legacy_cog(Snipe), cast("discord.Message", message))
    )
    _assert_returns(
        Honeypot.honeypot_on_message(
            _legacy_cog(Honeypot), cast("discord.Message", message)
        )
    )
    _assert_returns(
        Protection.protection_invite_message(
            _legacy_cog(Protection), cast("discord.Message", message)
        )
    )
    _assert_returns(
        CustomRoles.custom_role_member_join(
            _legacy_cog(CustomRoles), cast("discord.Member", member)
        )
    )
    _assert_returns(
        BoardEvents.on_board_reaction_add(
            _legacy_cog(BoardEvents), cast("discord.RawReactionActionEvent", payload)
        )
    )
    _assert_returns(
        BoardEvents.on_board_emoji_delete(
            _legacy_cog(BoardEvents), cast("discord.Guild", guild), [], []
        )
    )
    _assert_returns(
        Reputation.on_tatsu_reputation(
            _legacy_cog(Reputation), cast("discord.Message", message)
        )
    )
