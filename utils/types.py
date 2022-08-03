import discord
from typing import Union

GuildChannel = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
    discord.Thread,
]
