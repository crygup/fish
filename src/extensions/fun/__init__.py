from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

import discord
import psutil
from discord import app_commands
from discord.ext import commands

from utils import to_image
from utils.paths import FILES_ROOT

from .about import About
from .corn import Corn
from .helpers import RPSView, WTPView, dagpi

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class PhoneFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    onlyme: bool = commands.flag(
        aliases=["om", "private"],
        default=False,
        description="Only relay messages sent by you from this channel.",
    )

    @classmethod
    def parse_flags(cls, argument: str, *, ignore_extra: bool = True):
        """Allow the private flag to be used without a value."""
        bare_flag = r"(?<!\S)--?(?:onlyme|om|private)(?=\s*(?:--?\w+(?:\s|$)|$))"
        found = bool(re.search(bare_flag, argument, flags=re.IGNORECASE))
        argument = re.sub(bare_flag, "", argument, flags=re.IGNORECASE).strip()
        parsed = super().parse_flags(argument, ignore_extra=ignore_extra)
        if found:
            parsed["onlyme"] = ["true"]
        return parsed


@dataclass
class PhoneRinger:
    author_id: int
    guild_id: int
    channel_id: int
    channel: discord.abc.Messageable
    event: asyncio.Event
    onlyme: bool


@dataclass
class PhoneConnection:
    channel_ids: tuple[int, int]
    user_ids: tuple[int, int]
    onlyme: tuple[bool, bool]
    last_activity: float
    idle_task: asyncio.Task[None] | None = field(default=None, repr=False)
    logs: list[PhoneLogEntry] = field(default_factory=list, repr=False)
    log_bytes: int = field(default=0, repr=False)
    logs_truncated: bool = field(default=False, repr=False)
    cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False)
    reported: bool = False


@dataclass
class PhoneLogEntry:
    guild_id: int
    username: str
    user_id: int
    message_id: int
    channel_id: int
    content: str


