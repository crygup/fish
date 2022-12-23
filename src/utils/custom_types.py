from typing import Union

import discord

GuildChannel = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
    discord.Thread,
]
