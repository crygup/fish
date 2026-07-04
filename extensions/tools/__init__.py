from __future__ import annotations

import asyncio
import datetime
import json
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import discord
from discord.ext import commands
from discord.utils import escape_markdown
from playwright.async_api import async_playwright
from discord import app_commands
from extensions.context import Context
from utils import (
    Pager,
    SimplePages,
    TenorUrlConverter,
    UrbanPageSource,
    URLConverter,
    get_or_fetch_user,
    AuthorView,
    ROBLOX_ASSET_RE,
    to_image,
)

from .command_stats import CommandStats
from .downloads import Downloads
from .google import Google
from .purge import PurgeCog
from .reminders import Reminder
from .spotify import Spotify

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

param = commands.param


class ScreenshotFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    delay: int = commands.flag(default=0, aliases=["d"])
    full_page: bool = commands.flag(default=False, aliases=["fp"])


class Tools(Downloads, Reminder, Google, Spotify, PurgeCog, CommandStats):
    """Quality of life tools"""

    emoji = discord.PartialEmoji(name="\U0001f6e0")

    async def self_send_asset(self, ctx: Context, url: str, extra: dict, *, cached_at: Optional[datetime.datetime] = None) -> None:
        image = await to_image(self.bot.session, url)
        file = discord.File(image, filename=f"{extra.get('name', 'template')[:32]}.png")

        e = discord.Embed(title=str(extra.get("name", "Unknown")).title(), color=self.bot.embedcolor)
        e.set_image(url=f"attachment://{file.filename}")

        creator_name = extra.get("creator", "Unknown")
        creator_id = extra.get("creator_id")
        creator_type = extra.get("creator_type", "User")
        if creator_id:
            if creator_type == "Group":
                link = f"https://www.roblox.com/groups/{creator_id}/"
            else:
                link = f"https://www.roblox.com/users/{creator_id}/profile"
            e.add_field(name="Made by", value=f"[{creator_name}]({link})")
        else:
            e.add_field(name="Made by", value=creator_name)

        e.set_footer(text="Saved")
        if cached_at:
            e.timestamp = cached_at

        await ctx.send(embed=e, file=file)

    cyrillic_letters = {
        "A": "А",
        "B": "В",
        "C": "С",
        "D": "‌**D**‌",
        "E": "Е",
        "F": "‌**F**‌",
        "G": "Ԍ",
        "H": "Η",
        "I": "І",
        "J": "Ј",
        "K": "Κ",
        "L": "Ꮮ",
        "M": "Μ",
        "N": "Ν",
        "O": "Ο",
        "P": "Ρ",
        "Q": "‌**Q**‌",
        "R": "Ꮢ",
        "S": "Ѕ",
        "T": "Τ",
        "U": "Ս",
        "V": "Ꮩ",
        "W": "Ꮃ",
        "X": "Χ",
        "Y": "Υ",
        "Z": "Ζ",
        "a": "а",
        "b": "‌**b**‌",
        "c": "ϲ",
        "d": "ԁ",
        "e": "е",
        "f": "‌**f**‌",
        "g": "ɡ",
        "h": "һ",
        "i": "і",
        "j": "ϳ",
        "k": "‌**k**‌",
        "l": "ⅼ",
        "m": "‌**m**‌",
        "n": "‌**n**‌",
        "o": "ο",
        "p": "р",
        "q": "‌**q**‌",
        "r": "‌**r**‌",
        "s": "ѕ",
        "t": "‌**t**‌",
        "u": "υ",
        "v": "ν",
        "w": "‌**w**‌",
        "x": "х",
        "y": "у",
        "z": "‌**z**‌",
        " ": " ",
    }

    @commands.command(
        name="cyrillic",
        aliases=[
            "cryllic",
        ],
    )
    async def cyrillic(self, ctx: Context, *, words: str):
        words = discord.utils.escape_markdown(words, ignore_links=False)
        all_letters = [letter for word in words for letter in word]
        new_words = []

        for letter in all_letters:
            try:
                new_words.append(self.cyrillic_letters[letter])
            except:
                new_words.append(f"‌**{letter}**‌")
        fmt = "".join(new_words)
        await ctx.send(fmt[:2000])

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

    @commands.hybrid_command(name="xp")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def xp(self, ctx: Context, *, user: discord.User = commands.Author):
        """Check the XP you have."""
        xp: Optional[int] = await self.bot.pool.fetchval(
            "SELECT xp FROM message_xp WHERE user_id = $1", user.id
        )

        if not bool(xp):
            raise commands.BadArgument("This user has no recorded XP")

        await ctx.send(f"{user} has {xp:,} XP")

    async def lb_name(self, user_id: int) -> discord.User | int:
        try:
            return await get_or_fetch_user(self.bot, user_id)
        except:
            return user_id

    @commands.hybrid_command(name="leaderboard", aliases=("lb",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def leaderboard(self, ctx: Context):
        """Check the global XP leaderboard"""
        xp = await self.bot.pool.fetch(
            "SELECT user_id, xp FROM message_xp ORDER BY xp DESC LIMIT 100",
        )

        if not bool(xp):
            raise commands.BadArgument("No data found")

        data: Data[int, int] = dict(xp)  # type: ignore

        data = [
            escape_markdown(f"{await self.lb_name(user_id)}: {xp:,}")
            for user_id, xp in data.items()
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Gloabl ranks"
        await pages.start(ctx)

    @commands.command(name="solve")
    async def solve(self, ctx: Context):
        """Solve a Pokétwo hint by replying to the hint message"""
        ref = ctx.message.reference

        if not ref or not isinstance(ref.resolved, discord.Message):
            raise commands.BadArgument("Reply to a Pokétwo hint message to solve it.")

        events = self.bot.events
        if not events:
            raise commands.BadArgument("Events cog is not loaded.")

        try:
            found = events.auto_solve(ref.resolved.content)
        except commands.BadArgument:
            raise commands.BadArgument("Could not find a Pokémon hint in that message.")

        if not found:
            await ctx.send("No matching Pokémon found.")
            return

        for name in found:
            await events._log_solve(ctx.author.id, name, "command", ctx.guild.id if ctx.guild else None)

        await ctx.send("\n".join(found))

    @commands.hybrid_group("roblox", hidden=True)
    async def roblox_group(self, ctx: Context):
        """Roblox related commands."""
        await ctx.send_help(ctx.command)

    @roblox_group.command("asset")
    async def roblox_asset(self, ctx: Context, asset_id: str):
        """Get the 2D clothing template for a Roblox classic t-shirt, shirt, or pants"""
        async with ctx.typing():
            try:
                aid = int(asset_id)
            except ValueError:
                result = ROBLOX_ASSET_RE.search(asset_id)
                if not result or not result.group(4):
                    raise commands.CommandError(
                        "Could not find an asset ID in that URL. Provide a Roblox catalog URL or a raw asset ID."
                    )
                aid = int(result.group(4))

            cached = self.bot.cached_roblox_templates.get(aid)
            if cached and len(cached) == 3:
                await self.self_send_asset(ctx, cached[0], cached[1], cached_at=cached[2])
                return
            elif cached:
                del self.bot.cached_roblox_templates[aid]

            row = await self.bot.pool.fetchrow(
                "SELECT image_url, extra, cached_at FROM roblox_templates WHERE asset_id = $1", aid
            )
            if row:
                extra = json.loads(row["extra"]) if isinstance(row["extra"], str) else (row["extra"] or {})
                extra.setdefault("name", "Unknown")
                self.bot.cached_roblox_templates[aid] = (row["image_url"], extra, row["cached_at"])
                await self.self_send_asset(ctx, row["image_url"], extra, cached_at=row["cached_at"])
                return

            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "--max-time", "10",
                f"https://economy.roblox.com/v2/assets/{aid}/details",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                raise commands.CommandError(
                    f"Failed to reach Roblox (curl exit {proc.returncode}). Try again later."
                )

            details = json.loads(stdout)
            asset_type = details.get("AssetTypeId")
            if asset_type not in (2, 11, 12):
                raise commands.CommandError(
                    f"Asset {aid} is type {asset_type}, not a classic t-shirt (2), shirt (11), or pants (12)."
                )

            creator = details.get("Creator", {})
            creator_name = creator.get("Name", "Unknown")
            creator_id = creator.get("Id")
            creator_type = creator.get("CreatorType", "User")
            extra = {
                "name": details.get("Name", "Unknown"),
                "creator": creator_name,
                "creator_id": creator_id,
                "creator_type": creator_type,
                "type": asset_type,
            }
            if creator_type == "Group":
                extra["owner"] = creator_name

            cookie = self.bot.config["keys"].get("roblox", "")
            cookie_header = f"Cookie: .ROBLOSECURITY={cookie}" if cookie else ""
            curl_args = [
                "curl", "-s", "--compressed", "--max-time", "10", "-L",
                f"https://assetdelivery.roblox.com/v1/asset?id={aid}",
            ]
            if cookie_header:
                curl_args[1:1] = ["-H", cookie_header]

            proc = await asyncio.create_subprocess_exec(
                *curl_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                raise commands.CommandError(
                    f"Failed to fetch asset XML (curl exit {proc.returncode}). Try again later."
                )

            if stdout.startswith(b"{"):
                await ctx.send(
                    "Something went wrong while trying to get the asset, please try again later, we may be rate limited."
                )
                return

            try:
                root = ET.fromstring(stdout)
            except ET.ParseError:
                raise commands.CommandError(
                    "Roblox returned invalid XML. The asset may not be a classic t-shirt, shirt, or pants."
                )

            url_elem = root.find(".//Item/Properties/Content/url")
            if url_elem is None or not url_elem.text:
                raise commands.CommandError(
                    "No texture reference found in the asset XML."
                )

            texture_match = re.search(r"id=(\d+)", url_elem.text)
            if not texture_match:
                raise commands.CommandError(
                    "Could not parse the texture ID from the asset XML."
                )

            texture_id = texture_match.group(1)

            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "--max-time", "10",
                f"https://thumbnails.roblox.com/v1/assets?assetIds={texture_id}&size=420x420&format=Png",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                raise commands.CommandError(
                    f"Failed to fetch template thumbnail (curl exit {proc.returncode}). Try again later."
                )

            thumb_data = json.loads(stdout)
            image_url = thumb_data["data"][0]["imageUrl"]

            await self.bot.pool.execute(
                "INSERT INTO roblox_templates (asset_id, image_url, item_name, extra) VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (asset_id) DO UPDATE SET image_url = $2, item_name = $3, extra = $4::jsonb, cached_at = now() at time zone 'utc'",
                aid, image_url, extra["name"], json.dumps(extra),
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            self.bot.cached_roblox_templates[aid] = (image_url, extra, now)

            await self.self_send_asset(ctx, image_url, extra, cached_at=now)


async def setup(bot: Fishie):
    await bot.add_cog(Tools(bot))
