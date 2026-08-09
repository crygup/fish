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
    "username_logs": "user_id",
    "display_name_logs": "user_id",
    "stag_logs": "user_id",
    "nickname_logs": "user_id",
    "discrim_logs": "user_id",
    "member_join_logs": "member_id",
    "tags": "author_id",
    "emoji_stats": "author_id",
    "download_stats": "user_id",
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
            placeholder="Choose which tracking to opt out from",
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

        try:
            results = ctx.bot.db_cache.opted_out[ctx.author.id]
        except KeyError:
            results = []

        if value in results:
            sql = """UPDATE opted_out SET items = array_remove(opted_out.items, $1) WHERE user_id = $2"""

            await ctx.bot.pool.execute(sql, value, ctx.author.id)
            ctx.bot.db_cache.remove_opt_out(ctx.author.id, value)
            emoji = "\U0001f7e2"
        else:
            sql = """
            INSERT INTO opted_out (user_id, items) VALUES ($1, ARRAY [$2]) 
            ON CONFLICT (user_id) DO UPDATE
            SET items = array_append(opted_out.items, $2) 
            WHERE opted_out.user_id = $1
            """

            await ctx.bot.pool.execute(sql, ctx.author.id, value)
            ctx.bot.db_cache.add_opt_out(ctx.author.id, value)
            emoji = "\U0001f534"

        self.data.update({value: [self.data[value][0], emoji]})
        self.update_options()

    async def guild_opt(self):
        value = self.values[0]
        ctx: GuildContext = self.ctx  # type: ignore
        if self.guild_id is None:
            raise ValueError("Not in a guild")

        try:
            results = ctx.bot.db_cache.opted_out[self.guild_id]
        except KeyError:
            results = []

        if value in results:
            sql = """UPDATE guild_opted_out SET items = array_remove(guild_opted_out.items, $1) WHERE guild_id = $2"""

            await ctx.bot.pool.execute(sql, value, self.guild_id)
            ctx.bot.db_cache.remove_opt_out(self.guild_id, value)
            emoji = "\U0001f7e2"
        else:
            sql = """
            INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, ARRAY [$2]) 
            ON CONFLICT (guild_id) DO UPDATE
            SET items = array_append(guild_opted_out.items, $2) 
            WHERE guild_opted_out.guild_id = $1
            """

            await ctx.bot.pool.execute(sql, self.guild_id, value)
            ctx.bot.db_cache.add_opt_out(self.guild_id, value)
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


