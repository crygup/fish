from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

import asyncpg
import discord
from discord.ext import commands

from utils import BlankException, plural, to_bytesio

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class DataCog(CogBase):
    @commands.group(name="data", invoke_without_command=True)
    async def data_group(self, ctx: Context):
        """Manage your data I store on you."""
        await ctx.send_help(ctx.command)

    @data_group.group(name="remove", aliases=("delete",), invoke_without_command=True)
    async def remove_group(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @remove_group.group(
        name="avatars", aliases=("pfps", "avs", "avys"), invoke_without_command=True
    )
    async def delete_avatars(self, ctx: Context, id: Optional[int] = None):
        avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
            "SELECT * FROM avatars WHERE user_id = $1 ORDER BY created_at DESC",
            ctx.author.id,
        )
        if id:
            try:
                avatar = avatars[id - 1]
            except:
                return await ctx.send("Couldn't find an avatar at that index for you.")

            file = discord.File(
                await to_bytesio(ctx.session, avatar["avatar"]), filename="avatar.png"
            )

            prompt = await ctx.prompt(
                "Are you sure you want to delete this avatar from your avatars? This action **CANNOT** be undone.",
                files=[file],
                timeout=10,
            )

            if not prompt:
                return await ctx.send("Good, I didn't want to delete it anyway.")

            await self.bot.pool.execute(
                "DELETE FROM avatars WHERE avatar_key = $1 AND user_id = $2",
                avatar["avatar_key"],
                ctx.author.id,
            )

            return await ctx.send(f"Successfully deleted that avatar.")

        prompt = await ctx.prompt(
            "Are you sure you want to delete **ALL OF YOUR AVATARS**? This action **CANNOT** be undone.",
            timeout=10,
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM avatars WHERE user_id = $1 RETURNING avatar",
            ctx.author.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):avatar}")

    @delete_avatars.command(name="guild", aliases=("server",))
    async def delete_guild_avatars(
        self,
        ctx: Context,
        guild: Optional[discord.Guild] = commands.CurrentGuild,
        id: Optional[int] = None,
    ):
        guild = guild or ctx.guild
        avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
            "SELECT * FROM guild_avatars WHERE member_id = $1 AND guild_id = $2 ORDER BY created_at DESC",
            ctx.author.id,
            guild.id,
        )
        if id:
            try:
                avatar = avatars[id - 1]
            except:
                return await ctx.send("Couldn't find an avatar at that index for you.")

            file = discord.File(
                await to_bytesio(ctx.session, avatar["avatar"]), filename="avatar.png"
            )

            prompt = await ctx.prompt(
                "Are you sure you want to delete this avatar from your guild avatars? This action **CANNOT** be undone.",
                files=[file],
                timeout=10,
            )

            if not prompt:
                return await ctx.send("Good, I didn't want to delete it anyway.")

            await self.bot.pool.execute(
                "DELETE FROM guild_avatars WHERE avatar_key = $1 AND member_id = $2 AND guild_id = $3",
                avatar["avatar_key"],
                ctx.author.id,
                guild.id,
            )

            return await ctx.send(f"Successfully deleted that avatar.")

        prompt = await ctx.prompt(
            "Are you sure you want to delete **ALL OF YOUR AVATARS**? This action **CANNOT** be undone.",
            timeout=10,
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM guild_avatars WHERE member_id = $1 AND guild_id = $2 RETURNING avatar",
            ctx.author.id,
            guild.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):guild avatar}")

    @remove_group.command(name="usernames")
    async def remove_usernames(self, ctx: Context):
        records = await ctx.pool.fetch(
            """SELECT * FROM username_logs WHERE user_id = $1""", ctx.author.id
        )

        if records == []:
            raise BlankException("I have no username history on record for you.")

        prompt = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(records)):username}? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM username_logs WHERE user_id = $1 RETURNING username",
            ctx.author.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):username}")

    @remove_group.command(name="discrims", aliases=("discriminators",))
    async def remove_discrims(self, ctx: Context):
        records = await ctx.pool.fetch(
            """SELECT * FROM discrim_logs WHERE user_id = $1""", ctx.author.id
        )

        if records == []:
            raise BlankException("I have no discriminator history on record for you.")

        prompt = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(records)):discriminator}? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM discrim_logs WHERE user_id = $1 RETURNING discrim",
            ctx.author.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):discriminator}")

    @remove_group.command(name="nicknames")
    async def remove_nicknames(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        guild = guild or ctx.guild
        records = await ctx.pool.fetch(
            """SELECT * FROM nickname_logs WHERE user_id = $1 AND guild_id = $2""",
            ctx.author.id,
            guild.id,
        )

        if records == []:
            raise BlankException(
                "I have no nickname history on record for you for this guild."
            )

        prompt = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(records)):nickname}? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM nickname_logs WHERE user_id = $1 AND guild_id = $2 RETURNING nickname",
            ctx.author.id,
            guild.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):username}")

    @remove_group.command(name="uptime")
    async def remove_uptime(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        guild = guild or ctx.guild
        records = await ctx.pool.fetch(
            """SELECT * FROM uptime_logs WHERE user_id = $1""",
            ctx.author.id,
            guild.id,
        )

        if records == []:
            raise BlankException("I have no uptime history on record for you.")

        prompt = await ctx.prompt(
            f"Are you sure you want to opt out of having your uptime tracked?? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to do that anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM uptime_logs WHERE user_id = $1",
            ctx.author.id,
            guild.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):username}")

    @remove_group.group(name="guild", invoke_without_command=True)
    async def remove_guild_group(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @remove_guild_group.command(name="icons")
    @commands.has_guild_permissions(administrator=True)
    async def remove_guild_icons(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        guild = guild or ctx.guild

        records = await ctx.pool.fetch(
            """SELECT * FROM guild_icons WHERE guild_id = $1""",
            guild.id,
        )

        if records == []:
            raise BlankException("I have no icon history on record for this guild.")

        prompt = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(records)):icon}? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM guild_ioncs WHERE guild_id = $1 RETURNING icon",
            guild.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):icon}")

    @remove_guild_group.command(name="bans")
    @commands.has_guild_permissions(administrator=True)
    async def remove_guild_bans(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        guild = guild or ctx.guild

        records = await ctx.pool.fetch(
            """SELECT * FROM guild_bans WHERE guild_id = $1""",
            guild.id,
        )

        if records == []:
            raise BlankException("I have no ban history on record for this guild.")

        prompt = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(records)):ban log}? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM guild_bans WHERE guild_id = $1 RETURNING target_id",
            guild.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):ban log}")

    @remove_guild_group.command(name="names")
    @commands.has_guild_permissions(administrator=True)
    async def remove_guild_names(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        guild = guild or ctx.guild

        records = await ctx.pool.fetch(
            """SELECT * FROM guild_name_logs WHERE guild_id = $1""",
            guild.id,
        )

        if records == []:
            raise BlankException("I have no name history on record for this guild.")

        prompt = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(records)):guild name}? This action **CANNOT** be undone."
        )

        if not prompt:
            return await ctx.send("Good, I didn't want to delete them anyway.")

        deleted = await self.bot.pool.fetch(
            "DELETE FROM guild_name_logs WHERE guild_id = $1 RETURNING target_id",
            guild.id,
        )

        await ctx.send(f"Deleted {plural(len(deleted)):guild name}")
