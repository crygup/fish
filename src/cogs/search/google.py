from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils import (
    GoogleImageData,
    GoogleImagePageSource,
    Pager,
    response_checker,
    BlankException,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class GoogleCommands(CogBase):
    @commands.hybrid_command(
        name="google", aliases=("g",), description="Search something on the web"
    )
    @app_commands.describe(query="What are you searching for?")
    async def google(self, ctx: Context, *, query: str):
        """Search something on the web"""
        url = f"https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "cx": self.bot.config["keys"]["google-id"],
            "q": query,
            "key": self.bot.config["keys"]["google-search"],
        }
        await ctx.trigger_typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            embed = discord.Embed(color=self.bot.embedcolor)
            embed.set_footer(
                text=f"About {data['searchInformation']['formattedTotalResults']} results ({data['searchInformation']['formattedSearchTime']} seconds)"
            )

            embed.title = f"Google Search - {query}"[:256]

            text = ""
            items = data["items"]

            added = 0
            for item in items:
                if added == 5:
                    break
                try:
                    text += f"[{item['title']}]({item['link']})\n{item['snippet']}\n\n"
                    added += 1
                except KeyError:
                    continue
            embed.description = text

        await ctx.send(embed=embed)

    @commands.command(name="image", aliases=("img",))
    async def google_image(self, ctx: Context, *, query: str):
        url = f"https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "cx": self.bot.config["keys"]["google-id"],
            "q": query,
            "key": self.bot.config["keys"]["google-search"],
            "searchType": "image",
            "safe": "off"
            if not isinstance(ctx.channel, discord.Thread) and ctx.channel.nsfw
            else "high",
        }

        await ctx.trigger_typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            results = await r.json()

            items = results.get("items")

            if items is None:
                raise BlankException("No search results.")

            entries = [
                GoogleImageData(
                    image_url=data["link"],
                    url=data["image"]["contextLink"],
                    snippet=data["snippet"],
                    query=query,
                    author=ctx.author,
                )
                for data in results["items"]
            ]

        pager = Pager(GoogleImagePageSource(entries), ctx=ctx)
        await pager.start(ctx)
