from bot import Bot, Context
from discord.ext.commands import Cog


class DiscordBase(Cog):
    """Commands for discord itself"""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
