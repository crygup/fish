import discord
from discord.ext import commands
from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(Misc(bot))


class Misc(commands.Cog, name="misc"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name='first_message', aliases=('fm','oldest'))
    async def first_message(self, ctx: commands.Context, *, member: discord.Member = commands.Author):
        """Sends a url to the first message from a member in a channel.

        If the url seems to lead nowhere the message might've been deleted."""
        if ctx.guild is None:
            return

        record = await self.bot.pool.fetchrow("SELECT * FROM message_logs WHERE author_id = $1 AND guild_id = $2 AND channel_id = $3 ORDER BY created_at ASC LIMIT 1", member.id, ctx.guild.id, ctx.channel.id)
        if record is None:
            await ctx.send(f"It seems I have no records for {str(member)} in this channel")
            return

        url = f'https://discordapp.com/channels/{record["guild_id"]}/{record["channel_id"]}/{record["message_id"]}'
        await ctx.send(url)