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
from discord.ext import commands

from utils import CHECK, plural, GuildChannel

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context

channel: TypeAlias = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
]


class PurgeFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    user: Optional[discord.User] = commands.flag(
        description="Remove messages from this user", default=None
    )
    channel: Optional[
        Union[discord.TextChannel, discord.VoiceChannel, discord.Thread]
    ] = commands.flag(description="Remove messages from this user", default=None)
    contains: Optional[str] = commands.flag(
        description="Remove messages that contains this string (case sensitive)",
        default=None,
    )
    starts: Optional[str] = commands.flag(
        description="Remove messages that start with this string (case sensitive)",
        default=None,
    )
    ends: Optional[str] = commands.flag(
        description="Remove messages that end with this string (case sensitive)",
        default=None,
    )
    after: int = commands.flag(
        description="Search for messages that come after this message ID", default=None
    )
    before: int = commands.flag(
        description="Search for messages that come before this message ID", default=None
    )
    bot: bool = commands.flag(
        description="Remove messages from bots (not webhooks!)", default=False
    )
    webhooks: bool = commands.flag(
        description="Remove messages from webhooks", default=False
    )
    embeds: bool = commands.flag(
        description="Remove messages that have embeds", default=False
    )
    files: bool = commands.flag(
        description="Remove messages that have attachments", default=False
    )
    emoji: bool = commands.flag(
        description="Remove messages that have custom emoji", default=False
    )
    reactions: bool = commands.flag(
        description="Remove messages that have reactions", default=False
    )
    require: Literal["any", "all"] = commands.flag(
        description='Whether any or all of the flags should be met before deleting messages. Defaults to "all"',
        default="all",
    )


class PurgeCog(CogBase):
    @commands.group(
        name="purge",
        invoke_without_command=True,
        extras={"BPerms": ["Manage Messages"], "UPerms": ["Manage Messages"]},
    )
    @commands.bot_has_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    async def purge(
        self,
        ctx: Context,
        amount: Optional[commands.Range[int, 1, 2000]] = None,
        *,
        flags: PurgeFlags,
    ):
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
            custom_emoji = re.compile(r"<:(\w+):(\d+)>")
            predicates.append(lambda m: custom_emoji.search(m.content))

        if flags.user:
            predicates.append(lambda m: m.author == flags.user)

        if flags.contains:
            predicates.append(lambda m: flags.contains in m.content)  # type: ignore

        if flags.starts:
            predicates.append(lambda m: m.content.startswith(flags.starts))  # type: ignore

        if flags.ends:
            predicates.append(lambda m: m.content.endswith(flags.ends))  # type: ignore

        channel = flags.channel if flags.channel else ctx.channel

        def predicate(m: discord.Message) -> bool:
            r = op(p(m) for p in predicates)
            return r

        before = discord.Object(id=flags.before) if flags.before else None
        after = discord.Object(id=flags.after) if flags.after else None

        confirm = await ctx.prompt(
            f"I am about to purge {plural(amount):message}, do you wish to continue?",
            timeout=30,
        )
        if not confirm:
            return await ctx.send("Aborting.")

        async with ctx.typing():
            try:
                deleted = await channel.purge(
                    limit=amount, before=before, after=after, check=predicate
                )
            except discord.Forbidden as e:
                return await ctx.send("I do not have permissions to delete messages.")
            except discord.HTTPException as e:
                return await ctx.send(f"Error: {e} (try a smaller search?)")

            await ctx.send(
                f"Deleted {plural(len(deleted)):message}.",
                delete_after=7,
            )

    async def purge_guild_invites(
        self, ctx: Context, guild: discord.Guild, amount: Optional[int] = None
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
        self, ctx: Context, channel: channel, amount: Optional[int] = None
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
    async def purge_invites(
        self,
        ctx: Context,
        limit: Optional[Union[int, Literal[None]]] = None,
        *,
        channel: Optional[channel] = None,
    ):
        amount = None if not limit else limit

        function: Awaitable = (
            self.purge_channel_invites(ctx, channel, amount)
            if channel
            else self.purge_guild_invites(ctx, ctx.guild, amount)
        )

        async with ctx.typing():
            await function
