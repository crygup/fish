from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
    Optional,
    TypeAlias,
    Union,
)

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import plural

if TYPE_CHECKING:
    from extensions.context import GuildContext

InviteChannels: TypeAlias = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
]

PurgeChannels: TypeAlias = Union[
    discord.TextChannel, discord.VoiceChannel, discord.Thread
]


class PurgeFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    # fmt: off
    user: Optional[discord.User] = commands.flag(description="Remove messages from this user", default=None)
    channel: Optional[PurgeChannels] = commands.flag(description="Remove messages from this user", default=None)
    contains: Optional[str] = commands.flag(description="Remove messages that contains this string (case sensitive)",default=None,)
    starts: Optional[str] = commands.flag(description="Remove messages that start with this string (case sensitive)",default=None,)
    ends: Optional[str] = commands.flag(description="Remove messages that end with this string (case sensitive)",default=None,)
    after: int = commands.flag(description="Search for messages that come after this message ID", default=None)
    before: int = commands.flag(description="Search for messages that come before this message ID", default=None)
    bot: bool = commands.flag(description="Remove messages from bots (not webhooks!)", default=False)
    webhooks: bool = commands.flag(description="Remove messages from webhooks", default=False)
    embeds: bool = commands.flag(description="Remove messages that have embeds", default=False)
    files: bool = commands.flag(description="Remove messages that have attachments", default=False)
    emoji: bool = commands.flag(description="Remove messages that have custom emoji", default=False)
    reactions: bool = commands.flag(description="Remove messages that have reactions", default=False)
    require: Literal["any", "all"] = commands.flag(description='Whether any or all of the flags should be met before deleting messages. Defaults to "all"',default="all",)
    # fmt: on


class PurgeCog(Cog):
    @commands.group(name="purge", invoke_without_command=True, fallback="messages")
    @commands.bot_has_permissions(manage_messages=True)
    @commands.has_guild_permissions(manage_messages=True)
    @commands.guild_only()
    async def purge(
        self,
        ctx: GuildContext,
        amount: Optional[commands.Range[int, 1, 2000]] = None,
        *,
        flags: PurgeFlags,
    ):
        """
        Mass delete some messages
        """
        amount = amount or 100

        predicates: list[Callable[[discord.Message], Any]] = []
        op = all if flags.require == "all" else any

        if flags.bot:
            if flags.webhooks:
                predicates.append(lambda m: m.author.bot)
            else:
                predicates.append(
                    lambda m: (m.webhook_id is None or m.interaction is not None)
                    and m.author.bot
                )
        elif flags.webhooks:
            predicates.append(lambda m: m.webhook_id is not None)

        if flags.embeds:
            predicates.append(lambda m: len(m.embeds))

        if flags.files:
            predicates.append(lambda m: len(m.attachments))

        if flags.reactions:
            predicates.append(lambda m: len(m.reactions))

        if flags.emoji:
            predicates.append(lambda m: re.search(r"<:(\w+):(\d+)>", m.content))

        if flags.user:
            predicates.append(lambda m: m.author == flags.user)

        if flags.contains:
            predicates.append(lambda m: flags.contains in m.content)  # type: ignore

        if flags.starts:
            predicates.append(lambda m: m.content.startswith(flags.starts))  # type: ignore

        if flags.ends:
            predicates.append(lambda m: m.content.endswith(flags.ends))  # type: ignore

        channel: PurgeChannels = flags.channel if flags.channel else ctx.channel  # type: ignore

        def predicate(m: discord.Message) -> bool:
            r = op(p(m) for p in predicates)
            return r

        before = discord.Object(id=flags.before) if flags.before else None
        after = discord.Object(id=flags.after) if flags.after else None

        confirm = await ctx.prompt(
            f"I am about to purge {plural(amount):message}, do you wish to continue?",
            timeout=30,
            delete_after=False,
        )

        if not confirm:
            raise commands.BadArgument("Aborting.")

        async with ctx.typing():
            try:
                deleted = await channel.purge(
                    limit=amount, before=before, after=after, check=predicate
                )
            except discord.Forbidden as e:
                raise commands.BadArgument(
                    "I do not have permissions to delete messages."
                )
            except discord.HTTPException as e:
                raise commands.BadArgument(f"Error: {e} (try a smaller search?)")

            await confirm.edit(
                content=f"Deleted {plural(len(deleted)):message}.",
                delete_after=7,
                view=None,
            )

    async def purge_guild_invites(
        self, ctx: GuildContext, guild: discord.Guild, amount: Optional[int] = None
    ):
        invites = await guild.invites()

        completed = 0
        failed = 0
        for invite in invites:
            try:
                await invite.delete(
                    reason=f"Purge invites command invoked by {ctx.author} (ID: {ctx.author.id})"
                )
            except (discord.NotFound, discord.HTTPException):
                failed += 1
            completed += 1
            if amount and completed >= amount:
                break

        await ctx.send(f"Deleted {completed - failed}/{completed} invites.")

    async def purge_channel_invites(
        self, ctx: GuildContext, channel: InviteChannels, amount: Optional[int] = None
    ):
        invites = await channel.invites()

        completed = 0
        failed = 0
        for invite in invites:
            try:
                await invite.delete(
                    reason=f"Purge invites command invoked by {ctx.author} (ID: {ctx.author.id})"
                )
            except (discord.NotFound, discord.HTTPException):
                failed += 1
            completed += 1
            if amount and completed >= amount:
                break

        await ctx.send(f"Deleted {completed - failed}/{completed} invites.")

    @purge.command(name="invites")
    @commands.has_guild_permissions(manage_guild=True, manage_channels=True)
    @commands.bot_has_permissions(manage_guild=True, manage_channels=True)
    @commands.guild_only()
    async def purge_invites(
        self,
        ctx: GuildContext,
        limit: Optional[int] = None,
        *,
        channel: Optional[InviteChannels] = None,
    ):
        """
        Purge invites for a channel or server
        """
        amount = None if not limit else limit

        function: Awaitable = (
            self.purge_channel_invites(ctx, channel, amount)
            if channel
            else self.purge_guild_invites(ctx, ctx.guild, amount)
        )

        async with ctx.typing():
            await function
