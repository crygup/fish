import discord
from bot import Bot
from discord.ext import commands
from utils import GuildContext


async def setup(bot: Bot):
    await bot.add_cog(Egg(bot))


class Egg(commands.Cog, name="egg"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(hidden=True)
    @commands.is_owner()
    async def invite(self, ctx: GuildContext, users: int = 1):
        if ctx.guild.id != 1002190975965331477:
            return

        channel = self.bot.get_channel(1002262666569592903)

        if channel is None or not isinstance(channel, discord.TextChannel):
            return

        invite = await channel.create_invite(max_uses=users)

        await ctx.author.send(f"Here is the invite: {invite.url}")
