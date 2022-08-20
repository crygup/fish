import discord
from bot import Bot, Context

from .downloads import DownloadCommands
from .other import OtherCommands
from .tags import TagCommands


class Tools(TagCommands, DownloadCommands, OtherCommands, name="tools"):
    """Useful tools"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f528")


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))
