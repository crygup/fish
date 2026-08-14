from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from playwright.async_api import async_playwright

from core import Cog
from extensions.context import Context
from utils import URLConverter, is_public_address, validate_public_url

if TYPE_CHECKING:
    from core import Fishie


class ScreenshotFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    delay: int = commands.flag(default=0, aliases=["d"])
    full_page: bool = commands.flag(default=False, aliases=["fp"])


class Screenshot(Cog):
    """Capture a public website as a PNG."""

    def __init__(self, bot: Fishie) -> None:
        self.bot = bot

    @commands.command(
        name="screenshot",
        aliases=("ss",),
        extras={"usage": "<website> [-delay/-d <seconds>] [-full-page/-fp]"},
    )
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def screenshot(
        self,
        ctx: Context,
        website: str = commands.param(
            description="The website's URL.", converter=URLConverter
        ),
        *,
        flags: ScreenshotFlags = commands.param(
            description="Flags to use while screenshotting."
        ),
    ) -> None:
        """Take a screenshot of a website.

        -# -delay/-d       Wait up to 10 seconds before taking the screenshot.
        -# -full-page/-fp  Capture the full page instead of the visible area.
        """
        if flags.delay < 0 or flags.delay > 10:
            raise commands.BadArgument(
                "Screenshot delay must be between 0 and 10 seconds."
            )
        await validate_public_url(website)
        async with self.bot.media_semaphore, ctx.typing():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    args=["--disable-dev-shm-usage", "--no-first-run"]
                )
                browser_context = None
                try:
                    browser_context = await browser.new_context(
                        locale="en-US", service_workers="block"
                    )
                    page = await browser_context.new_page()

                    # Playwright resolves hosts inside Chromium, independently
                    # from our initial URL check.  Inspect the peer reported by
                    # Chromium as well as revalidating every requested URL so a
                    # DNS rebinding response cannot be returned as a screenshot.
                    cdp = await browser_context.new_cdp_session(page)
                    await cdp.send("Network.enable")
                    unsafe_peer: str | None = None

                    def inspect_response(params: dict[str, object]) -> None:
                        nonlocal unsafe_peer
                        response = params.get("response")
                        if not isinstance(response, dict):
                            return
                        remote_ip = response.get("remoteIPAddress")
                        if isinstance(remote_ip, str) and remote_ip:
                            if not is_public_address(remote_ip):
                                unsafe_peer = remote_ip

                    cdp.on("Network.responseReceived", inspect_response)

                    async def guard_request(route) -> None:
                        if unsafe_peer is not None:
                            await route.abort("blockedbyclient")
                            return
                        try:
                            await validate_public_url(route.request.url)
                        except commands.CommandError:
                            await route.abort("blockedbyclient")
                        else:
                            await route.continue_()

                    await page.route("**/*", guard_request)
                    await page.goto(
                        website, wait_until="domcontentloaded", timeout=15_000
                    )
                    if unsafe_peer is not None:
                        raise commands.BadArgument(
                            "The website connected to a private network address."
                        )
                    await asyncio.sleep(flags.delay)
                    if unsafe_peer is not None:
                        raise commands.BadArgument(
                            "The website connected to a private network address."
                        )
                    if flags.full_page:
                        height = await page.evaluate(
                            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                        )
                        if int(height) > 12_000:
                            raise commands.BadArgument(
                                "The page is too tall for a full-page screenshot."
                            )
                    image = await page.screenshot(
                        type="png", timeout=15 * 1000, full_page=flags.full_page
                    )
                    if unsafe_peer is not None:
                        raise commands.BadArgument(
                            "The website connected to a private network address."
                        )
                    file = discord.File(BytesIO(image), filename="screenshot.png")
                finally:
                    if browser_context is not None:
                        await browser_context.close()
                    await browser.close()

        await ctx.send(file=file)
