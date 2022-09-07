import discord

from bot import Bot

from .waifu import Waifus


class Image(Waifus, name="image"):
    """Image related stuff"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f3a8")


async def setup(bot: Bot):
    await bot.add_cog(Image(bot))
