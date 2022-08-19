import discord
from bot import Bot, Context

from .info import InfoCommands
from .search import SearchCommand
from .user import UserCommands
from .other import OtherCommands


class Discord(SearchCommand, InfoCommands, UserCommands, OtherCommands, name="discord"):
    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="discord", id=1006848754944593921)


async def setup(bot: Bot):
    await bot.add_cog(Discord(bot))
