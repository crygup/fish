from __future__ import annotations
import asyncio
import os

import re
from typing import (
    TYPE_CHECKING,
    List,
    Literal,
    Optional,
    Set,
    TypeAlias,
    Union,
    TypeVar,
)

import discord

from utils import (
    AuthorView,
    BlankException,
    DeleteView,
    can_execute_action,
    RoleConverter,
    ActionReason,
)

if TYPE_CHECKING:
    from cogs.context import Context

Mode: TypeAlias = Union[Literal["ban"], Literal["kick"], Literal["nick"]]


class ActionDropdown(discord.ui.Select):
    view: ActionView

    def __init__(
        self,
        ctx: Context,
        mode: Mode,
    ):
        self.ctx = ctx
        self.mode = mode

        options = [
            discord.SelectOption(
                label=f"Username",
                value="username",
                description="The regex that usernames must match.",
            ),
            discord.SelectOption(
                label=f"Nickname",
                value="nickname",
                description="The regex that nickname must match.",
            ),
            discord.SelectOption(
                label=f"No avatar",
                value="no_avatar",
                description="Matches members with no avatar.",
            ),
            discord.SelectOption(
                label=f"No roles",
                value="no_role",
                description="Matches members with no roles.",
            ),
            discord.SelectOption(
                label=f"Hoisters",
                value="hoisters",
                description="Matches members if they have a special character to hoist themselves to the top of the member list.",
            ),
            discord.SelectOption(
                label=f"Only bots",
                value="bots",
                description="Matches only bots.",
            ),
            discord.SelectOption(
                label=f"Only members",
                value="members",
                description="Matches only members.",
            ),
            discord.SelectOption(
                label=f"Has specific role",
                value="has_role",
                description="Matches members that have specified role.",
            ),
        ]

        super().__init__(
            min_values=1,
            max_values=len(options),
            options=options,
            placeholder="Choose some options.",
        )

    async def ban_method(
        self,
        interaction: discord.Interaction,
        members: Set[discord.Member],
        reason: str,
    ):
        ctx = self.ctx
        assert interaction.message is not None

        count = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await interaction.message.edit(content=f"Banned {count}/{len(members)}")

    async def kick_method(
        self,
        interaction: discord.Interaction,
        members: Set[discord.Member],
        reason: str,
    ):
        ctx = self.ctx
        assert interaction.message is not None

        count = 0
        for member in members:
            try:
                await ctx.guild.kick(member, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await interaction.message.edit(content=f"Kicked {count}/{len(members)}")

    async def nick_method(
        self,
        interaction: discord.Interaction,
        members: Set[discord.Member],
        reason: str,
        nick: str,
    ):
        ctx = self.ctx
        assert interaction.message is not None

        count = 0
        for member in members:
            try:
                await member.edit(nick=nick, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await interaction.message.edit(content=f"Nicked {count}/{len(members)}")

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        author = ctx.author
        bot = ctx.bot
        members = []
        assert interaction.message is not None

        if ctx.guild.chunked:
            members = ctx.guild.members
        else:
            async with ctx.typing():
                await ctx.guild.chunk(cache=True)
            members = ctx.guild.members

        predicates = [
            lambda m: isinstance(m, discord.Member)
            and can_execute_action(ctx, author, m),  # Only if applicable
            lambda m: m.discriminator != "0000",  # No deleted users
        ]

        # these can take awhile so rather just differ beforehand
        await interaction.response.defer()

        if "no_avatar" in self.values:
            predicates.append(lambda m: m.avatar is None)

        if "no_role" in self.values:
            predicates.append(lambda m: len(m.roles) == 1)

        if "bots" in self.values:
            predicates.append(lambda m: m.bot)

        if "members" in self.values:
            predicates.append(lambda m: not m.bot)

        if "bots" in self.values and "members" in self.values:
            raise BlankException("Can not use both 'bots' and 'members' option here.")

        if "hoisters" in self.values:
            predicates.append(lambda m: m.display_name.startswith("!"))

        if "username" in self.values:
            await interaction.message.edit(
                content="Please type in chat the username you want to scan for.",
                view=None,
            )
            try:
                try:
                    msg: discord.Message = await bot.wait_for(
                        "message",
                        check=lambda m: m.channel.id == ctx.channel.id
                        and m.author.id == ctx.author.id,
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    await interaction.message.edit(
                        content="Timed out, please run the command again.", view=None
                    )
                    return

                _regex = re.compile(msg.content)
            except re.error as e:
                return await ctx.send(f"Invalid regex passed to `username:` error: {e}")
            else:
                predicates.append(lambda m, x=_regex: x.match(m.name))

        if "nickname" in self.values:
            await interaction.message.edit(
                content="Please type in chat the nickname you want to scan for.",
                view=None,
            )
            try:
                try:
                    msg: discord.Message = await bot.wait_for(
                        "message",
                        check=lambda m: m.channel.id == ctx.channel.id
                        and m.author.id == ctx.author.id,
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    await interaction.message.edit(
                        content="Timed out, please run the command again.", view=None
                    )
                    return

                _regex = re.compile(msg.content)
            except re.error as e:
                return await ctx.send(f"Invalid regex passed to `nickname:` error: {e}")
            else:
                predicates.append(lambda m, x=_regex: m.nick and x.match(m.nick))

        if "has_role" in self.values:
            await interaction.message.edit(
                content="Please type in chat the role name, id or mention, of the role you want to scan for.",
                view=None,
            )

            try:
                msg: discord.Message = await bot.wait_for(
                    "message",
                    check=lambda m: m.channel.id == ctx.channel.id
                    and m.author.id == ctx.author.id,
                    timeout=30,
                )
            except asyncio.TimeoutError:
                await interaction.message.edit(
                    content="Timed out, please run the command again.", view=None
                )
                return

            role = await RoleConverter().convert(ctx, msg.content)

            predicates.append(lambda m: role.id in [role.id for role in m.roles])

        members = {m for m in members if all(p(m) for p in predicates)}
        if len(members) == 0:
            return await interaction.message.edit(
                content="No members found matching criteria.", view=None
            )
        nick = None
        if self.mode == "nick":
            await interaction.message.edit(
                content="Please type in chat the new nickname for all members, you can also type **None** to reset everyone's nickname.",
                view=None,
            )

            try:
                msg: discord.Message = await bot.wait_for(
                    "message",
                    check=lambda m: m.channel.id == ctx.channel.id
                    and m.author.id == ctx.author.id,
                    timeout=30,
                )
            except asyncio.TimeoutError:
                await interaction.message.edit(
                    content="Timed out, please run the command again.", view=None
                )
                return

            nick = msg.content if msg.content.lower() != "none" else None

        await interaction.message.edit(
            content=f"Please type in chat the reasoning for this, this will also start the action on {len(members):,} members. \n\nType **Abort** to stop.",
            view=None,
        )

        try:
            msg: discord.Message = await bot.wait_for(
                "message",
                check=lambda m: m.channel.id == ctx.channel.id
                and m.author.id == ctx.author.id,
                timeout=30,
            )
        except asyncio.TimeoutError:
            await interaction.message.edit(
                content="Timed out, please run the command again.", view=None
            )
            return

        reason = msg.content

        if reason.lower() == "abort":
            await interaction.message.edit(content="Aborting.")
            return

        reason = await ActionReason().convert(ctx, reason)

        functions = {
            "ban": self.ban_method,
            "kick": self.kick_method,
            "nick": self.nick_method,
        }

        await interaction.message.edit(content="Working on it.")

        await functions[self.mode](interaction, members, reason, nick)


class ActionView(AuthorView):
    def __init__(self, ctx: Context, mode: Mode):
        super().__init__(ctx)
        self.members: Set[discord.Member] = set()
        self.reason: str = ""
        self.add_item(ActionDropdown(ctx, mode))
        self.add_item(DeleteView(ctx).delete)
