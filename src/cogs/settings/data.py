from __future__ import annotations

from typing import TYPE_CHECKING, List, Literal, Optional, Union

import asyncpg
import discord
from discord.ext import commands

from utils import BlankException, plural, to_bytesio

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class DataCog(CogBase):
    async def user_remove(
        self,
        ctx: Context,
        table: str,
        author_id: int,
        *,
        index: Optional[int] = None,
    ):
        if index:
            first_sql = f"SELECT COUNT(*) FROM {table} WHERE user_id = $1 AND id = $2"
            second_sql = f"""
            DELETE FROM {table}
            WHERE user_id = $1 AND id = $2 
            RETURNING *
            """
            args = (author_id, index)
            message = f"Are you sure you want to remove index {index}"
        else:
            first_sql = f"""SELECT COUNT(*) FROM {table} WHERE user_id = $1"""
            second_sql = f"""
            DELETE FROM {table}
            WHERE user_id = $1
            RETURNING *
            """
            args = (author_id,)
            message = "Are you sure you want to remove **ALL** of your data? this cannot be undone."

        results = await self.bot.pool.fetchval(first_sql, *args)
        if results == 0:
            raise BlankException("No results found.")

        prompt = await ctx.prompt(message)

        if not prompt:
            raise BlankException("Well I didn't want to remove it anyway.")

        await self.bot.pool.fetchrow(second_sql, *args)

        await ctx.send("Removed.")

    async def member_remove(
        self,
        ctx: Context,
        table: str,
        author_id: int,
        guild_id: int,
        author_method: Literal["user_id", "member_id"],
        *,
        index: Optional[int] = None,
    ):
        if index:
            first_sql = f"SELECT COUNT(*) FROM {table} WHERE {author_method} = $1 AND id = $2 AND guild_id =  $3"
            second_sql = f"""
            DELETE FROM {table}
            WHERE {author_method} = $1 AND id = $2 AND guild_id = $4
            RETURNING *
            """
            args = (author_id, index, guild_id)
            message = f"Are you sure you want to remove index {index}"
        else:
            first_sql = f"""SELECT COUNT(*) FROM {table} WHERE {author_method} = $1 AND guild_id = $2"""
            second_sql = f"""
            DELETE FROM {table}
            WHERE {author_method} = $1 AND guild_id = $2
            RETURNING *
            """
            args = (author_id, guild_id)
            message = "Are you sure you want to remove **ALL** of your data? this cannot be undone."

        results = await self.bot.pool.fetchval(first_sql, *args)
        if results == 0:
            raise BlankException("No results found.")

        prompt = await ctx.prompt(message)

        if not prompt:
            raise BlankException("Well I didn't want to remove it anyway.")

        await self.bot.pool.fetchrow(second_sql, *args)

        await ctx.send("Removed.")

    @commands.group(name="data", invoke_without_command=True)
    async def data_group(self, ctx: Context):
        """Manage your data I store on you."""
        await ctx.send_help(ctx.command)

    @data_group.group(
        name="remove", aliases=("delete", "r", "d"), invoke_without_command=True
    )
    async def remove_group(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @remove_group.group(name="avatars", invoke_without_command=True)
    async def remove_avatars(self, ctx: Context, index: Optional[int] = None):
        await self.user_remove(ctx, "avatars", ctx.author.id, index=index)

    @remove_avatars.command(name="guild", aliases=("server", "s", "g"))
    async def remove_guild_avatars(
        self,
        ctx: Context,
        guild: Optional[Union[discord.Guild, int]] = commands.CurrentGuild,
        index: Optional[int] = None,
    ):
        guild_id = guild.id if isinstance(guild, discord.Guild) else guild

        if guild_id is None:
            return

        await self.member_remove(
            ctx, "guild_avatars", ctx.author.id, guild_id, "member_id", index=index
        )

    @remove_group.command(name="usernames", aliases=("names",))
    async def remove_usernames(self, ctx: Context, index: Optional[int] = None):
        await self.user_remove(ctx, "username_logs", ctx.author.id, index=index)

    @remove_group.command(name="discrims", aliases=("discriminators",))
    async def discrims(self, ctx: Context, index: Optional[int] = None):
        await self.user_remove(ctx, "discrim_logs", ctx.author.id, index=index)

    @remove_group.command(name="nicknames", aliases=("nicks",))
    async def nicknames(
        self,
        ctx: Context,
        guild: Optional[Union[discord.Guild, int]] = commands.CurrentGuild,
        index: Optional[int] = None,
    ):
        guild_id = guild.id if isinstance(guild, discord.Guild) else guild

        if guild_id is None:
            return

        await self.member_remove(
            ctx, "nickname_logs", ctx.author.id, guild_id, "user_id", index=index
        )

    @remove_group.command(name="joins")
    async def join_logs(
        self,
        ctx: Context,
        guild: Optional[Union[discord.Guild, int]] = commands.CurrentGuild,
        index: Optional[int] = None,
    ):
        guild_id = guild.id if isinstance(guild, discord.Guild) else guild

        if guild_id is None:
            return

        await self.member_remove(
            ctx, "member_join_logs", ctx.author.id, guild_id, "member_id", index=index
        )
