from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import AuthorView, interaction_only, to_image

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


format_table = {
    "avatars": "user_id",
    "guild_avatars": "member_id",
    "status_logs": "user_id",
    "username_logs": "user_id",
    "display_name_logs": "user_id",
    "nickname_logs": "user_id",
    "discrim_logs": "user_id",
    "member_join_logs": "member_id",
}


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
        """Manage your logging settings for the bot"""
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
        """Manage logging for the server"""
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

    @commands.hybrid_group(name="logging-delete", fallback="all", hidden=True)
    @interaction_only()
    async def logging_delete(self, ctx: GuildContext, data: str):
        """Delete your saved logging data."""

        msg = await ctx.prompt(
            f"Are you sure you want to delete ALL your data for {data}? **THIS CANNOT BE UNDONE**",
            ephemeral=True,
            delete_after=False,
        )

        if not msg:
            return await ctx.send("Good choice.", ephemeral=True)

        await msg.edit(
            content="Okay, deleting everything.",
            view=None,
        )

        sql = f"""DELETE FROM {data} WHERE {format_table[data]} = $1"""

        await self.bot.pool.execute(sql, ctx.author.id)

        await msg.edit(content="Okay, the data was deleted.")

    @logging_delete.autocomplete("data")
    async def del_autocomplete(self, _, current: str) -> List[app_commands.Choice[str]]:
        data = {
            "avatars": "Avatars",
            "guild_avatars": "Avatars",
            "status_logs": "Statuses",
            "username_logs": "Usernames",
            "display_name_logs": "Display names",
            "nickname_logs": "Nicknames",
            "discrim_logs": "Discriminators",
            "member_join_logs": "Server joins",
        }

        return [
            app_commands.Choice(name=name, value=value)
            for value, name in data.items()
            if current.lower() in name.lower()
        ]

    async def easy_delete(
        self,
        message: discord.Message,
        prompt: Union[
            Literal["avatars"],
            Literal["guild_avatars"],
            Literal["status_logs"],
            Literal["username_logs"],
            Literal["display_name_logs"],
            Literal["nickname_logs"],
            Literal["discrim_logs"],
            Literal["member_join_logs"],
        ],
        id: int,
        author_id: int,
    ):
        await message.edit(content=f"Deleting ID `{id}`", attachments=[], view=None)

        sql = f"""DELETE FROM {prompt} WHERE id = $1 AND {format_table[prompt]} = $2 RETURNING *"""
        results = await self.bot.pool.fetch(sql, id, author_id)

        if not bool(results):
            return await message.edit(
                content="Nothing was deleted. Maybe try a different ID."
            )

        await message.edit(content=f"Deleted ID `{id}`")

    @logging_delete.command(name="avatar", hidden=True)
    @interaction_only()
    async def ldelete_avatar(self, ctx: GuildContext, id: int, guild: bool = False):
        """Delete a saved avatar."""

        table = ["avatars", "guild_avatars"][guild]
        user = ["user_id", "member_id"][guild]
        avatar = await self.bot.pool.fetchval(
            f"SELECT avatar FROM {table} WHERE {user} = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"This is your avatar with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
            file=discord.File(await to_image(ctx.session, avatar), "avatar.png"),
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(
            message, "guild_avatars" if guild else "avatars", id, ctx.author.id
        )

    @logging_delete.command(name="username", hidden=True)
    @interaction_only()
    async def ldelete_username(
        self, ctx: GuildContext, id: int = commands.param(displayed_name="username")
    ):
        """Delete a saved username."""

        name = await self.bot.pool.fetchval(
            f"SELECT username FROM username_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{name}` is the named saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "username_logs", id, ctx.author.id)

    @ldelete_username.autocomplete("id")
    async def ldu_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        usernames = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(name=username["username"], value=username["id"])
            for username in usernames
            if current.lower() in str(username["username"]).lower()
        ]

    @logging_delete.command(name="nickname", hidden=True)
    @interaction_only()
    async def ldelete_nickname(
        self, ctx: GuildContext, id: int = commands.param(displayed_name="nickname")
    ):
        """Delete a saved nickname."""

        name = await self.bot.pool.fetchval(
            f"SELECT nickname FROM nickname_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{name}` is the nickname saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "nickname_logs", id, ctx.author.id)

    @ldelete_nickname.autocomplete("id")
    async def ldn_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        nicknames = await self.bot.pool.fetch(
            "SELECT * FROM nickname_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(
                name=f'{nick["nickname"]} - {nick["id"]}', value=nick["id"]
            )
            for nick in nicknames
            if current.lower() in str(nick["nickname"]).lower()
        ]

    @logging_delete.command(name="display_name", hidden=True)
    @interaction_only()
    async def ldelete_display(
        self, ctx: GuildContext, id: int = commands.param(displayed_name="display_name")
    ):
        """Delete a saved display name."""

        name = await self.bot.pool.fetchval(
            f"SELECT display_name FROM display_name_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{name}` is the display name saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "display_name_logs", id, ctx.author.id)

    @ldelete_display.autocomplete("id")
    async def lddn_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        display_names = await self.bot.pool.fetch(
            "SELECT * FROM display_name_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(
                name=f'{dname["display_name"]} - {dname["id"]}', value=dname["id"]
            )
            for dname in display_names
            if current.lower() in str(dname["display_name"]).lower()
        ]

    @logging_delete.command(name="discriminators", hidden=True)
    @interaction_only()
    async def ldelete_discrim(
        self,
        ctx: GuildContext,
        id: int = commands.param(displayed_name="discriminator"),
    ):
        """Delete a saved discriminator."""

        discrim = await self.bot.pool.fetchval(
            f"SELECT discrim FROM discrim_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{discrim}` is the discriminator saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "discrim_logs", id, ctx.author.id)

    @ldelete_discrim.autocomplete("id")
    async def ldd_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        discrims = await self.bot.pool.fetch(
            "SELECT * FROM discrim_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(
                name=f'{discrim["discrim"]} - {discrim["id"]}', value=discrim["id"]
            )
            for discrim in discrims
            if current.lower() in str(discrim["discrim"]).lower()
        ]
