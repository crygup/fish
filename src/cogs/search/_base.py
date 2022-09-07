from discord.ext.commands import Cog

from bot import Bot


class CogBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
