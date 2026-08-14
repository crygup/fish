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
    channel: Optional[PurgeChannels] = commands.flag(description="Remove messages from this channel", default=None)
    contains: Optional[str] = commands.flag(description="Remove messages that contains this string (case sensitive)",default=None,)
    starts: Optional[str] = commands.flag(description="Remove messages that start with this string (case sensitive)",default=None,)
    ends: Optional[str] = commands.flag(description="Remove messages that end with this string (case sensitive)",default=None,)
    after: int = commands.flag(description="Remove messages that come after this message ID", default=None)
    before: int = commands.flag(description="Remove messages that come before this message ID", default=None)
    bot: bool = commands.flag(description="Remove messages from bots (not webhooks!)", default=False)
    webhooks: bool = commands.flag(description="Remove messages from webhooks", default=False)
    embeds: bool = commands.flag(description="Remove messages that have embeds", default=False)
    files: bool = commands.flag(description="Remove messages that have attachments", default=False)
    emoji: bool = commands.flag(description="Remove messages that have custom emoji", default=False)
    reactions: bool = commands.flag(description="Remove messages that have reactions", default=False)
    left: bool = commands.flag(description="Remove messages from users who left the server", default=False)
    online: bool = commands.flag(description="Remove messages from users who are online", default=False)
    offline: bool = commands.flag(description="Remove messages from users who are offline", default=False)
    idle: bool = commands.flag(description="Remove messages from users who are idle", default=False)
    require: Literal["any", "all"] = commands.flag(description='Whether any or all of the flags should be met before deleting messages. Defaults to "all"',default="all",)
    skip: bool = commands.flag(description="Skip the purge confirmation", default=False)
    # fmt: on

    @classmethod
    def parse_flags(cls, argument: str, *, ignore_extra: bool = True):
        # FlagConverter normally requires a value for boolean flags. Move bare
        # boolean flags to the end so they can safely receive `true`, even when
        # other flags follow them. Explicit values such as `-bot false` remain
        # unchanged.
        boolean_flags = (
            "bot|webhooks|embeds|files|emoji|reactions|left|online|offline|idle|skip"
        )
        bare_boolean = (
            rf"(?<!\S)--?(?P<flag>{boolean_flags})(?=\s*(?:--?\w+(?=\s|$)|$))"
        )
        bare_flags = re.findall(bare_boolean, argument)
        if bare_flags:
            argument = re.sub(bare_boolean, "", argument).strip()
            argument = " ".join(
                part
                for part in (argument, *(f"-{flag} true" for flag in bare_flags))
                if part
            )

        parsed = super().parse_flags(argument, ignore_extra=ignore_extra)
        return {
            name: [value.strip() for value in values] for name, values in parsed.items()
        }


class ReactionPurgeFlags(PurgeFlags):
    """Message filters plus reaction name and ID filters.

    ``-reaction`` also accepts ``-reaction_name`` and ``-emoji_name``.
    ``-reaction_id`` also accepts ``-emoji_id``.
    """

    reaction: Optional[str] = commands.flag(
        description="Only clear reactions with this name",
        aliases=["emoji_name", "reaction_name"],
        default=None,
    )
    reaction_id: Optional[int] = commands.flag(
        description="Only clear the custom reaction with this emoji ID",
        aliases=["emoji_id"],
        default=None,
    )


def _reaction_emoji_name(emoji: object) -> str:
    name = getattr(emoji, "name", None)
    return str(name) if name else str(emoji)


def _reaction_matches(
    reaction: discord.Reaction,
    flags: ReactionPurgeFlags,
) -> bool:
    emoji = reaction.emoji
    if flags.reaction and (
        _reaction_emoji_name(emoji).casefold() != flags.reaction.casefold()
    ):
        return False
    if (
        flags.reaction_id is not None
        and getattr(emoji, "id", None) != flags.reaction_id
    ):
        return False
    return True


