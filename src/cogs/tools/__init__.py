import discord
from bot import Bot, Context

from .downloads import DownloadCommands
from .other import OtherCommands
from .tags import TagCommands
from .afk import AfkCommands


class Tools(TagCommands, DownloadCommands, OtherCommands, AfkCommands, name="tools"):
    def __init__(self, bot: Bot):
        super().__init__(bot)
        self.currently_downloading: list[str] = []

    """Useful tools"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f528")


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))
