from __future__ import annotations

import argparse
import asyncio
import re
from io import BytesIO
import shlex
import time
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional

import discord
from bs4 import BeautifulSoup
from discord.ext import commands
from playwright.async_api import async_playwright

from utils import (
    Pager,
    TenorUrlConverter,
    UrbanPageSource,
    emoji_extras,
    human_join,
    to_thread,
    BlankException,
    get_size,
    get_wh,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class ArgumentsTyped:
    delay: int
    full: bool


def nsfw_channel(ctx: Context):
    if isinstance(ctx.channel, discord.Thread):
        if ctx.channel.parent:
            return not ctx.channel.parent.nsfw
        return True

    if ctx.author.guild_permissions.manage_guild:
        return False

    return not ctx.channel.nsfw


class UrlConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> str:
        if not argument.startswith("https://"):
            argument = f"https://{argument}"

        return argument


class OtherCommands(CogBase):
    @commands.command(name="screenshot", aliases=("ss",), extras={"nsfw": True})
    async def screenshot(
        self,
        ctx: Context,
        url: Annotated[str, UrlConverter],
        *,
        flags: Optional[str] = None,
    ):
        """Takes a screenshot of a website

        Flags:
            -delay: a number of how long the delay should be before taking a screenshot, maximum is 10
            -full: whether or not the screenshot should be a complete grab of the site or not (dont pass any args)

        """
        delay = 0
        full = False

        if flags:
            parser = Arguments(add_help=False, allow_abbrev=False)
            parser.add_argument("-delay", type=int, default=0)
            parser.add_argument("-full", action="store_true")

            try:
                _flags: ArgumentsTyped = parser.parse_args(
                    shlex.split(flags)
                )  # type:ignore
            except Exception as e:
                return await ctx.send(str(e))

            if _flags.delay:
                if _flags.delay > 10 or _flags.delay < 0:
                    raise BlankException(
                        "Delay must be greater than 0 or less than 10 seconds."
                    )
                delay = _flags.delay

            if _flags.full:
                full = True

        async with ctx.typing():
            start = time.perf_counter()
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page()
                await page.goto(url)
                await asyncio.sleep(delay)
                screenshot_bytes = BytesIO(
                    await page.screenshot(full_page=full, timeout=15 * 1000, type="png")
                )
                end = time.perf_counter()

        embed = discord.Embed(color=ctx.bot.embedcolor, description=f"url: {url}")
        width, height = await get_wh(screenshot_bytes)
        size = await get_size(screenshot_bytes)

        embed.set_footer(
            text=f"{width}x{height}, {size}, took {round(end-start, 2)} seconds"
        )
        embed.set_image(url=f"attachment://screenshot.png")
        embed.set_author(
            name=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url
        )

        await ctx.send(
            file=discord.File(screenshot_bytes, filename=f"screenshot.png"),
            embed=embed,
        )

    @commands.command(name="steal", aliases=("clone",), extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def steal(self, ctx: Context, *, emojis: Optional[str]):
        ref = ctx.message.reference
        content = ctx.message.content

        if emojis is None:
            if ref is None:
                return await ctx.send(
                    "You need to provide some emojis to steal, either reply to a message or give them as an argument."
                )

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
    async def tenor(self, ctx: commands.Context, url: TenorUrlConverter):
        """Gets the actual gif URL from a tenor link"""

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
