from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import AuthorLayoutView, LayoutPager

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import GuildContext


def to_lower(argument: str):
    return argument.lower()


class Dropdown(discord.ui.ChannelSelect):
    def __init__(self, ctx: GuildContext):
        self.ctx = ctx
        super().__init__(
            placeholder="Choose a channel.",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        channel: discord.TextChannel = self.values[0]  # type: ignore

        if self.ctx.bot.settings is None:
            raise commands.BadArgument("Settings cog could not be found somehow.")

        await self.ctx.bot.settings.add_adl_channel(channel)

        if isinstance(self.view, DropdownView):
            self.view.status.content = f"## Auto-download settings\nAuto-download channel set to {channel.mention}."
            self.disabled = True
            await interaction.response.edit_message(
                view=self.view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.send_message(
            f"Auto-download channel set to {channel.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class DropdownView(AuthorLayoutView):
    def __init__(self, ctx: GuildContext):
        super().__init__(ctx, timeout=180)
        self.status = discord.ui.TextDisplay(
            "## Auto-download settings\nChoose the channel for automatic downloads."
        )
        self.dropdown = Dropdown(ctx)
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(self.dropdown),
                accent_color=ctx.bot.embedcolor,
            )
        )


class SettingsMessageView(AuthorLayoutView):
    """Render a one-off server-settings response as Components V2."""

    def __init__(self, ctx: GuildContext, text: str) -> None:
        super().__init__(ctx, timeout=120)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=ctx.bot.embedcolor,
            )
        )


