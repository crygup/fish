from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from core import Cog
from utils import AuthorView, FieldPageSource, Pager, get_or_fetch_user

if TYPE_CHECKING:
    from extensions.context import GuildContext
    from extensions.context import Context


def to_lower(argument: str):
    return argument.lower()


TWITCH_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")


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

        if interaction.message is None:
            raise commands.BadArgument(
                "Message is none somehow. However the auto-download channel was set, enjoy."
            )

        await interaction.message.edit(
            content=f"Auto-download channel set to {channel.mention}", view=None
        )
        await interaction.response.defer()


class DropdownView(AuthorView):
    def __init__(self, ctx: GuildContext):
        super().__init__(ctx)

        self.add_item(Dropdown(ctx))


class TwitchMessageModal(discord.ui.Modal, title="Twitch Live Message"):
    message = discord.ui.TextInput(
        label="Announcement text",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000,
        placeholder="Leave blank to post only the embed.",
    )

    def __init__(self, bot, author_id: int, guild_id: int, channel_name: str):
        super().__init__()
        self.bot = bot
        self.author_id = author_id
        self.guild_id = guild_id
        self.channel_name = channel_name

    async def on_submit(self, interaction: discord.Interaction):
        message_template = (self.message.value or "").strip() or None
        result = await self.bot.pool.execute(
            "UPDATE twitch_follows SET message_template = $3 "
            "WHERE guild_id = $1 AND channel_name = $2",
            self.guild_id,
            self.channel_name,
            message_template,
        )
        if result == "UPDATE 0":
            await interaction.response.send_message(
                "That Twitch channel is no longer being followed.", ephemeral=True
            )
            return

        if message_template:
            await interaction.response.send_message(
                "The custom Twitch announcement text was saved. Mentions will be allowed when it posts.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "The custom Twitch announcement text was cleared; only the embed will post.",
                ephemeral=True,
            )


