import discord
from bot import Bot

from .afk import AfkCommands
from .downloads import DownloadCommands
from .other import OtherCommands
from .tags import TagCommands
from .money import MoneyCommands
from .feed import FeedCommands


class Tools(
    TagCommands,
    DownloadCommands,
    OtherCommands,
    AfkCommands,
    MoneyCommands,
    # FeedCommands, # it's not working at the moment due to random shut downs of the live twitter client so until that's fixed it's shut down
    name="tools",
):
    """Useful tools"""

    def __init__(self, bot: Bot):
        super().__init__(bot)
        self.currently_downloading: list[str] = []

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f528")


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))
