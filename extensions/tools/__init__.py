from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from playwright.async_api import async_playwright

from .downloads import Downloads

if TYPE_CHECKING:
    from core import Fishie


class ScreenshotFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    delay: int = commands.flag(default=0, aliases=["d"])
    full_page: bool = commands.flag(default=False, aliases=["fp"])


class Tools(Downloads):
    emoji = discord.PartialEmoji(name="\U0001f6e0")

    @commands.command(name="screenshot", aliases=("ss",))
    async def screenshot(
        self, ctx: commands.Context[Fishie], website: str, *, flags: ScreenshotFlags
    ):
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


async def setup(bot: Fishie):
    await bot.add_cog(Tools())
