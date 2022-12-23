from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Dict, Optional

import discord
from discord.ext import commands

from utils import FieldPageSource, Pager, has_mod, is_table, to_bytes

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Table(bot))


class Table(commands.Cog, name="table", command_attrs=dict(hidden=True)):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:
        table_check = is_table(ctx)
        mod_check = has_mod(ctx)

        if not table_check or not mod_check:
            raise commands.CheckFailure

        return True

    @commands.group(name="booster", aliases=("boost",), invoke_without_command=True)
    async def booster(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @booster.command(name="roles")
    async def booster_roles(self, ctx: Context):
        sql = """
        SELECT * FROM table_boosters ORDER BY created_at DESC
        """

        results = await self.bot.pool.fetch(sql)

        if not results:
            await ctx.send("No boosters found.")
            return

        def get_role_name(role_id: int) -> str:
            role = ctx.guild.get_role(role_id)

            return role.name if role else "\U00002753 Not Found"

        def get_name(user_id: int) -> str:
            user = self.bot.get_user(user_id)

            return user.mention if user else "\U00002753 Not Found"

        entries = [
            (
                get_role_name(r["role_id"]),
                f'Member: {get_name(r["user_id"])} \nCreated: {discord.utils.format_dt(r["created_at"], "d")} \nBy: {get_name(r["author_id"])}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries, per_page=2)
        source.embed.title = f"Table booster roles"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @booster.group(name="role", invoke_without_command=True)
    async def booster_role(self, ctx: Context, *, member: discord.Member):
        sql = """
        SELECT * FROM table_boosters WHERE user_id = $1
        """

        results = await self.bot.pool.fetchrow(sql, member.id)

        if not results:
            await ctx.send("No booster role found.")
            return

        role_author = await self.bot.getch_user(results["author_id"])
        role = ctx.guild.get_role(results["role_id"])
        not_found = "\U00002753 Not Found"

        description = f"""
        Role: {role.mention if role else {not_found}}
        User: {member.mention}
        Author: {role_author.mention}
        Created: {discord.utils.format_dt(results['created_at'], 'R')}  |  {discord.utils.format_dt(results['created_at'], 'd')}
        """

        embed = discord.Embed(
            color=self.bot.embedcolor, description=textwrap.dedent(description)
        )

        embed.set_author(
            name=f"{member}'s booster role", icon_url=member.display_avatar.url
        )

        await ctx.send(embed=embed)

    @booster_role.command(name="create")
    async def booster_create(
        self,
        ctx: Context,
        member: discord.Member,
        name: str,
        color: Optional[discord.Color],
        icon: Optional[str],
    ):
        sql = """
        SELECT user_id FROM table_boosters WHERE user_id = $1
        """

        results = await self.bot.pool.fetchrow(sql, member.id)

        if results:
            await ctx.send("User already has a booster role.")
            return

        icon_bytes: bytes | str = await to_bytes(ctx.session, icon) if icon else ""
        color_value = color.value if color else 0

        role = await ctx.guild.create_role(
            name=name,
            color=color_value,
            display_icon=icon_bytes,
        )

        sql = """
        INSERT INTO table_boosters (user_id, role_id, author_id, created_at)
        VALUES ($1, $2, $3, $4)
        """

        await self.bot.pool.execute(
            sql, member.id, role.id, ctx.author.id, discord.utils.utcnow()
        )

        await member.add_roles(role)

        await ctx.send(f"Gave {member} the role {role}.")

    @booster_role.command(name="remove", aliases=("delete",))
    async def booster_remove(self, ctx: Context, member: discord.Member):
        sql = """
        SELECT * FROM table_boosters WHERE user_id = $1
        """

        results: Dict = await self.bot.pool.fetchrow(sql, member.id)  # type: ignore

        if not results:
            await ctx.send("No booster role found.")
            return

        role = ctx.guild.get_role(results["role_id"])

        if role is None:
            await ctx.send("No booster role found.")
            return

        sql = """   
        DELETE FROM table_boosters WHERE role_id = $1
        """

        await self.bot.pool.execute(sql, role.id)

        await role.delete()

        await ctx.send(f"Successfully removed {role} from {member}.")
