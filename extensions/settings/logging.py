from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

import discord
from discord.ext import commands

from core import Cog
from utils import AuthorView

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context, GuildContext


class Dropdown(discord.ui.Select):
    def __init__(
        self, ctx: Context, data: Dict[str, List], guild_id: Optional[int] = None
    ):
        options = []
        self.ctx = ctx
        self.data = data
        self.guild_id = guild_id

        for key, items in data.items():
            options.append(
                discord.SelectOption(label=items[0], emoji=items[1], value=key),
            )

        super().__init__(
            placeholder="Choose which logging to opt out from",
            min_values=1,
            max_values=1,
            options=options,
        )

    def update_options(self):
        self.options.clear()
        for key, items in self.data.items():
            self.options.append(
                discord.SelectOption(label=items[0], emoji=items[1], value=key)
            )

    async def user_opt(self):
        value = self.values[0]
        ctx = self.ctx

        results = await ctx.bot.redis.smembers(f"opted_out:{ctx.author.id}")

        if value in results:
            sql = """UPDATE opted_out SET items = array_remove(opted_out.items, $1) WHERE user_id = $2"""

            await ctx.bot.pool.execute(sql, value, ctx.author.id)
            await ctx.bot.redis.srem(f"opted_out:{ctx.author.id}", value)
            emoji = "\U0001f7e2"
        else:
            sql = """
            INSERT INTO opted_out (user_id, items) VALUES ($1, ARRAY [$2]) 
            ON CONFLICT (user_id) DO UPDATE
            SET items = array_append(opted_out.items, $2) 
            WHERE opted_out.user_id = $1
            """

            await ctx.bot.pool.execute(sql, ctx.author.id, value)
            await ctx.bot.redis.sadd(f"opted_out:{ctx.author.id}", value)
            emoji = "\U0001f534"

        self.data.update({value: [self.data[value][0], emoji]})
        self.update_options()

    async def guild_opt(self):
        value = self.values[0]
        ctx = self.ctx

        results = await ctx.bot.redis.smembers(f"guild_opted_out:{self.guild_id}")

        if value in results:
            sql = """UPDATE guild_opted_out SET items = array_remove(guild_opted_out.items, $1) WHERE guild_id = $2"""

            await ctx.bot.pool.execute(sql, value, self.guild_id)
            await ctx.bot.redis.srem(f"guild_opted_out:{self.guild_id}", value)
            emoji = "\U0001f7e2"
        else:
            sql = """
            INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, ARRAY [$2]) 
            ON CONFLICT (guild_id) DO UPDATE
            SET items = array_append(guild_opted_out.items, $2) 
            WHERE guild_opted_out.guild_id = $1
            """

            await ctx.bot.pool.execute(sql, self.guild_id, value)
            await ctx.bot.redis.sadd(f"guild_opted_out:{self.guild_id}", value)
            emoji = "\U0001f534"

        self.data.update({value: [self.data[value][0], emoji]})
        self.update_options()

    async def callback(self, interaction: discord.Interaction):
        if self.guild_id:
            await self.guild_opt()
        else:
            await self.user_opt()

        if interaction.message is None:
            raise commands.BadArgument("No message somehow.")

        await interaction.message.edit(view=self.view)
        await interaction.response.defer()


class DropdownView(AuthorView):
    def __init__(
        self, ctx: Context, data: Dict[str, List], guild_id: Optional[int] = None
    ):
        super().__init__(ctx)

        self.add_item(Dropdown(ctx, data, guild_id=guild_id))


class Logging(Cog):
    @commands.hybrid_group(name="logging", fallback="user", invoke_without_command=True)
    async def logging(self, ctx: Context):
        sql = """SELECT * FROM opted_out WHERE user_id = $1"""
        records = await self.bot.pool.fetchrow(sql, ctx.author.id)
        data = {
            "avatar": ["Avatar logging", "\U0001f7e2"],
            "status": ["Status logging", "\U0001f7e2"],
            "username": ["Username logging", "\U0001f7e2"],
            "display": ["Display name logging", "\U0001f7e2"],
            "nickname": ["Nickname logging", "\U0001f7e2"],
            "discrim": ["Discriminator logging", "\U0001f7e2"],
            "joins": ["Server join logging", "\U0001f7e2"],
        }

        if bool(records):
            for item in records["items"]:
                if data.get(item):
                    data.update({item: [data[item][0], "\U0001f534"]})

        await ctx.send(view=DropdownView(ctx, data))

    @logging.command(name="guild", aliases=("server",))
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def logging_server(self, ctx: GuildContext):
        sql = """SELECT * FROM guild_opted_out WHERE guild_id = $1"""
        records = await self.bot.pool.fetchrow(sql, ctx.guild.id)
        data = {
            "name": ["Name logging", "\U0001f7e2"],
            "icon": ["Icon logging", "\U0001f7e2"],
        }

        if bool(records):
            for item in records["items"]:
                if data.get(item):
                    data.update({item: [data[item][0], "\U0001f534"]})

        await ctx.send(view=DropdownView(ctx, data, guild_id=ctx.guild.id))