def _message_predicate(
    ctx: GuildContext,
    flags: PurgeFlags,
) -> Callable[[discord.Message], bool]:
    predicates: list[Callable[[discord.Message], Any]] = []
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

    if flags.left:
        predicates.append(lambda m: ctx.guild.get_member(m.author.id) is None)

    if flags.online:
        predicates.append(
            lambda m: getattr(m.author, "status", None) == discord.Status.online
        )

    if flags.offline:
        predicates.append(
            lambda m: getattr(m.author, "status", None) == discord.Status.offline
        )

    if flags.idle:
        predicates.append(
            lambda m: getattr(m.author, "status", None) == discord.Status.idle
        )

    if flags.contains:
        predicates.append(lambda m: flags.contains in m.content)  # type: ignore

    if flags.starts:
        starts = flags.starts
        predicates.append(lambda m: m.content.startswith(starts))

    if flags.ends:
        ends = flags.ends
        predicates.append(lambda m: m.content.endswith(ends))

    if not predicates:
        # If nothing is passed then default to `True` to emulate ?purge all behaviour
        predicates.append(lambda m: True)

    op = all if flags.require == "all" else any
    return lambda message: op(predicate(message) for predicate in predicates)


"""
-user          Messages from a specific user
-channel       Messages from a specific channel
-contains      Messages that contains this text (case sensitive)
-starts        Messages that start with this text (case sensitive)
-ends          Messages that end with this text (case sensitive)
-after         Messages that come after this message ID
-before        Messages that come before this message ID
-bot           Messages from bots (not webhooks!)
-webhooks      Messages from webhooks
-embeds        Messages that have embeds
-files         Messages that have attachments
-emoji         Messages that have custom emojis
-reactions     Messages that have reactions
-left          Messages from users who have left the server
-online        Messages from users who are online
-offline       Messages from users who are offline
-idle          Messages from users who are idle
-require       Whether any or all of the flags should be met before deleting messages. Defaults to "all"
-skip          Skip the purge confirmation
"""