class TwitchFollowView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        bot: commands.Bot,
        author_id: int,
        guild_id: int,
        channel_name: str,
    ):
        super().__init__(ctx, timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.author_id = author_id
        self.guild_id = guild_id
        self.channel_name = channel_name

    @discord.ui.button(
        label="Customize announcement", style=discord.ButtonStyle.blurple
    )
    async def customize(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        row = await self.bot.pool.fetchrow(
            "SELECT message_template FROM twitch_follows "
            "WHERE guild_id = $1 AND channel_name = $2",
            self.guild_id,
            self.channel_name,
        )
        if not row:
            await interaction.response.send_message(
                "That Twitch channel is no longer being followed.", ephemeral=True
            )
            return

        modal = TwitchMessageModal(
            self.bot, self.author_id, self.guild_id, self.channel_name
        )
        modal.message.default = row["message_template"] or ""
        await interaction.response.send_modal(modal)


class Server(Cog):
    @commands.hybrid_group(name="prefix", fallback="get")
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

        p = FieldPageSource(entries, per_page=4)
        p.embed.title = f"Prefixes in {ctx.guild}"
        menu = Pager(p, ctx=ctx)
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
        await ctx.send(f"Added prefix `{prefix}` to the server.")

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
        await ctx.send(f"Removed prefix `{prefix}` from the server.")

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

    @commands.hybrid_group(
        name="auto-download",
        fallback="set",
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
                "Choose a channel to enable auto-downloads in", view=DropdownView(ctx)
            )

        await self.add_adl_channel(channel)

        await ctx.send(f"Set auto-download channel to {channel.mention}")

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

        channel: discord.TextChannel = self.bot.get_channel(channel_id)  # type: ignore

        await self.remove_adl_channel(channel)
        await ctx.send(f"Removed auto-downloads from {channel.mention}")

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
            f"{['Disabled', 'Enabled'][value]} Pokétwo auto-solving for this server."
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
            f"{['Disabled', 'Enabled'][value]} auto media reactions for this server."
        )

    @commands.hybrid_group(name="twitch", fallback="list")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch(self, ctx: GuildContext):
        """Manage Twitch live announcements for this server."""
        rows = await self.bot.pool.fetch(
            "SELECT channel_name, announce_channel_id, message_template "
            "FROM twitch_follows "
            "WHERE guild_id = $1 ORDER BY channel_name",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("No Twitch channels are being followed.")

        lines = [
            f"**{row['channel_name']}** → <#{row['announce_channel_id']}>"
            f" ({'custom text' if row['message_template'] else 'embed only'})"
            for row in rows
        ]
        await ctx.send("Twitch live announcements:\n" + "\n".join(lines))

    @twitch.command(name="follow")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_follow(
        self,
        ctx: GuildContext,
        channel_name: str,
        announcement_channel: Optional[discord.TextChannel] = None,
    ):
        """Follow a Twitch channel and announce live streams here or elsewhere."""
        channel_name = channel_name.strip().lstrip("@").lower()
        if not TWITCH_CHANNEL_RE.fullmatch(channel_name):
            raise commands.BadArgument(
                "Enter a valid Twitch channel name (letters, numbers, and underscores only)."
            )

        events = self.bot.get_cog("Events")
        if events is None or not hasattr(events, "_get_twitch_user"):
            raise commands.BadArgument("Twitch monitoring is not available right now.")
        twitch_user = await events._get_twitch_user(channel_name)
        if not twitch_user or not twitch_user.get("id"):
            raise commands.BadArgument(
                f"Could not find a Twitch channel named **{channel_name}**."
            )
        broadcaster_id = str(twitch_user["id"])

        target = announcement_channel or ctx.channel
        if not hasattr(target, "send"):
            raise commands.BadArgument(
                "Choose a text channel for Twitch announcements."
            )

        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"fishie:twitch:{ctx.guild.id}",
                )
                existing = await connection.fetchval(
                    "SELECT 1 FROM twitch_follows "
                    "WHERE guild_id = $1 AND channel_name = $2",
                    ctx.guild.id,
                    channel_name,
                )
                if not existing:
                    count = await connection.fetchval(
                        "SELECT COUNT(*) FROM twitch_follows WHERE guild_id = $1",
                        ctx.guild.id,
                    )
                    if count >= 3:
                        raise commands.BadArgument(
                            "You can follow up to 3 Twitch channels per server."
                        )

                await connection.execute(
                    """INSERT INTO twitch_follows
                       (guild_id, channel_name, announce_channel_id, broadcaster_id)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (guild_id, channel_name) DO UPDATE
                       SET announce_channel_id = EXCLUDED.announce_channel_id,
                           broadcaster_id = EXCLUDED.broadcaster_id""",
                    ctx.guild.id,
                    channel_name,
                    target.id,
                    broadcaster_id,
                )

        try:
            await events.ensure_twitch_eventsub_subscription(broadcaster_id)
        except Exception as error:
            self.bot.logger.warning(
                "Could not enable Twitch EventSub for %s: %s",
                channel_name,
                error,
            )

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_modal(
                TwitchMessageModal(self.bot, ctx.author.id, ctx.guild.id, channel_name)
            )
            return

        await ctx.send(
            f"Now following **{channel_name}**; live announcements will be posted in {target.mention}.",
            view=TwitchFollowView(
                ctx, ctx.bot, ctx.author.id, ctx.guild.id, channel_name
            ),
        )

    @twitch.command(name="message", aliases=("customize", "text"))
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_message(self, ctx: GuildContext, channel_name: str):
        """Customize the text sent above a Twitch live embed."""
        channel_name = channel_name.strip().lstrip("@").lower()
        exists = await self.bot.pool.fetchval(
            "SELECT 1 FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2",
            ctx.guild.id,
            channel_name,
        )
        if not exists:
            raise commands.BadArgument(
                f"This server is not following **{channel_name}**."
            )
        await ctx.send(
            f"Customize the announcement text for **{channel_name}**:",
            view=TwitchFollowView(
                ctx, ctx.bot, ctx.author.id, ctx.guild.id, channel_name
            ),
        )

    @twitch.command(name="unfollow", aliases=("remove", "delete"))
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def twitch_unfollow(self, ctx: GuildContext, channel_name: str):
        """Stop following a Twitch channel in this server."""
        channel_name = channel_name.strip().lstrip("@").lower()
        broadcaster_id = await self.bot.pool.fetchval(
            "SELECT broadcaster_id FROM twitch_follows "
            "WHERE guild_id = $1 AND channel_name = $2",
            ctx.guild.id,
            channel_name,
        )
        result = await self.bot.pool.execute(
            "DELETE FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2",
            ctx.guild.id,
            channel_name,
        )
        if result == "DELETE 0":
            raise commands.BadArgument(
                f"This server is not following **{channel_name}**."
            )
        if broadcaster_id:
            events = self.bot.get_cog("Events")
            if events is not None and hasattr(
                events, "remove_twitch_eventsub_subscription"
            ):
                await events.remove_twitch_eventsub_subscription(str(broadcaster_id))
        await ctx.send(f"Stopped following **{channel_name}**.")
