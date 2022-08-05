import discord
from bot import Bot
from discord.ext import commands


async def setup(bot: Bot):
    await bot.add_cog(TableEvents(bot))


class TableEvents(commands.Cog, name="table_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    #@commands.Cog.listener("on_member_update")
    #async def fuck_you_ducki(self, before: discord.Member, after: discord.Member):
    #    if before.guild.id != 848507662437449750:
    #        return
#
    #    if before.id != 651454696208465941:
    #        return
#
    #    if after.nick != "#1 liz fan":
    #        await after.edit(nick="#1 liz fan")
#
    #@commands.Cog.listener("on_member_join")
    #async def fuck_you_ducki_2(self, member: discord.Member):
    #    if member.guild.id != 848507662437449750:
    #        return
#
    #    if member.id != 651454696208465941:
    #        return
#
    #    await member.edit(nick="#1 liz fan")
