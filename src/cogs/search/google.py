import discord
from bot import Context
from discord.ext import commands

from ._base import CogBase


class GoogleCommands(CogBase):
    @commands.group(name="google", aliases=("g",))
    async def google(self, ctx: Context, *, query: str):
        url = f"https://customsearch.googleapis.com/customsearch/v1?cx={self.bot.config['keys']['google-id']}&q={query}&key={self.bot.config['keys']['google-search']}"

        await ctx.trigger_typing()
        async with self.bot.session.get(url) as r:
            data = await r.json()

            embed = discord.Embed(color=self.bot.embedcolor)
            embed.set_footer(
                text=f"About {data['searchInformation']['formattedTotalResults']} results ({data['searchInformation']['formattedSearchTime']} seconds)"
            )

            embed.title = f"Google Search - {query}"[:256]

            text = ""
            items = data["items"][:5]

            for item in items:
                text += f"[{item['title']}]({item['link']})\n{item['snippet']}\n\n"

            embed.description = text

        await ctx.send(embed=embed)