class PrefixPageSource:
    def __init__(self, entries: list[tuple[str, str]], guild_name: str) -> None:
        self.entries = entries
        self.guild_name = guild_name

    def get_max_pages(self) -> int:
        return max(1, (len(self.entries) + 3) // 4)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        start = page_number * 4
        page_entries = self.entries[start : start + 4]
        lines = [f"## Prefixes in {self.guild_name}"]
        lines.extend(f"**{prefix}**\n{details}" for prefix, details in page_entries)
        lines.append(f"\n-# Page {page_number + 1}/{self.get_max_pages()}")
        return [discord.ui.TextDisplay("\n\n".join(lines))]


class ServerSettingsView(AuthorLayoutView):
    """Interactive server settings panel used by ``settings server``."""

    def __init__(self, ctx: GuildContext, values: dict[str, object]) -> None:
        super().__init__(ctx, timeout=300)
        if ctx.guild is None:
            raise ValueError("Server settings can only be used in a guild.")
        self.guild = ctx.guild
        self.values = values
        self._render()

    @classmethod
    async def create(cls, ctx: GuildContext) -> "ServerSettingsView":
        row = await ctx.bot.pool.fetchrow(
            """
            SELECT tracking_enabled, history_public, auto_download, poketwo,
                   auto_reactions, pinboard
            FROM guild_settings WHERE guild_id = $1
            """,
            ctx.guild.id,
        )
        honeypot = await ctx.bot.pool.fetchval(
            "SELECT channel_id FROM honeypot_channels WHERE guild_id = $1",
            ctx.guild.id,
        )
        values = {
            "tracking_enabled": bool(row["tracking_enabled"]) if row else True,
            "history_public": bool(row["history_public"]) if row else False,
            "auto_download": row["auto_download"] if row else None,
            "poketwo": bool(row["poketwo"]) if row else False,
            "auto_reactions": bool(row["auto_reactions"]) if row else False,
            "pinboard": row["pinboard"] if row else None,
            "honeypot": honeypot,
        }
        return cls(ctx, values)

    @staticmethod
    def _channel_mention(channel_id: object) -> str:
        return f"<#{channel_id}>" if channel_id else "Disabled"

    @property
    def content(self) -> str:
        tracking = "Enabled" if self.values["tracking_enabled"] else "Disabled"
        visibility = "Public" if self.values["history_public"] else "Private"
        poketwo = "Enabled" if self.values["poketwo"] else "Disabled"
        reactions = "Enabled" if self.values["auto_reactions"] else "Disabled"
        return (
            f"## Server settings · {self.guild.name}\n"
            f"**Tracking:** {tracking}\n"
            f"**Saved history:** {visibility}\n"
            f"**Pokétwo auto-solving:** {poketwo}\n"
            f"**Auto reactions:** {reactions}\n"
            f"**Auto-download:** {self._channel_mention(self.values['auto_download'])}\n"
            f"**Pinboard:** {self._channel_mention(self.values['pinboard'])}\n"
            f"**Honeypot:** {self._channel_mention(self.values['honeypot'])}\n\n"
            "Use the channel selectors to choose destinations. Server history is "
            "private until an administrator makes it public."
        )

    def _render(self) -> None:
        self.clear_items()
        buttons: list[discord.ui.Button] = []
        controls = (
            (
                (
                    "Disable tracking"
                    if self.values["tracking_enabled"]
                    else "Enable tracking"
                ),
                "tracking_enabled",
            ),
            (
                (
                    "Make history private"
                    if self.values["history_public"]
                    else "Make history public"
                ),
                "history_public",
            ),
            (
                "Disable Pokétwo" if self.values["poketwo"] else "Enable Pokétwo",
                "poketwo",
            ),
            (
                (
                    "Disable auto reactions"
                    if self.values["auto_reactions"]
                    else "Enable auto reactions"
                ),
                "auto_reactions",
            ),
        )
        for label, setting in controls:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def callback(
                interaction: discord.Interaction,
                selected: str = setting,
            ) -> None:
                value = not bool(self.values[selected])
                await self.ctx.bot.pool.execute(
                    f"""
                    INSERT INTO guild_settings (guild_id, {selected})
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id) DO UPDATE
                    SET {selected} = EXCLUDED.{selected}
                    """,
                    self.guild.id,
                    value,
                )
                self.values[selected] = value
                cache = self.ctx.bot.db_cache
                if selected == "tracking_enabled":
                    if value:
                        cache.guild_tracking_disabled.discard(self.guild.id)
                    else:
                        cache.guild_tracking_disabled.add(self.guild.id)
                elif selected == "history_public":
                    cache.set_guild_history_public(self.guild.id, value)
                elif selected == "poketwo":
                    (cache.add_poketwo if value else cache.remove_poketwo)(
                        self.guild.id
                    )
                elif selected == "auto_reactions":
                    (
                        cache.add_reaction_guilds
                        if value
                        else cache.remove_reaction_guilds
                    )(self.guild.id)
                self._render()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            button.callback = callback
            buttons.append(button)

        controls = [discord.ui.ActionRow(*buttons)]
        for kind, label in (
            ("auto_download", "Set auto-download channel"),
            ("pinboard", "Set pinboard channel"),
            ("honeypot", "Set honeypot channel"),
        ):
            select = discord.ui.ChannelSelect(
                placeholder=label,
                min_values=1,
                max_values=1,
                channel_types=[discord.ChannelType.text],
            )

            async def select_callback(
                interaction: discord.Interaction,
                selected_kind: str = kind,
                channel_select: discord.ui.ChannelSelect = select,
            ) -> None:
                channel = channel_select.values[0]
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "Please choose a text channel.", ephemeral=True
                    )
                    return
                await self._set_channel(selected_kind, channel.id)
                self._render()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            select.callback = select_callback
            controls.append(discord.ui.ActionRow(select))

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(self.content),
                *controls,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _set_channel(self, kind: str, channel_id: int) -> None:
        guild_id = self.guild.id
        if kind in {"auto_download", "pinboard"}:
            old = self.values.get(kind)
            await self.ctx.bot.pool.execute(
                f"""
                INSERT INTO guild_settings (guild_id, {kind}) VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET {kind} = EXCLUDED.{kind}
                """,
                guild_id,
                channel_id,
            )
            if kind == "auto_download":
                if isinstance(old, int):
                    self.ctx.bot.db_cache.remove_adl(old)
                self.ctx.bot.db_cache.add_adl(channel_id)
            else:
                self.ctx.bot.db_cache.add_pinboard(guild_id, channel_id)
        else:
            await self.ctx.bot.pool.execute(
                """
                INSERT INTO honeypot_channels (guild_id, channel_id) VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id
                """,
                guild_id,
                channel_id,
            )
            self.ctx.bot.cached_honeypots[guild_id] = channel_id
        self.values[kind] = channel_id