class PrivacySettingsView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        *,
        tracking_enabled: bool,
        history_public: bool,
    ) -> None:
        super().__init__(ctx, timeout=180)
        self.tracking_enabled = tracking_enabled
        self.history_public = history_public
        self._update_buttons()

    @property
    def content(self) -> str:
        tracking = "Enabled" if self.tracking_enabled else "Disabled"
        history = "Public" if self.history_public else "Private"
        return (
            f"**Tracking:** {tracking}\n"
            f"**Saved history:** {history}\n\n"
            "Tracking controls whether Fishie saves new personal activity. "
            "History visibility controls whether other users can look up data "
            "that is already saved. You can change either setting whenever "
            "you want. These settings never delete existing data."
        )

    def _update_buttons(self) -> None:
        self.toggle_tracking.label = (
            "Disable tracking" if self.tracking_enabled else "Enable tracking"
        )
        self.toggle_tracking.style = (
            discord.ButtonStyle.danger
            if self.tracking_enabled
            else discord.ButtonStyle.success
        )
        self.toggle_history.label = (
            "Make history private" if self.history_public else "Make history public"
        )
        self.toggle_history.style = (
            discord.ButtonStyle.secondary
            if self.history_public
            else discord.ButtonStyle.primary
        )

    async def _save(
        self,
        interaction: discord.Interaction,
        *,
        setting: Literal["tracking_enabled", "history_public"],
        value: bool,
    ) -> None:
        await self.ctx.bot.pool.execute(
            f"""
            INSERT INTO user_settings (user_id, {setting})
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
            SET {setting} = EXCLUDED.{setting}
            """,
            self.ctx.author.id,
            value,
        )
        cache = self.ctx.bot.db_cache
        target = (
            cache.tracking_disabled_users
            if setting == "tracking_enabled"
            else cache.private_history_users
        )
        if value:
            target.discard(self.ctx.author.id)
        else:
            target.add(self.ctx.author.id)
        self._update_buttons()
        await interaction.response.edit_message(content=self.content, view=self)

    @discord.ui.button(label="Disable tracking")
    async def toggle_tracking(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.tracking_enabled = not self.tracking_enabled
        await self._save(
            interaction,
            setting="tracking_enabled",
            value=self.tracking_enabled,
        )

    @discord.ui.button(label="Make history private")
    async def toggle_history(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.history_public = not self.history_public
        await self._save(
            interaction,
            setting="history_public",
            value=self.history_public,
        )


class Logging(Cog):
    async def privacy_settings_view(self, ctx: Context) -> PrivacySettingsView:
        row = await self.bot.pool.fetchrow(
            """
            SELECT tracking_enabled, history_public
            FROM user_settings
            WHERE user_id = $1
            """,
            ctx.author.id,
        )
        return PrivacySettingsView(
            ctx,
            tracking_enabled=row["tracking_enabled"] if row else True,
            history_public=row["history_public"] if row else True,
        )

    async def send_privacy_settings_interaction(
        self,
        ctx: Context,
        interaction: discord.Interaction,
    ) -> None:
        view = await self.privacy_settings_view(ctx)
        await interaction.response.send_message(
            view.content,
            view=view,
            ephemeral=True,
        )

    @commands.hybrid_command(name="settings")
    async def settings(self, ctx: Context) -> None:
        """Manage your Fishie privacy and tracking settings."""
        view = await self.privacy_settings_view(ctx)
        view.message = await ctx.send(
            view.content,
            view=view,
            ephemeral=ctx.interaction is not None,
        )

    @commands.hybrid_group(
        name="tracking",
        aliases=("logging",),
        fallback="user",
    )
    async def logging(self, ctx: Context):
        """Manage your personal tracking settings for the bot."""
        sql = """SELECT * FROM opted_out WHERE user_id = $1"""
        records = await self.bot.pool.fetchrow(sql, ctx.author.id)
        data = {
            "avatar": ["Avatar tracking", "\U0001f7e2"],
            "username": ["Username tracking", "\U0001f7e2"],
            "display": ["Display name tracking", "\U0001f7e2"],
            "stag": ["Server tag tracking", "\U0001f7e2"],
            "nickname": ["Nickname tracking", "\U0001f7e2"],
            "discrim": ["Discriminator tracking", "\U0001f7e2"],
            "joins": ["Server join tracking", "\U0001f7e2"],
            "xp": ["XP and message count tracking", "\U0001f7e2"],
            "commands": ["Command usage tracking", "\U0001f7e2"],
            "status": ["Presence status tracking", "\U0001f7e2"],
            "activity": ["Game and activity tracking", "\U0001f7e2"],
            "pokemon": ["Pokémon solve tracking", "\U0001f7e2"],
            "corn": ["Corn reaction tracking", "\U0001f7e2"],
            "emoji": ["Emoji statistics tracking", "\U0001f7e2"],
            "downloads": ["Download site statistics", "\U0001f7e2"],
            "snipe": ["Deleted and edited message sniping", "\U0001f7e2"],
        }

        if bool(records):
            for item in records["items"]:
                if data.get(item):
                    data.update({item: [data[item][0], "\U0001f534"]})

        await ctx.send(
            "-# Manage your tracking settings on the [website](https://crygup.com/discord?tab=settings)",
            view=DropdownView(ctx, data),
        )

    @logging.command(name="guild", aliases=("server",))
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def logging_server(self, ctx: GuildContext):
        """Manage tracking for the server."""
        sql = """SELECT * FROM guild_opted_out WHERE guild_id = $1"""
        records = await self.bot.pool.fetchrow(sql, ctx.guild.id)
        data = {
            "name": ["Name tracking", "\U0001f7e2"],
            "icon": ["Icon tracking", "\U0001f7e2"],
            "emoji": ["Emoji statistics tracking", "\U0001f7e2"],
        }

        if bool(records):
            for item in records["items"]:
                if data.get(item):
                    data.update({item: [data[item][0], "\U0001f534"]})

        await ctx.send(
            "-# Manage server tracking settings on the [website](https://crygup.com/discord?tab=settings)",
            view=DropdownView(ctx, data, guild_id=ctx.guild.id),
        )

    @commands.hybrid_group(
        name="tracking-delete",
        aliases=("logging-delete",),
        fallback="all",
        hidden=True,
    )
    @interaction_only()
    async def logging_delete(self, ctx: GuildContext, data: str):
        """Delete your saved tracking data."""

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
            "username_logs": "Usernames",
            "display_name_logs": "Display names",
            "stag_logs": "Server tags",
            "nickname_logs": "Nicknames",
            "discrim_logs": "Discriminators",
            "member_join_logs": "Server joins",
            "tags": "Tags",
            "emoji_stats": "Emoji statistics",
            "download_stats": "Download statistics",
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
            Literal["username_logs"],
            Literal["display_name_logs"],
            Literal["stag_logs"],
            Literal["nickname_logs"],
            Literal["discrim_logs"],
            Literal["member_join_logs"],
            Literal["tags"],
            Literal["emoji_stats"],
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
            "SELECT username FROM username_logs WHERE user_id = $1 AND id = $2",
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
            "SELECT nickname FROM nickname_logs WHERE user_id = $1 AND id = $2",
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
            "SELECT display_name FROM display_name_logs WHERE user_id = $1 AND id = $2",
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

    @logging_delete.command(name="servertag", aliases=("stag", "tag"), hidden=True)
    @interaction_only()
    async def ldelete_server_tag(
        self,
        ctx: GuildContext,
        id: int = commands.param(displayed_name="server_tag"),
    ):
        """Delete a saved primary server tag."""

        record = await self.bot.pool.fetchrow(
            "SELECT tag FROM stag_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )
        if record is None:
            return await ctx.send(
                "No server tag record found with that ID.", ephemeral=True
            )

        tag = record["tag"] or "No server tag"
        message = await ctx.prompt(
            f"`{tag}` is the server tag saved with the ID `{id}`. "
            "Are you sure you want to delete it?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "stag_logs", id, ctx.author.id)

    @ldelete_server_tag.autocomplete("id")
    async def ldst_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        tags = await self.bot.pool.fetch(
            "SELECT id, tag FROM stag_logs "
            "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 25",
            interaction.user.id,
        )

        return [
            app_commands.Choice(
                name=f'{tag["tag"] or "No server tag"} - {tag["id"]}'[:100],
                value=tag["id"],
            )
            for tag in tags
            if current.lower() in str(tag["tag"] or "No server tag").lower()
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
            "SELECT discrim FROM discrim_logs WHERE user_id = $1 AND id = $2",
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

    @logging_delete.command(name="joins", hidden=True)
    @interaction_only()
    async def ldelete_join(
        self,
        ctx: GuildContext,
        id: int = commands.param(displayed_name="join"),
    ):
        """Delete a saved join record."""

        record = await self.bot.pool.fetchrow(
            "SELECT guild_id, time FROM member_join_logs WHERE member_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        if not record:
            return await ctx.send("No join record found with that ID.", ephemeral=True)

        guild = self.bot.get_guild(record["guild_id"])
        guild_name = guild.name if guild else f"guild {record['guild_id']}"
        joined = discord.utils.format_dt(record["time"], "R")

        message = await ctx.prompt(
            f"You joined **{guild_name}** {joined}. Are you sure you want to delete this record? (ID `{id}`)",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "member_join_logs", id, ctx.author.id)

    @ldelete_join.autocomplete("id")
    async def ldj_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        joins = await self.bot.pool.fetch(
            "SELECT * FROM member_join_logs WHERE member_id = $1 ORDER BY time DESC LIMIT 25",
            interaction.user.id,
        )

        choices: List[app_commands.Choice[int]] = []
        for join in joins:
            guild = self.bot.get_guild(join["guild_id"])
            guild_label = guild.name if guild else str(join["guild_id"])
            label = f"{guild_label} • {discord.utils.format_dt(join['time'], 'R')}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=join["id"]))

        return choices
