from __future__ import annotations

import asyncio
import datetime
import json
import re
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast
from urllib.parse import urljoin

import aiohttp
import discord
from defusedxml import ElementTree as ET
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown
from playwright.async_api import async_playwright

from extensions.context import Context
from utils import (
    ROBLOX_ASSET_RE,
    AuthorView,
    FieldPageSource,
    Pager,
    SimplePages,
    TenorUrlConverter,
    UrbanPageSource,
    URLConverter,
    get_or_fetch_user,
    plural,
    read_bounded_response,
    to_image,
    validate_connected_peer,
    validate_public_url,
)

from .calculator import Calculator
from .command_stats import CommandStats
from .downloads import Downloads
from .google import Google
from .letterboxd import Letterboxd
from .purge import PurgeCog
from .reminders import Reminder
from .tags import Tags

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

ROBLOX_TIMEOUT = aiohttp.ClientTimeout(total=10)

param = commands.param


class ScreenshotFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    delay: int = commands.flag(default=0, aliases=["d"])
    full_page: bool = commands.flag(default=False, aliases=["fp"])


class EndView(discord.ui.View):
    def __init__(self, cog, original_ctx):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = original_ctx

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.blurple)
    async def play_again(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message(
                "This isn't your game.", ephemeral=True
            )
        await interaction.response.defer()
        await self.cog.pokepractice(self.ctx)


class GameView(discord.ui.View):
    def __init__(self, ctx: Context, give_up_flag: asyncio.Event):
        super().__init__(timeout=None)
        self.ctx = ctx
        self.give_up_flag = give_up_flag

    @discord.ui.button(label="Give Up", style=discord.ButtonStyle.grey)
    async def give_up(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message(
                "This isn't your game.", ephemeral=True
            )
        await interaction.response.defer()
        self.give_up_flag.set()


class Tools(
    Tags,
    Downloads,
    Reminder,
    Google,
    PurgeCog,
    CommandStats,
    Letterboxd,
    Calculator,
):
    """Quality of life tools"""

    emoji = discord.PartialEmoji(name="\U0001f6e0")

    def __init__(self, bot: Fishie) -> None:
        super().__init__(bot)
        self._highlight_activity: dict[tuple[int, int], float] = {}
        self._highlight_cache: dict[int, list[tuple[int, str, re.Pattern[str]]]] = {}
        self._highlight_tasks: set[asyncio.Task[None]] = set()

    def cog_unload(self) -> None:
        for task in self._highlight_tasks:
            task.cancel()
        super().cog_unload()

    async def _get_highlights(
        self, guild_id: int
    ) -> list[tuple[int, str, re.Pattern[str]]]:
        cached = self._highlight_cache.get(guild_id)
        if cached is not None:
            return cached

        rows = await self.bot.pool.fetch(
            "SELECT user_id, word, word_normalized FROM highlights "
            "WHERE guild_id = $1",
            guild_id,
        )
        highlights = []
        for row in rows:
            word = str(row["word"])
            normalized = str(row["word_normalized"])
            if not normalized:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)")
            highlights.append((int(row["user_id"]), word, pattern))

        self._highlight_cache[guild_id] = highlights
        return highlights

    def _invalidate_highlights(self, guild_id: int) -> None:
        self._highlight_cache.pop(guild_id, None)

    async def _send_highlight_list(self, ctx: Context) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        highlights = await self._get_highlights(ctx.guild.id)
        mine = sorted(
            (word for user_id, word, _ in highlights if user_id == ctx.author.id),
            key=str.casefold,
        )
        description = "\n".join(f"• {escape_markdown(word)}" for word in mine)
        if not description:
            description = "You do not have any highlights in this server."
        elif len(description) > 4096:
            description = description[:4093].rsplit("\n", 1)[0] + "\n…"

        embed = discord.Embed(
            title=f"Your highlights in {ctx.guild.name}",
            description=description,
            color=self.bot.embedcolor,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _resolve_guild(bot: Fishie, value: str) -> discord.Guild | None:
        value = value.strip()
        if value.isdigit():
            return bot.get_guild(int(value))

        matches = [
            guild for guild in bot.guilds if guild.name.casefold() == value.casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def _highlight_task_done(self, task: asyncio.Task[None]) -> None:
        self._highlight_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            self.bot.logger.error("Highlight notification failed", exc_info=error)

    async def _send_highlight_notification(
        self,
        message: discord.Message,
        user_id: int,
        words: list[str],
        triggered_at: float,
    ) -> None:
        await asyncio.sleep(15)

        guild = message.guild
        if guild is None:
            return

        activity = self._highlight_activity.get((guild.id, user_id), 0.0)
        if activity >= triggered_at:
            return

        member = guild.get_member(user_id)
        if member is None:
            return

        highlighted = ", ".join(
            f"**{escape_markdown(word)}**"
            for word in sorted(set(words), key=str.casefold)
        )
        guild_name = discord.utils.escape_mentions(escape_markdown(guild.name))
        channel = getattr(message.channel, "mention", "this channel")
        content = (
            f"Your highlight {highlighted} was mentioned in **{guild_name}** "
            f"in {channel}. [Jump to the message]({message.jump_url})"
        )

        try:
            await member.send(
                content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            self.bot.logger.debug(
                "Could not DM highlight notification to user %s", user_id
            )
        except discord.HTTPException:
            self.bot.logger.warning(
                "Failed to send highlight notification to user %s", user_id
            )

    @commands.Cog.listener("on_message")
    async def _highlight_on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return

        now = asyncio.get_running_loop().time()
        self._highlight_activity[(guild.id, message.author.id)] = now
        if len(self._highlight_activity) > 10_000:
            cutoff = now - 900
            self._highlight_activity = {
                key: timestamp
                for key, timestamp in self._highlight_activity.items()
                if timestamp >= cutoff
            }
        highlights = await self._get_highlights(guild.id)
        content = message.content.casefold()
        if not content:
            return

        matches: dict[int, list[str]] = {}
        for user_id, word, pattern in highlights:
            if pattern.search(content):
                matches.setdefault(user_id, []).append(word)

        for user_id, words in matches.items():
            task = asyncio.create_task(
                self._send_highlight_notification(message, user_id, words, now)
            )
            self._highlight_tasks.add(task)
            task.add_done_callback(self._highlight_task_done)

    @commands.Cog.listener("on_typing")
    async def _highlight_on_typing(
        self,
        channel: discord.abc.Messageable,
        user: discord.User | discord.Member,
        when: datetime.datetime,
    ) -> None:
        guild = getattr(channel, "guild", None)
        if guild is not None and not user.bot:
            self._highlight_activity[(guild.id, user.id)] = (
                asyncio.get_running_loop().time()
            )

    @commands.hybrid_group(name="highlight", aliases=("hl",))
    @commands.guild_only()
    async def highlight(self, ctx: Context) -> None:
        """Manage words that should notify you when mentioned in this server."""
        await ctx.send_help(ctx.command)

    @highlight.command(name="list")
    @commands.guild_only()
    async def highlight_list(self, ctx: Context) -> None:
        """Show your highlights in this server."""
        await self._send_highlight_list(ctx)

    @highlight.command(name="add", aliases=("set",))
    @commands.guild_only()
    async def highlight_add(
        self,
        ctx: Context,
        *,
        word: str = commands.param(description="The word or phrase to highlight."),
    ) -> None:
        """Add a word or phrase to your highlights."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        word = " ".join(word.split())
        if not word:
            raise commands.BadArgument("The highlight cannot be empty.")
        if len(word) > 100:
            raise commands.BadArgument("Highlights must be 100 characters or fewer.")

        normalized = word.casefold()
        row = await self.bot.pool.fetchrow(
            "INSERT INTO highlights (user_id, guild_id, word, word_normalized) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, guild_id, word_normalized) DO NOTHING "
            "RETURNING word",
            ctx.author.id,
            ctx.guild.id,
            word,
            normalized,
        )
        self._invalidate_highlights(ctx.guild.id)
        if row is None:
            await ctx.send(
                f"You are already highlighting **{escape_markdown(word)}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"Now highlighting **{escape_markdown(word)}** in this server.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @highlight.command(name="remove", aliases=("delete", "del", "unset"))
    @commands.guild_only()
    async def highlight_remove(
        self,
        ctx: Context,
        *,
        word: str = commands.param(description="The word or phrase to remove."),
    ) -> None:
        """Remove one of your highlights from this server."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        normalized = " ".join(word.split()).casefold()
        row = await self.bot.pool.fetchrow(
            "DELETE FROM highlights "
            "WHERE user_id = $1 AND guild_id = $2 AND word_normalized = $3 "
            "RETURNING word",
            ctx.author.id,
            ctx.guild.id,
            normalized,
        )
        self._invalidate_highlights(ctx.guild.id)
        if row is None:
            await ctx.send(
                "That highlight does not exist in this server.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"Removed highlight **{escape_markdown(str(row['word']))}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @highlight.command(name="import")
    @commands.guild_only()
    async def highlight_import(
        self,
        ctx: Context,
        *,
        source: str = commands.param(
            description="The source server ID or exact server name."
        ),
    ) -> None:
        """Copy your highlights from another server into this server."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        source_guild = self._resolve_guild(self.bot, source)
        if source_guild is None:
            raise commands.BadArgument(
                "I could not find one unique server matching that ID or name."
            )
        if source_guild.id == ctx.guild.id:
            raise commands.BadArgument(
                "The source and destination servers are the same."
            )

        source_member = source_guild.get_member(ctx.author.id)
        if source_member is None:
            try:
                source_member = await source_guild.fetch_member(ctx.author.id)
            except discord.HTTPException as exc:
                raise commands.BadArgument(
                    "You must be a member of the source server to import highlights."
                ) from exc

        rows = await self.bot.pool.fetch(
            "SELECT word, word_normalized FROM highlights "
            "WHERE user_id = $1 AND guild_id = $2",
            ctx.author.id,
            source_guild.id,
        )
        if not rows:
            await ctx.send(
                "You do not have any highlights in the source server.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self.bot.pool.executemany(
            "INSERT INTO highlights (user_id, guild_id, word, word_normalized) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, guild_id, word_normalized) DO NOTHING",
            [
                (ctx.author.id, ctx.guild.id, row["word"], row["word_normalized"])
                for row in rows
            ],
        )
        self._invalidate_highlights(ctx.guild.id)
        await ctx.send(
            f"Imported {len(rows)} highlight(s) from **{escape_markdown(source_guild.name)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def self_send_asset(
        self,
        ctx: Context,
        url: str,
        extra: dict,
        *,
        cached_at: Optional[datetime.datetime] = None,
    ) -> None:
        image = await to_image(self.bot.session, url)
        file = discord.File(image, filename=f"{extra.get('name', 'template')[:32]}.png")

        e = discord.Embed(
            title=str(extra.get("name", "Unknown")).title(), color=self.bot.embedcolor
        )
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
        """Replace Latin letters with similar-looking Cyrillic characters."""
        words = discord.utils.escape_markdown(words, ignore_links=False)
        all_letters = [letter for word in words for letter in word]
        new_words = []

        for letter in all_letters:
            try:
                new_words.append(self.cyrillic_letters[letter])
            except KeyError:
                new_words.append(f"‌**{letter}**‌")
        fmt = "".join(new_words)
        await ctx.send(fmt[:2000])

    @commands.command(
        name="screenshot",
        aliases=("ss",),
        extras={"usage": "<website> [-delay 0 -full-page]"},
    )
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def screenshot(
        self,
        ctx: Context,
        website: str = param(description="The website's url.", converter=URLConverter),
        *,
        flags: ScreenshotFlags = param(
            description="Flags to use while screenshotting."
        ),
    ):
        """Take a screenshot of a website.

        -# -delay        Wait up to 10 seconds before taking the screenshot.
        -# -full-page    Capture the full page instead of the visible area.
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
                browser_context = await browser.new_context(
                    locale="en-US", service_workers="block"
                )
                page = await browser_context.new_page()

                async def guard_request(route):
                    try:
                        await validate_public_url(route.request.url)
                    except commands.CommandError:
                        await route.abort("blockedbyclient")
                    else:
                        await route.continue_()

                await page.route("**/*", guard_request)
                await page.goto(website, wait_until="domcontentloaded", timeout=15_000)
                await asyncio.sleep(flags.delay)
                if flags.full_page:
                    height = await page.evaluate(
                        "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                    )
                    if int(height) > 12_000:
                        raise commands.BadArgument(
                            "The page is too tall for a full-page screenshot."
                        )
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
        """Search for a word on Urban Dictionary.

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
        except discord.HTTPException:
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

        xp_by_user: dict[int, int] = dict(xp)  # type: ignore
        entries = [
            escape_markdown(f"{await self.lb_name(user_id)}: {xp:,}")
            for user_id, xp in xp_by_user.items()
        ]
        pages = SimplePages(entries=entries, per_page=10, ctx=ctx)
        pages.embed.title = "Global ranks"
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
            await events._log_solve(
                ctx.author.id, name, "command", ctx.guild.id if ctx.guild else None
            )

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
                await self.self_send_asset(
                    ctx, cached[0], cached[1], cached_at=cached[2]
                )
                return
            elif cached:
                del self.bot.cached_roblox_templates[aid]

            row = await self.bot.pool.fetchrow(
                "SELECT image_url, extra, cached_at FROM roblox_templates WHERE asset_id = $1",
                aid,
            )
            if row:
                extra = (
                    json.loads(row["extra"])
                    if isinstance(row["extra"], str)
                    else (row["extra"] or {})
                )
                extra.setdefault("name", "Unknown")
                self.bot.cached_roblox_templates[aid] = (
                    row["image_url"],
                    extra,
                    row["cached_at"],
                )
                await self.self_send_asset(
                    ctx, row["image_url"], extra, cached_at=row["cached_at"]
                )
                return

            async with self.bot.session.get(
                f"https://economy.roblox.com/v2/assets/{aid}/details",
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if response.status != 200:
                    raise commands.CommandError(
                        "Failed to reach Roblox. Try again later."
                    )
                details = json.loads(await read_bounded_response(response, 1_000_000))
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
            asset_url = f"https://assetdelivery.roblox.com/v1/asset?id={aid}"
            headers = {"Cookie": f".ROBLOSECURITY={cookie}"} if cookie else None
            async with self.bot.session.get(
                asset_url,
                headers=headers,
                allow_redirects=False,
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if 300 <= response.status < 400 and response.headers.get("Location"):
                    # Never forward the account cookie to the redirect target.
                    redirected = urljoin(asset_url, response.headers["Location"])
                    await validate_public_url(redirected)
                    async with self.bot.session.get(
                        redirected, allow_redirects=False, timeout=ROBLOX_TIMEOUT
                    ) as redirected_response:
                        validate_connected_peer(redirected_response)
                        if redirected_response.status != 200:
                            raise commands.CommandError(
                                "Failed to fetch asset XML. Try again later."
                            )
                        stdout = await read_bounded_response(
                            redirected_response, 5_000_000
                        )
                elif response.status == 200:
                    stdout = await read_bounded_response(response, 5_000_000)
                else:
                    raise commands.CommandError(
                        "Failed to fetch asset XML. Try again later."
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

            async with self.bot.session.get(
                "https://thumbnails.roblox.com/v1/assets",
                params={"assetIds": texture_id, "size": "420x420", "format": "Png"},
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if response.status != 200:
                    raise commands.CommandError(
                        "Failed to fetch template thumbnail. Try again later."
                    )
                thumb_data = json.loads(
                    await read_bounded_response(response, 1_000_000)
                )
            image_url = thumb_data["data"][0]["imageUrl"]

            await self.bot.pool.execute(
                "INSERT INTO roblox_templates (asset_id, image_url, item_name, extra) VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (asset_id) DO UPDATE SET image_url = $2, item_name = $3, extra = $4::jsonb, cached_at = now() at time zone 'utc'",
                aid,
                image_url,
                extra["name"],
                json.dumps(extra),
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            self.bot.cached_roblox_templates[aid] = (image_url, extra, now)

            await self.self_send_asset(ctx, image_url, extra, cached_at=now)

    @commands.command(name="pokehelp")
    async def pokehelp(self, ctx: Context):
        """Shows which Pokémon you ask for help solving the most."""

        rows = await self.bot.pool.fetch(
            "SELECT pokemon_name, COUNT(*) as c FROM pokemon_solves WHERE user_id = $1 GROUP BY pokemon_name ORDER BY c DESC",
            ctx.author.id,
        )

        if not rows:
            raise commands.BadArgument(
                f"No solve history found for {ctx.author}. Have you used fish to auto-solve Pokétwo spawns?"
            )

        entries = [
            f"{r['pokemon_name'].capitalize()} (*{plural(int(r['c'])):time}*)"
            for r in rows
        ]

        pages = SimplePages(entries=entries, ctx=ctx, per_page=15)
        pages.embed.color = self.bot.embedcolor
        pages.embed.title = f"Most helped Pokémon for {ctx.author.display_name}"
        await pages.start(ctx)

    @commands.command(name="pokepractice")
    async def pokepractice(self, ctx: Context):
        """Guess a random Pokémon from your solve history."""

        row = await self.bot.pool.fetchrow(
            "SELECT pokemon_name FROM pokemon_solves WHERE user_id = $1 ORDER BY RANDOM() LIMIT 1",
            ctx.author.id,
        )

        if not row:
            raise commands.BadArgument(
                f"No solve history found for {ctx.author}. Have you used fish to auto-solve Pokétwo spawns?"
            )

        name = row["pokemon_name"]
        display = name.capitalize()

        url = f"https://img.pokemondb.net/artwork/large/{name}.jpg"

        img_data: bytes | None = None
        async with ctx.typing():
            for attempt in range(3):
                try:
                    # ``bytes=True`` returns raw bytes at runtime, but the
                    # converter's union return type cannot express that
                    # relationship to static type checkers.
                    img_data = cast(bytes, await to_image(ctx.session, url, bytes=True))
                    break
                except Exception:
                    if attempt >= 2:
                        raise commands.BadArgument(
                            "Couldn't load an image, this may be a Pokétwo-specific Pokémon not found in standard databases."
                        )
                    row = await self.bot.pool.fetchrow(
                        "SELECT pokemon_name FROM pokemon_solves WHERE user_id = $1 AND pokemon_name != $2 ORDER BY RANDOM() LIMIT 1",
                        ctx.author.id,
                        name,
                    )
                    if not row:
                        raise commands.BadArgument(
                            "Couldn't find an image for any of your Pokémon."
                        )
                    name = row["pokemon_name"]
                    display = name.capitalize()
                    url = f"https://img.pokemondb.net/artwork/large/{name}.jpg"

        if img_data is None:
            raise commands.BadArgument("Couldn't load an image.")

        embed = discord.Embed(
            color=self.bot.embedcolor, title="What Pokémon is this? You have 5 guesses."
        )
        embed.set_image(url="attachment://pokemon.png")

        give_up_flag = asyncio.Event()
        game_view = GameView(ctx, give_up_flag)

        msg = await ctx.send(
            embed=embed,
            file=discord.File(BytesIO(img_data), "pokemon.png"),
            view=game_view,
        )

        attempts = 0
        correct = False
        while attempts < 5 and not correct and not give_up_flag.is_set():
            tasks = [
                asyncio.create_task(
                    ctx.bot.wait_for(
                        "message",
                        timeout=30.0,
                        check=lambda m: m.author == ctx.author
                        and m.channel == ctx.channel,
                    )
                ),
                asyncio.create_task(give_up_flag.wait()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            if give_up_flag.is_set():
                embed.color = 0xE74C3C
                embed.title = f"You gave up! It was {display}."
                await msg.edit(embed=embed, view=EndView(self, ctx))
                return

            guess_msg = done.pop().result()
            guess = guess_msg.content.strip().lower()
            attempts += 1

            if guess == name:
                correct = True
                embed.color = 0x2ECC71
                embed.title = f"Correct! It's {display}."
                try:
                    await guess_msg.add_reaction("✅")
                except discord.HTTPException:
                    pass
            else:
                try:
                    await guess_msg.add_reaction("❌")
                except discord.HTTPException:
                    pass
                if attempts >= 5:
                    embed.color = 0xE74C3C
                    embed.title = f"❌ Out of guesses! It was {display}."
                else:
                    embed.title = f"What Pokémon is this? ({5 - attempts} guesses left)"
            await msg.edit(embed=embed)

        await msg.edit(embed=embed, view=EndView(self, ctx))


async def setup(bot: Fishie):
    await bot.add_cog(Tools(bot))
