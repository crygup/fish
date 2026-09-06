from __future__ import annotations

from typing import TYPE_CHECKING, List, Union

import discord
from discord.ext import commands

# The support/operations guild hosts Fishie's private review, upload, and
# logging channels.  It is deliberately kept out of user-facing event
# automation and history listeners; the dedicated review commands still
# address these channels explicitly.
OPERATIONAL_GUILD_ID = 939497177821110272


def is_operational_guild(value: object | None) -> bool:
    """Return whether *value* identifies Fishie's private operations guild.

    ``value`` may be a guild-like object, channel/member object, or an integer
    ID.  Keeping this predicate in the shared cog module lets event cogs use
    the same guard without importing one another (and without accidentally
    coupling review/upload code to the event filter).
    """

    if value is None:
        return False
    if isinstance(value, int):
        return value == OPERATIONAL_GUILD_ID
    guild_id = getattr(value, "guild_id", None)
    if guild_id is None:
        guild = getattr(value, "guild", None)
        guild_id = getattr(guild, "id", None)
    if guild_id is None:
        guild_id = getattr(value, "id", None)
    if guild_id is None:
        return False
    try:
        return int(guild_id) == OPERATIONAL_GUILD_ID
    except (TypeError, ValueError):
        return False


if TYPE_CHECKING:
    from .bot import Fishie


class Cog(commands.Cog):
    emoji: Union[discord.Emoji, discord.PartialEmoji]
    aliases: List[str] = []
    bot: Fishie
    hidden: bool = False
