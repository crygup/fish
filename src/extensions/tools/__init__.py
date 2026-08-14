from __future__ import annotations

import asyncio
import datetime
import re
from importlib import import_module
from io import BytesIO
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional, cast

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from extensions.context import Context
from utils import (
    AuthorView,
    FieldPageSource,
    Pager,
    SimplePages,
    TenorUrlConverter,
    UrbanPageSource,
    fetch_public_bytes,
    get_or_fetch_user,
    plural,
    to_image,
    translate,
    update_pokemon,
)

from .calculator import Calculator
from .command_stats import CommandStats
from .downloads import Downloads
from .purge import PurgeCog
from .reminders import Reminder
from .tags import Tags

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

param = commands.param


def _qr_safe_text(value: object, limit: int = 1_500) -> str:
    text = discord.utils.escape_mentions(escape_markdown(str(value or ""))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "(empty)"


def _make_qr_png(text: str) -> bytes:
    """Generate a QR PNG without importing the optional package at bot startup."""
    qrcode = import_module("qrcode")
    image = qrcode.make(text)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _decode_qr_values(data: bytes) -> list[str]:
    """Decode one or more QR codes from image bytes with OpenCV."""
    cv2 = import_module("cv2")
    numpy = import_module("numpy")
    image = cv2.imdecode(numpy.frombuffer(data, dtype=numpy.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return []

    detector = cv2.QRCodeDetector()
    values: list[str] = []
    try:
        result = detector.detectAndDecodeMulti(image)
        if result and result[0]:
            values.extend(str(value) for value in result[1] if value)
    except (AttributeError, cv2.error):
        # Older OpenCV builds may not expose multi-code detection.
        pass

    if not values:
        try:
            value, _points, _straight = detector.detectAndDecode(image)
        except (AttributeError, cv2.error):
            value = ""
        if value:
            values.append(str(value))
    return list(dict.fromkeys(values))


def _translate_display_text(value: object, limit: int = 3_500) -> str:
    """Keep translated text from becoming a mention or overflowing a display."""
    text = discord.utils.escape_mentions(escape_markdown(str(value or ""))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "(empty)"


class TranslateView(discord.ui.LayoutView):
    """Components V2 presentation for a translation result."""

    def __init__(self, ctx: Context, result: Any) -> None:
        super().__init__(timeout=120)
        source = _translate_display_text(result.source_language, 100)
        target = _translate_display_text(result.target_language, 100)
        original = _translate_display_text(result.original)
        translated = _translate_display_text(result.translated)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Translated"),
                discord.ui.TextDisplay(f"**From {source}:**\n{original}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"**To {target}:**\n{translated}"),
                accent_color=ctx.bot.embedcolor,
            )
        )


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
    PurgeCog,
    CommandStats,
    Calculator,
):
    """Quality of life tools"""

    emoji = discord.PartialEmoji(name="\U0001f6e0")

    def __init__(self, bot: Fishie) -> None:
        super().__init__(bot)
        self._highlight_activity: dict[tuple[int, int], float] = {}
        self._highlight_cache: dict[int, list[tuple[int, str, re.Pattern[str]]]] = {}
        self._highlight_tasks: set[asyncio.Task[None]] = set()

    @cast(Any, commands.hybrid_group)(
        name="text",
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def text(self, ctx: Context) -> None:
        """Transform text with Fishie's text utilities."""
        await ctx.send_help(ctx.command)

    @text.command(name="cyrillic", aliases=("cryllic",))
    @app_commands.describe(words="Text to replace with similar-looking characters.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def text_cyrillic(self, ctx: Context, *, words: str) -> None:
        """Replace Latin letters with similar-looking Cyrillic characters."""
        await self._send_cyrillic(ctx, words)

    @text.command(name="merica", aliases=("cm",))
    @app_commands.describe(words="Text to separate with United States flag emojis.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def text_merica(self, ctx: Context, *, words: str) -> None:
        """Separate words with United States flag emojis."""
        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", words)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @cast(Any, commands.hybrid_group)(
        name="pokemon",
        aliases=("poke",),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pokemon(self, ctx: Context) -> None:
        """Show how many Pokémon names are currently cached."""
        await ctx.send(f"There are currently {len(self.bot.pokemon):,} cached.")

    @pokemon.command(name="update")
    @commands.is_owner()
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pokemon_update(self, ctx: Context) -> None:
        """Refresh the cached Pokémon name list (owner only)."""
        await update_pokemon(self.bot)
        await ctx.send(
            "Pokémon cache updated.", allowed_mentions=discord.AllowedMentions.none()
        )

    @pokemon.command(name="add")
    @commands.is_owner()
    @app_commands.describe(name="Pokémon name to add to the solver cache.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pokemon_add(self, ctx: Context, *, name: str) -> None:
        """Add a Pokémon name to the solver cache (owner only)."""
        name = name.strip()
        if not name:
            raise commands.BadArgument("Provide a Pokémon name to add.")
        await self.bot.pool.execute(
            "INSERT INTO added_pokemon (name, created_at) VALUES ($1, $2)",
            name.casefold(),
            discord.utils.utcnow(),
        )
        await update_pokemon(self.bot)
        await ctx.send("Pokémon added to the solver cache.")

    @pokemon.command(name="solve")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pokemon_solve(self, ctx: Context) -> None:
        """Solve a replied Pokétwo hint message."""
        await self.solve(ctx)

    @pokemon.command(name="solved")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pokemon_solved(self, ctx: Context) -> None:
        """Show which Pokémon you have asked Fishie to solve most often."""
        await self.pokesolved(ctx)

    @pokemon.command(name="practice")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pokemon_practice(self, ctx: Context) -> None:
        """Practice identifying a Pokémon from your solve history."""
        await self.pokepractice(ctx)

    async def _send_qr_make(self, ctx: Context, text: str) -> None:
        text = str(text).strip()
        if not text:
            raise commands.BadArgument("Provide text to encode in the QR code.")
        if len(text) > 2_000:
            raise commands.BadArgument(
                "QR text cannot be longer than 2,000 characters."
            )
        try:
            image_data = await asyncio.to_thread(_make_qr_png, text)
        except ImportError as error:
            raise commands.BadArgument(
                "QR support is not installed on this bot."
            ) from error
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## QR code\n**Text:** {_qr_safe_text(text)}"),
                discord.ui.Separator(),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem("attachment://qrcode.png")
                ),
                discord.ui.TextDisplay("Generated by Fishie."),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(
            view=view,
            file=discord.File(BytesIO(image_data), filename="qrcode.png"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_qr_read(
        self,
        ctx: Context,
        media: str | None,
        attachment: discord.Attachment | None,
    ) -> None:
        data: bytes | None = None
        if attachment is None:
            message = getattr(ctx, "message", None)
            attachments = getattr(message, "attachments", ())
            attachment = next(iter(attachments), None)
        if attachment is not None:
            if attachment.size > 10 * 1024 * 1024:
                raise commands.BadArgument("The QR image is too large (10 MB maximum).")
            data = await attachment.read()
            if len(data) > 10 * 1024 * 1024:
                raise commands.BadArgument("The QR image is too large (10 MB maximum).")
        elif media:
            media = media.strip().strip("<>")
            fetched = await fetch_public_bytes(
                ctx.session,
                media,
                max_bytes=10 * 1024 * 1024,
                allowed_content_prefixes=("image/",),
            )
            data = fetched.data
        if not data:
            raise commands.BadArgument("Provide an image attachment or image URL.")

        try:
            values = await asyncio.to_thread(_decode_qr_values, data)
        except ImportError as error:
            raise commands.BadArgument(
                "QR support is not installed on this bot."
            ) from error
        if not values:
            raise commands.BadArgument(
                "I couldn't find a readable QR code in that image."
            )

        lines = ["## QR code contents"]
        lines.extend(
            f"{index}. `{_qr_safe_text(value, 1_800)}`"
            for index, value in enumerate(values, 1)
        )
        lines.append("\n*Decoded with OpenCV.*")
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @cast(Any, commands.hybrid_group)(
        name="qr",
        aliases=("qrcode",),
        fallback="make",
        invoke_without_command=True,
    )
    @app_commands.describe(text="The text or URL to encode in a QR code.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def qr(self, ctx: Context, *, text: str) -> None:
        """Generate a QR code from text. Use `qr read` to decode one."""
        await self._send_qr_make(ctx, text)

    @qr.command(name="generate")
    @app_commands.describe(text="The text or URL to encode in a QR code.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def qr_generate(self, ctx: Context, *, text: str) -> None:
        """Generate a QR code from text or a URL."""
        await self._send_qr_make(ctx, text)

    @qr.command(name="read", aliases=("decode", "scan"))
    @app_commands.describe(
        media="An image URL containing a QR code.",
        attachment="An image attachment containing a QR code.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def qr_read(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Read one or more QR codes from an image URL or attachment."""
        async with ctx.typing():
            await self._send_qr_read(ctx, media, attachment)

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
        await self._send_cyrillic(ctx, words)

    async def _send_cyrillic(self, ctx: Context, words: str) -> None:
        """Send the shared Cyrillic text transformation."""
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

    @commands.command(name="pokesolved")
    async def pokesolved(self, ctx: Context):
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

    @commands.command(hidden=True)
    async def translate(
        self,
        ctx: Context,
        *,
        message: Annotated[Optional[str], commands.clean_content] = None,
    ):
        """Translates a message to English using Google translate."""

        if message is None:
            reply = ctx.ref
            if reply is None:
                return await ctx.send("Missing a message to translate")
            message = reply.content

        try:
            result = await translate(message, session=self.bot.session)
        except Exception as error:
            return await ctx.send(
                f"An error occurred: {error.__class__.__name__}: {error}"
            )

        await ctx.send(
            view=TranslateView(ctx, result),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Fishie):
    await bot.add_cog(Tools(bot))