class Server(Cog):
    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, ctx: GuildContext):
        """Manage the server prefixes"""
        format_dt = discord.utils.format_dt

        sql = """SELECT * FROM guild_prefixes WHERE guild_id = $1 AND time IS NOT NULL ORDER BY time DESC"""
        records = await ctx.bot.pool.fetch(sql, ctx.guild.id)

        if not bool(records):
            raise commands.BadArgument("This server has no prefixes set.")

        entries = [
            (
                record["prefix"],
                f'{format_dt(record["time"], "R")}  |  {format_dt(record["time"], "d")} | <@{record["author_id"]}>',
            )
            for record in records
        ]

        menu = LayoutPager(PrefixPageSource(entries, str(ctx.guild)), ctx=ctx)
        await menu.start(ctx)

    @prefix.command(name="add", aliases=("set", "a", "+"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_add(
        self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)
    ):
        """Add a prefix to the server"""
        bot = self.bot
        now = discord.utils.utcnow()
        if len(prefix) > 10:
            raise commands.BadArgument("Prefixes can only be 10 characters long.")

        try:
            if prefix in bot.db_cache.prefixes[ctx.guild.id]:
                raise commands.BadArgument("This prefix is already set.")
        except KeyError:
            pass

        sql = """INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, $4)"""
        await bot.pool.execute(sql, ctx.guild.id, prefix, ctx.author.id, now)
        bot.db_cache.add_prefix(ctx.guild.id, prefix)
        await ctx.send(
            view=SettingsMessageView(
                ctx, f"## Prefix settings\nAdded prefix `{prefix}` to the server."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @prefix.command(name="remove", aliases=("delete", "r", "d", "del", "-"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_remove(
        self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)
    ):
        """Remove a prefix from the server"""
        bot = self.bot

        if prefix not in bot.db_cache.prefixes[ctx.guild.id]:
            raise commands.BadArgument(
                "This prefix does not exist. Check your spelling and try again."
            )

        sql = """DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2"""
        await bot.pool.execute(sql, ctx.guild.id, prefix)
        bot.db_cache.remove_prefix(ctx.guild.id, prefix)
        await ctx.send(
            view=SettingsMessageView(
                ctx, f"## Prefix settings\nRemoved prefix `{prefix}` from the server."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def add_adl_channel(self, channel: discord.TextChannel):
        sql = """
        INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET auto_download = $2
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, channel.guild.id, channel.id)
        self.bot.db_cache.add_adl(channel.id)

    async def remove_adl_channel(self, channel: discord.TextChannel):
        sql = """UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1"""

        await self.bot.pool.execute(sql, channel.guild.id)
        self.bot.db_cache.remove_adl(channel.id)

    @commands.group(
        name="auto-download",
        invoke_without_command=True,
        aliases=("adl", "autodownload", "auto_download"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def auto_download(
        self, ctx: GuildContext, *, channel: Optional[discord.TextChannel] = None
    ):
        """Add a channel to auto-download videos from"""
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        channel_id: int = await self.bot.pool.fetchval(sql, ctx.guild.id)
        if channel_id:
            raise commands.BadArgument(
                f"An auto-download channel is already set in this server, if you would like to change or remove it please run the command `{ctx.get_prefix}auto-download remove`"
            )

        if not channel:
            return await ctx.send(
                view=DropdownView(ctx),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await self.add_adl_channel(channel)

        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Auto-download settings\nSet auto-download channel to {channel.mention}.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @auto_download.command(
        name="remove",
        aliases=("r", "delete", "d", "-"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def auto_download_remove(self, ctx: GuildContext):
        """Add a channel to auto-download videos from"""
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        channel_id: int = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not channel_id:
            raise commands.BadArgument(
                f"No auto-download channel found. You may set one with `{ctx.get_prefix}auto-download`"
            )

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.bot.pool.execute(
                    "UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1",
                    ctx.guild.id,
                )
                self.bot.db_cache.remove_adl(channel_id)
                raise commands.BadArgument(
                    "The configured auto-download channel no longer exists. "
                    "The setting was removed."
                )

        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != ctx.guild.id
        ):
            await self.bot.pool.execute(
                "UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1",
                ctx.guild.id,
            )
            self.bot.db_cache.remove_adl(channel_id)
            raise commands.BadArgument(
                "The configured auto-download channel is no longer available. "
                "The setting was removed."
            )

        await self.remove_adl_channel(channel)
        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Auto-download settings\nRemoved auto-downloads from {channel.mention}.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(
        name="auto-solve",
        aliases=(
            "auto_solve",
            "autosolve",
        ),
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_solve(self, ctx: GuildContext):
        """Toggle auto solving for Pokétwo hint messages"""
        value: Optional[bool] = await self.bot.pool.fetchval(
            "SELECT poketwo FROM guild_settings WHERE guild_id = $1", ctx.guild.id
        )

        value = not value if value else True

        sql = """
        INSERT INTO guild_settings (guild_id, poketwo) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET poketwo = $2 
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, ctx.guild.id, value)

        func = [self.bot.db_cache.remove_poketwo, self.bot.db_cache.add_poketwo]

        func[value](ctx.guild.id)

        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Server settings\n{['Disabled', 'Enabled'][value]} Pokétwo auto-solving for this server.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(
        name="auto-reactions",
        aliases=("auto_reactions", "autoreactions", "areactions"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_reactions(self, ctx: GuildContext):
        """Toggle auto reactions to media uploads"""
        value: Optional[bool] = await self.bot.pool.fetchval(
            "SELECT auto_reactions FROM guild_settings WHERE guild_id = $1",
            ctx.guild.id,
        )

        value = not value if value else True

        sql = """
        INSERT INTO guild_settings (guild_id, auto_reactions) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET auto_reactions = $2 
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, ctx.guild.id, value)

        func = [
            self.bot.db_cache.remove_reaction_guilds,
            self.bot.db_cache.add_reaction_guilds,
        ]

        func[value](ctx.guild.id)

        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Server settings\n{['Disabled', 'Enabled'][value]} auto media reactions for this server.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
