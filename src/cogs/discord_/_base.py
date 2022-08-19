from bot import Bot, Context
from discord.ext.commands import Cog


class DiscordBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