class PhoneConsentView(discord.ui.View):
    def __init__(self, cog: Fun, user_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.result: bool | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This consent prompt belongs to another user.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _submit(
        self,
        interaction: discord.Interaction,
        accepted: bool,
    ) -> None:
        try:
            await self.cog._save_phone_consent(self.user_id, accepted)
        except Exception:
            self.cog.bot.logger.exception("Failed to save phone consent")
            await interaction.response.send_message(
                "I couldn't save your phone consent. Please try again.", ephemeral=True
            )
            return

        self.result = accepted
        self.stop()
        self.disable_all()
        content = (
            "Phone consent accepted. You can now use `fish phone`."
            if accepted
            else "Phone consent declined. Your phone messages will not be relayed."
        )
        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="I Agree", style=discord.ButtonStyle.green)
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._submit(interaction, True)

    @discord.ui.button(label="I Don't Agree", style=discord.ButtonStyle.red)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._submit(interaction, False)

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PhoneReportView(discord.ui.View):
    def __init__(self, cog: Fun, connection: PhoneConnection):
        super().__init__(timeout=300)
        self.cog = cog
        self.connection = connection
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.connection.user_ids:
            return True
        await interaction.response.send_message(
            "Only someone who was in this call can report it.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="Report conversation", style=discord.ButtonStyle.red)
    async def report(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        try:
            sent = await self.cog._report_phone_session(self.connection)
        except Exception:
            self.cog.bot.logger.exception("Failed to send phone report")
            await interaction.response.send_message(
                "I couldn't send the report. Please try again.", ephemeral=True
            )
            return

        self.disable_all()
        if sent:
            content = "Phone report sent. Thank you."
        else:
            content = "This phone call has already been reported."
        await interaction.response.edit_message(content=content, view=self)

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


PHONE_IDLE_TIMEOUT = 30.0
PHONE_LOG_MAX_ENTRIES = 500
PHONE_LOG_MAX_BYTES = 200_000


class Fun(About, Corn):
    """Fun miscellaneous commands"""

    emoji = discord.PartialEmoji(name="\U0001f604")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self._phone_lock = asyncio.Lock()
        self._phone_ringers: list[PhoneRinger] = []
        self._phone_connections: dict[int, PhoneConnection] = {}
        self._phone_archives: dict[int, PhoneConnection] = {}
        self._phone_consent_lock = asyncio.Lock()
        self._phone_consent_cache: dict[int, bool | None] = {}
        self._phone_consent_prompts: set[int] = set()
        self.phone_logs = discord.Webhook.from_url(
            self.bot.config["webhooks"]["phone_logs"], session=self.bot.session
        )
        self.process = psutil.Process()
        self.invite_url = discord.utils.oauth_url(
            self.bot.config["ids"]["bot_id"], permissions=self.bot.bot_permissions
        )

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        """
        Play rock paper scissors against me!
        """
        await ctx.send(view=RPSView(ctx))

    @commands.command(name="monark")
    @commands.cooldown(1, 5)
    async def monark(self, ctx: Context):
        """monark said this"""

        await ctx.send(
            file=discord.File(
                FILES_ROOT / "monark" / f"monark{random.randint(1,3)}.png",
                "monark.png",
            )
        )

    @commands.command(name="hattori")
    @commands.cooldown(1, 3)
    async def hattori(self, ctx: Context):
        """hattori"""
        name = "hattori2" if random.randint(0, 15) == 6 else "hattori"
        await ctx.send(
            file=discord.File(FILES_ROOT / "images" / f"{name}.png", "hattori.png")
        )

    @commands.command(name="merica", aliases=("cm",))
    @commands.cooldown(1, 5)
    async def merica(self, ctx: Context, *, text: str):
        """we love america!!!"""

        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", text)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="invite", aliases=("join",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def invite(self, ctx: Context):
        """Sends a link to add me to a server."""

        await ctx.send(self.invite_url)

    async def _phone_consent(self, user_id: int) -> bool | None:
        if user_id in self._phone_consent_cache:
            return self._phone_consent_cache[user_id]

        row = await self.bot.pool.fetchrow(
            "SELECT consented FROM phone_consent WHERE user_id = $1", user_id
        )
        consented = None if row is None else bool(row["consented"])
        self._phone_consent_cache[user_id] = consented
        return consented

    async def _save_phone_consent(self, user_id: int, consented: bool) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO phone_consent (user_id, consented)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET consented = EXCLUDED.consented
            """,
            user_id,
            consented,
        )
        self._phone_consent_cache[user_id] = consented

    async def _ensure_phone_consent(
        self,
        channel: discord.abc.Messageable,
        user_id: int,
        *,
        prompt_declined: bool,
    ) -> bool:
        status = await self._phone_consent(user_id)
        if status is True:
            return True
        if status is False and not prompt_declined:
            return False

        async with self._phone_consent_lock:
            if user_id in self._phone_consent_prompts:
                return False
            self._phone_consent_prompts.add(user_id)

        view = PhoneConsentView(self, user_id)
        try:
            view.message = await channel.send(
                "Before using Fishie Phone, please confirm: calls may be recorded "
                "in case of reports to make sure everyone is safe while using the "
                "Fishie Phone.",
                view=view,
            )
            await view.wait()
            return view.result is True
        except discord.HTTPException:
            return False
        finally:
            async with self._phone_consent_lock:
                self._phone_consent_prompts.discard(user_id)

    def _phone_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None

    @staticmethod
    def _format_phone_logs(connection: PhoneConnection) -> str:
        grouped: dict[tuple[int, int], list[PhoneLogEntry]] = {}
        for entry in connection.logs:
            grouped.setdefault((entry.guild_id, entry.channel_id), []).append(entry)

        sections: list[str] = []
        for (guild_id, channel_id), entries in grouped.items():
            lines = [f"[{guild_id}] - [{channel_id}]", ""]
            lines.extend(
                f"{entry.username} {entry.user_id} {entry.message_id} - {entry.content}"
                for entry in entries
            )
            sections.append("\n".join(lines))
        report = "\n\n".join(sections) or "No messages were sent during this call."
        if connection.logs_truncated:
            report += "\n\n[Transcript truncated to protect resources.]"
        return report

    async def _report_phone_session(self, connection: PhoneConnection) -> bool:
        async with self._phone_lock:
            if connection.reported:
                return False
            connection.reported = True

        try:
            report = self._format_phone_logs(connection).encode("utf-8")
            file = discord.File(BytesIO(report), filename="phone-report.txt")
            await self.phone_logs.send(file=file, username="Fishie Phone Reports")
        except Exception:
            async with self._phone_lock:
                connection.reported = False
            raise
        return True

    async def _expire_phone_archive(self, connection: PhoneConnection) -> None:
        await asyncio.sleep(300)
        self._phone_archives.pop(id(connection), None)
        connection.logs.clear()

    async def _close_phone(self, connection: PhoneConnection, *, reason: str) -> bool:
        async with self._phone_lock:
            removed = False
            for channel_id in connection.channel_ids:
                if self._phone_connections.get(channel_id) is connection:
                    self._phone_connections.pop(channel_id, None)
                    removed = True

        if not removed:
            return False

        current_task = asyncio.current_task()
        if connection.idle_task and connection.idle_task is not current_task:
            connection.idle_task.cancel()

        self._phone_archives[id(connection)] = connection
        connection.cleanup_task = asyncio.create_task(
            self._expire_phone_archive(connection)
        )

        for channel_id in connection.channel_ids:
            channel = self._phone_channel(channel_id)
            if channel is not None:
                try:
                    view = PhoneReportView(self, connection)
                    view.message = await channel.send(reason, view=view)
                except discord.HTTPException:
                    pass
        return True

    async def _phone_idle_watch(self, connection: PhoneConnection) -> None:
        while True:
            delay = max(
                0.0,
                connection.last_activity
                + PHONE_IDLE_TIMEOUT
                - asyncio.get_running_loop().time(),
            )
            await asyncio.sleep(delay)
            if (
                asyncio.get_running_loop().time() - connection.last_activity
                < PHONE_IDLE_TIMEOUT
            ):
                continue
            await self._close_phone(
                connection,
                reason="☎️ This phone call ended after 30 seconds without any messages.",
            )
            return

    @staticmethod
    async def _is_phone_command(bot: Fishie, message: discord.Message) -> bool:
        context = await bot.get_context(message)
        command = context.command
        return bool(command and command.name in {"phone", "hangup"})

    @commands.hybrid_command(
        name="phone",
        aliases=("ring", "userphone", "call", "fishiephone", "fishphone"),
        extras={"usage": "[-onlyme]"},
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def phone(self, ctx: Context, *, flags: PhoneFlags):
        """Ring for a user in another server and connect the two channels.

        -# -onlyme    Only relay messages sent by you from this channel.
        """

        if ctx.guild is None:
            return

        is_nsfw = getattr(ctx.channel, "is_nsfw", None)
        if callable(is_nsfw) and is_nsfw():
            await ctx.send("The phone command cannot be used in an NSFW channel.")
            return

        if not await self._ensure_phone_consent(
            ctx.channel, ctx.author.id, prompt_declined=True
        ):
            return

        if ctx.channel.id in self._phone_connections:
            await ctx.send("This channel is already connected to another phone call.")
            return

        already_ringing = False
        match: PhoneRinger | None = None
        async with self._phone_lock:
            already_ringing = any(
                r.author_id == ctx.author.id for r in self._phone_ringers
            )
            if not already_ringing:
                match = next(
                    (
                        r
                        for r in self._phone_ringers
                        if r.guild_id != ctx.guild.id and r.author_id != ctx.author.id
                    ),
                    None,
                )
                if match is not None:
                    self._phone_ringers.remove(match)
                    connection = PhoneConnection(
                        channel_ids=(match.channel_id, ctx.channel.id),
                        user_ids=(match.author_id, ctx.author.id),
                        onlyme=(match.onlyme, flags.onlyme),
                        last_activity=asyncio.get_running_loop().time(),
                    )
                    self._phone_connections[match.channel_id] = connection
                    self._phone_connections[ctx.channel.id] = connection
                    connection.idle_task = asyncio.create_task(
                        self._phone_idle_watch(connection)
                    )
                    match.event.set()
                else:
                    ringer = PhoneRinger(
                        author_id=ctx.author.id,
                        guild_id=ctx.guild.id,
                        channel_id=ctx.channel.id,
                        channel=ctx.channel,
                        event=asyncio.Event(),
                        onlyme=flags.onlyme,
                    )
                    self._phone_ringers.append(ringer)

        if already_ringing:
            await ctx.send("You are already ringing for a phone call.")
            return

        if match is not None:
            await match.channel.send(
                "☎️ Connected! Messages from this channel will now be relayed."
            )
            await ctx.send(
                "☎️ Connected! Messages from this channel will now be relayed."
            )
            return

        mode = (
            "Only your messages will be relayed."
            if flags.onlyme
            else "Messages from everyone in this channel will be relayed."
        )
        await ctx.send(
            f"☎️ Ringing for a user in another server for 60 seconds. {mode}"
        )

        try:
            await asyncio.wait_for(ringer.event.wait(), timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("☎️ Nobody answered your call.")
        finally:
            async with self._phone_lock:
                if ringer in self._phone_ringers:
                    self._phone_ringers.remove(ringer)

    @commands.hybrid_command(name="hangup", aliases=("end",))
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def hangup(self, ctx: Context) -> None:
        """End your active phone call or cancel your unanswered ring."""

        if ctx.guild is None:
            return

        async with self._phone_lock:
            ringer = next(
                (r for r in self._phone_ringers if r.author_id == ctx.author.id),
                None,
            )
            if ringer is not None:
                self._phone_ringers.remove(ringer)
                ringer.event.set()
            connection = self._phone_connections.get(ctx.channel.id)

        if connection is not None:
            await self._close_phone(connection, reason="☎️ Phone call ended.")
            return

        if ringer is not None:
            await ctx.send("☎️ Ring cancelled.")
        else:
            await ctx.send("☎️ You do not have an active phone call.")

    @commands.Cog.listener("on_message")
    async def phone_relay(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        if await self._is_phone_command(self.bot, message):
            return

        connection = self._phone_connections.get(message.channel.id)
        if connection is None:
            return

        if not await self._ensure_phone_consent(
            message.channel, message.author.id, prompt_declined=False
        ):
            return

        try:
            source_index = connection.channel_ids.index(message.channel.id)
        except ValueError:
            return

        if connection.onlyme[source_index] and (
            message.author.id != connection.user_ids[source_index]
        ):
            return

        target_channel_id = connection.channel_ids[1 - source_index]
        target = self.bot.get_channel(target_channel_id)
        if target is None:
            try:
                target = await self.bot.fetch_channel(target_channel_id)
            except (discord.HTTPException, discord.NotFound):
                return
        if not isinstance(target, discord.abc.Messageable):
            return

        connection.last_activity = asyncio.get_running_loop().time()
        author_name = discord.utils.escape_markdown(message.author.name)
        attachment_links = [attachment.url for attachment in message.attachments]
        logged_content = message.content
        if attachment_links:
            logged_content += "".join(f"\n{url}" for url in attachment_links)
        original_log_length = len(logged_content)
        if (
            len(connection.logs) < PHONE_LOG_MAX_ENTRIES
            and connection.log_bytes < PHONE_LOG_MAX_BYTES
        ):
            remaining = PHONE_LOG_MAX_BYTES - connection.log_bytes
            encoded_content = logged_content.encode("utf-8")[:remaining]
            logged_content = encoded_content.decode("utf-8", "ignore")
            connection.logs.append(
                PhoneLogEntry(
                    guild_id=message.guild.id,
                    username=message.author.name,
                    user_id=message.author.id,
                    message_id=message.id,
                    channel_id=message.channel.id,
                    content=logged_content,
                )
            )
            connection.log_bytes += len(encoded_content)
            if len(logged_content) < original_log_length:
                connection.logs_truncated = True
        else:
            connection.logs_truncated = True

        content = f"**{author_name}**"
        if message.content:
            content += f": {message.content}"
        if attachment_links:
            content += "".join(f"\n{url}" for url in attachment_links)

        try:
            await target.send(
                content=content[:2000],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    @commands.hybrid_command(name="8ball")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def _8ball(
        self,
        ctx: Context,
        *,
        question: str = commands.param(
            displayed_name="question", description="What shall you ask?"
        ),
    ):
        """Ask the magic 8-ball a question.

        tony wanted this command"""

        answers = (
            "It is certain.",
            "It is decidedly so.",
            "Without a doubt.",
            "Yes definitely.",
            "You may rely on it.",
            "As I see it, yes.",
            "Most likely.",
            "Outlook good.",
            "Yes.",
            "Signs point to yes.",
            "Reply hazy, try again.",
            "Ask again later.",
            "Better not tell you now.",
            "Cannot predict now.",
            "Concentrate and ask again.",
            "Don't count on it.",
            "My reply is no.",
            "My sources say no.",
            "Outlook not so good.",
            "Very doubtful.",
            "prolly",
            "prolly not",
            "cheese beast",
            "are you him?",
            "himmers bro",
            "ur mic is on btw",
            "u might be muted",
            "ur a beast",
            "ur him",
            "BANG",
            "sure man",
        )

        msg = random.choice(answers)
        if ctx.interaction:
            msg += f"\n-# {ctx.author.display_name} asked: *{question}*"

        await ctx.send(msg)

    @commands.command(name="wtp", hidden=True, enabled=False)
    async def wtp(self, ctx: Context):
        """Start a Who's That Pokémon guessing game."""
        await ctx.typing()

        data = await dagpi(self.bot, ctx.message, "https://api.dagpi.xyz/data/wtp")

        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_author(name="Who's that pokemon?")

        image = await to_image(ctx.session, data["question"])
        file = discord.File(fp=image, filename="pokemon.png")

        embed.set_image(url="attachment://pokemon.png")

        await ctx.send(embed=embed, file=file, view=WTPView(ctx, data))

    @commands.hybrid_command(
        name="badapple",
        aliases=(
            "ba",
            "bad apple",
        ),
    )
    @commands.cooldown(1, 15, commands.BucketType.channel)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def badapple(self, ctx: Context):
        """Bad Apple!! feat.nomico"""

        async with ctx.typing():
            await ctx.send(
                file=discord.File(
                    FILES_ROOT / "videos" / "bad apple.mp4",
                    filename=f"fishie_loves_{ctx.author.name}.mp4",
                )
            )

    @commands.command(
        name="quoteisifyouhaveaproblemwithmetextmeandifyoudonthavemynumberyoudontknowmewellenoughtohaveaproblemwithme",
        aliases=("QIIYHAPWMTMAIYDHMNYDKMWETHAPWM",),
        hidden=True,
    )
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def QIIYHAPWMTMAIYDHMNYDKMWETHAPWM(self, ctx: Context):
        """if you have a problem with me text me and if you dont have my number you dont know me well enough to have a problem with me"""

        async with ctx.typing():
            await ctx.send(
                file=discord.File(
                    FILES_ROOT / "videos" / "QIIYHAPWMTMAIYDHMNYDKMWETHAPWM.mp4",
                    filename=f"fishie_loves_{ctx.author.name}.mp4",
                )
            )


async def setup(bot: Fishie):
    await bot.add_cog(Fun(bot))
