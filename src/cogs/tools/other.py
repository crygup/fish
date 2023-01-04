from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import discord
from bs4 import BeautifulSoup
from discord.ext import commands

from utils import (
    Pager,
    TenorUrlConverter,
    UrbanPageSource,
    emoji_extras,
    human_join,
    to_thread,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class OtherCommands(CogBase):
    @commands.command(name="steal", aliases=("clone",), extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def steal(self, ctx: Context, *, emojis: Optional[str]):
        ref = ctx.message.reference
        content = ctx.message.content

        if emojis is None:
            if ref is None:
                await ctx.send(
                    "You need to provide some emojis to steal, either reply to a message or give them as an argument."
                )
                return

            resolved = ref.resolved
            if (
                isinstance(resolved, discord.DeletedReferencedMessage)
                or resolved is None
            ):
                return

            content = resolved.content

        pattern = re.compile(r"<a?:[a-zA-Z0-9\_]{1,}:[0-9]{1,}>")
        results = pattern.findall(content)

        if len(results) == 0:
            await ctx.send("No emojis found.")
            return

        message = await ctx.send("Stealing emojis...")

        if message is None:
            return

        completed_emojis = []
        for result in results:
            emoji = await commands.PartialEmojiConverter().convert(ctx, result)

            if emoji is None:
                continue

            try:
                e = await ctx.guild.create_custom_emoji(
                    name=emoji.name, image=await emoji.read()
                )
                completed_emojis.append(str(e))
            except discord.HTTPException:
                pass

            await message.edit(
                content=f'Successfully stole {human_join(completed_emojis, final="and")} *({len(completed_emojis)}/{len(results)})*.'
            )

    @to_thread
    def get_real_url(self, text: str) -> str:
        scraper = BeautifulSoup(text, "html.parser")
        container = scraper.find(id="single-gif-container")
        if not container:
            raise ValueError("Couldn't find anything.")

        try:
            element = container.find("div").find("div").find("img")  # type: ignore
        except Exception as e:
            raise ValueError(f"Something went wrong. \n{e}")

        if element is None:
            raise ValueError(f"Something went wrong.")

        return element["src"]  # type: ignore

    @commands.command(name="tenor")
    async def tenor(self, ctx: commands.Context, url: str):
        """Gets the actual gif URL from a tenor link"""
        url = await TenorUrlConverter().convert(ctx, url)

        await ctx.send(f"Here is the real URL: {url}")

    @commands.command(name="urban")
    async def urban(self, ctx: Context, *, word: str):
        """Search for a word on urban

        Warning: could be NSFW"""

        url = "https://api.urbandictionary.com/v0/define"

        async with ctx.session.get(url, params={"term": word}) as resp:
            json = await resp.json()
            data: List[Dict[Any, Any]] = json.get("list", [])
            if not data:
                return await ctx.send("Nothing was found for this phrase.")

        p = UrbanPageSource(data, per_page=4)
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)