class PurgeCog(Cog):
    @commands.hybrid_group(
        name="purge",
        fallback="start",
        extras={"usage": "[amount] [flags...]"},
    )
    @commands.bot_has_permissions(manage_messages=True)
    @commands.has_guild_permissions(manage_messages=True)
    @commands.guild_only()
    @app_commands.describe(
        amount="Maximum number of messages to scan and delete (defaults to 100)."
    )
    async def purge(
        self,
        ctx: GuildContext,
        amount: Optional[commands.Range[int, 1, 2000]] = None,
        *,
        flags: PurgeFlags,
    ):
        """
        Mass delete messages from the current channel.

        -# -user          Messages from a specific user
        -# -channel       Messages from a specific channel
        -# -contains      Messages that contain this text (case sensitive)
        -# -starts        Messages that start with this text (case sensitive)
        -# -ends          Messages that end with this text (case sensitive)
        -# -after         Messages that come after this message ID
        -# -before        Messages that come before this message ID
        -# -bot           Messages from bots (not webhooks!)
        -# -webhooks      Messages from webhooks
        -# -embeds        Messages that have embeds
        -# -files         Messages that have attachments
        -# -emoji         Messages that have custom emojis
        -# -reactions     Messages that have reactions
        -# -left          Messages from users who have left the server
        -# -online        Messages from users who are online
        -# -offline       Messages from users who are offline
        -# -idle          Messages from users who are idle
        -# -require       Whether any or all of the flags should be met before deleting messages.
        -# -skip          Skip the purge confirmation.
        """
        amount_was_given = amount is not None
        has_filter_flags = any(
            (
                flags.user,
                flags.channel,
                flags.contains,
                flags.starts,
                flags.ends,
                flags.after,
                flags.before,
                flags.bot,
                flags.webhooks,
                flags.embeds,
                flags.files,
                flags.emoji,
                flags.reactions,
                flags.left,
                flags.online,
                flags.offline,
                flags.idle,
            )
        )

        if flags.after and amount is None:
            amount = 2000

        if amount is None:
            amount = 100

        channel: PurgeChannels = flags.channel if flags.channel else ctx.channel  # type: ignore
        confirmation_message: Optional[discord.Message] = None
        needs_confirmation = not flags.skip and (
            amount > 100 or (not amount_was_given and not has_filter_flags)
        )
        if needs_confirmation:
            confirmation_message = await ctx.prompt(
                f"This will delete up to {amount} messages in {channel.mention}. Continue?",
                confirm_label="Yes",
                cancel_label="No",
            )
            if not confirmation_message:
                return await ctx.send("Purge cancelled.")

        async with ctx.typing():
            predicate = _message_predicate(ctx, flags)

            before = discord.Object(id=flags.before) if flags.before else None
            after = discord.Object(id=flags.after) if flags.after else None
            if ctx.interaction is None or not ctx.interaction.response.is_done():
                await ctx.defer()

            if before is None and ctx.interaction is not None:
                # Keep the interaction response out of the purge. A confirmation
                # prompt may already have deleted the original response, so reuse
                # its returned message object when available.
                before = (
                    confirmation_message or await ctx.interaction.original_response()
                )

            ctx.bot.dispatch("logger_purge_start", ctx.guild.id, channel.id)
            try:
                deleted = await channel.purge(
                    limit=amount, before=before, after=after, check=predicate
                )
            except discord.Forbidden:
                ctx.bot.dispatch("logger_purge_cancel", ctx.guild.id, channel.id)
                return await ctx.send("I do not have permissions to delete messages.")
            except discord.HTTPException as e:
                ctx.bot.dispatch("logger_purge_cancel", ctx.guild.id, channel.id)
                return await ctx.send(f"Error: {e} (try a smaller search?)")

            if len(deleted) > 1:
                ctx.bot.dispatch("logger_purge", ctx.guild, channel, tuple(deleted))
            else:
                ctx.bot.dispatch("logger_purge_cancel", ctx.guild.id, channel.id)
            ctx.bot.dispatch(
                "logger_fishie_moderation",
                ctx.guild,
                "Messages purged by Fishie",
                f"Fishie purged {len(deleted):,} messages in {channel.mention}.",
                None,
                ctx.author,
                "Fishie purge command",
            )
            await ctx.send(f"Deleted {plural(len(deleted)):message}.", delete_after=7)

    @purge.command(
        name="reactions",
        description="Clear reactions from messages matching the purge filters.",
        extras={"usage": "[amount] [flags...]"},
    )
    @commands.bot_has_permissions(manage_messages=True)
    @commands.has_guild_permissions(manage_messages=True)
    @commands.guild_only()
    @app_commands.describe(
        amount="Maximum number of messages to scan (defaults to 100)."
    )
    async def purge_reactions(
        self,
        ctx: GuildContext,
        amount: Optional[commands.Range[int, 1, 2000]] = None,
        *,
        flags: ReactionPurgeFlags,
    ) -> None:
        """
        Clear reactions from messages in the current channel.

        The message filters are the same as the main purge command. Use
        ``-reaction_name`` or ``-reaction_id`` to target a specific reaction.

        -# -user          Messages from a specific user
        -# -channel       Messages from a specific channel
        -# -contains      Messages that contain this text (case sensitive)
        -# -starts        Messages that start with this text (case sensitive)
        -# -ends          Messages that end with this text (case sensitive)
        -# -after         Messages that come after this message ID
        -# -before        Messages that come before this message ID
        -# -bot           Messages from bots (not webhooks!)
        -# -webhooks      Messages from webhooks
        -# -embeds        Messages that have embeds
        -# -files         Messages that have attachments
        -# -emoji         Messages that have custom emoji
        -# -reactions     Messages that have reactions
        -# -left          Messages from users who have left the server
        -# -online        Messages from users who are online
        -# -offline       Messages from users who are offline
        -# -idle          Messages from users who are idle
        -# -reaction      Reaction/emoji name to clear (aliases: -reaction_name, -emoji_name)
        -# -reaction_id   Custom reaction emoji ID to clear (alias: -emoji_id)
        -# -require       Whether any or all message filters should be met.
        -# -skip          Skip the purge confirmation.
        """
        amount_was_given = amount is not None
        has_filter_flags = any(
            (
                flags.user,
                flags.channel,
                flags.contains,
                flags.starts,
                flags.ends,
                flags.after,
                flags.before,
                flags.bot,
                flags.webhooks,
                flags.embeds,
                flags.files,
                flags.emoji,
                flags.reactions,
                flags.left,
                flags.online,
                flags.offline,
                flags.idle,
                flags.reaction,
                flags.reaction_id,
            )
        )

        if flags.after and amount is None:
            amount = 2000
        if amount is None:
            amount = 100

        channel: PurgeChannels = flags.channel if flags.channel else ctx.channel  # type: ignore
        confirmation_message: Optional[discord.Message] = None
        needs_confirmation = not flags.skip and (
            amount > 100 or (not amount_was_given and not has_filter_flags)
        )
        if needs_confirmation:
            confirmation_message = await ctx.prompt(
                f"This will clear reactions from up to {amount} messages in {channel.mention}. Continue?",
                confirm_label="Yes",
                cancel_label="No",
            )
            if not confirmation_message:
                await ctx.send("Reaction purge cancelled.")
                return

        async with ctx.typing():
            if ctx.interaction is None or not ctx.interaction.response.is_done():
                await ctx.defer()

            before = discord.Object(id=flags.before) if flags.before else None
            after = discord.Object(id=flags.after) if flags.after else None
            if before is None and ctx.interaction is not None:
                before = (
                    confirmation_message or await ctx.interaction.original_response()
                )

            message_check = _message_predicate(ctx, flags)
            scanned = matched_messages = cleared = failed = 0
            try:
                async for message in channel.history(
                    limit=amount,
                    before=before,
                    after=after,
                ):
                    scanned += 1
                    if not message_check(message):
                        continue
                    message_had_match = False
                    for reaction in list(message.reactions):
                        if not _reaction_matches(reaction, flags):
                            continue
                        message_had_match = True
                        try:
                            await reaction.clear()
                        except (
                            discord.Forbidden,
                            discord.NotFound,
                            discord.HTTPException,
                        ):
                            failed += 1
                        else:
                            cleared += 1
                    if message_had_match:
                        matched_messages += 1
            except discord.Forbidden:
                await ctx.send(
                    "I do not have permissions to read or clear reactions in that channel."
                )
                return
            except discord.HTTPException as error:
                await ctx.send(f"Error: {error} (try a smaller search?)")
                return

            if not cleared and not failed:
                await ctx.send(f"No matching reactions found in {scanned} messages.")
                return

            result = (
                f"Cleared {plural(cleared):reaction} from "
                f"{plural(matched_messages):message}."
            )
            if failed:
                result += f" Failed to clear {plural(failed):reaction}."
            ctx.bot.dispatch(
                "logger_fishie_moderation",
                ctx.guild,
                "Reactions purged by Fishie",
                f"Fishie cleared {cleared:,} reactions in {channel.mention}.",
                None,
                ctx.author,
                "Fishie purge reactions command",
            )
            await ctx.send(result, delete_after=7)

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

    @purge.command(
        name="invites",
        aliases=("purge-invites",),
        description="Purge invites for a channel or the entire server.",
    )
    @commands.has_guild_permissions(manage_guild=True, manage_channels=True)
    @commands.bot_has_permissions(manage_guild=True, manage_channels=True)
    @commands.guild_only()
    @app_commands.describe(
        limit="Maximum number of invites to delete.",
        channel="Optional channel whose invites should be deleted.",
    )
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
