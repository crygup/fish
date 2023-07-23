from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING, List, Dict, Any

import discord
from discord.ext import commands
from playwright.async_api import async_playwright

from utils import URLConverter, TenorUrlConverter, UrbanPageSource, Pager

from .downloads import Downloads
from .google import Google
from .reminders import Reminder
from .spotify import Spotify

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

param = commands.param


class ScreenshotFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    delay: int = commands.flag(default=0, aliases=["d"])
    full_page: bool = commands.flag(default=False, aliases=["fp"])


class Tools(Downloads, Reminder, Google, Spotify):
    """Quality of life tools"""

    emoji = discord.PartialEmoji(name="\U0001f6e0")

    @commands.command(name="screenshot", aliases=("ss",))
    async def screenshot(
        self,
        ctx: Context,
        website: str = param(description="The website's url.", converter=URLConverter),
        *,
        flags: ScreenshotFlags = param(
            description="Flags to use while screenshotting."
        ),
    ):
        """Screenshot a website from the internet"""
        async with ctx.typing():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(locale="en-US")
                await page.goto(website)
                await asyncio.sleep(flags.delay)
                file = discord.File(
                    BytesIO(
                        await page.screenshot(
                            type="png", timeout=15 * 1000, full_page=flags.full_page
                        )
                    ),
                    filename="screenshot.png",
                )

        await ctx.send(file=file)

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
                raise commands.BadArgument("Nothing was found for this phrase.")

        p = UrbanPageSource(data, per_page=4)
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)


async def setup(bot: Fishie):
    await bot.add_cog(Tools(bot))
