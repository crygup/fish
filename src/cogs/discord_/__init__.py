import discord

from bot import Bot

from .info import InfoCommands
from .other import OtherCommands
from .search import SearchCommand
from .user import UserCommands


class Discord(SearchCommand, InfoCommands, UserCommands, OtherCommands, name="discord"):
    """Commands for discord itself"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="discord", id=1006848754944593921)


async def setup(bot: Bot):
    await bot.add_cog(Discord(bot))
