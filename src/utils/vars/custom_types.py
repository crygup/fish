from typing import Optional, ParamSpec, TypeAlias, TypeVar, Union

import discord

T = TypeVar("T")
P = ParamSpec("P")

GuildChannel: TypeAlias = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
    discord.Thread,
]

Channel: TypeAlias = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
    discord.ForumChannel,
    discord.Thread,
]

Argument: TypeAlias = Optional[
    discord.Member
    | discord.User
    | discord.PartialEmoji
    | discord.Role
    | discord.Message
    | str
]

NonOptionalArgument: TypeAlias = Union[
    discord.Member,
    discord.User,
    discord.PartialEmoji,
    discord.Role,
    discord.Message,
    str,
]

InfoArgument: TypeAlias = Optional[
    discord.Member | discord.User | discord.Role | discord.Guild | GuildChannel
]
