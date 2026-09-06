from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog, is_operational_guild
from core.handoff import is_legacy_instance
from core.views import AuthorView

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


DEFAULT_HONEYPOT_MESSAGE = (
    "This channel was made to catch people who spam in every channel, if you type "
    "here there will be no coming back."
)


class HoneypotSetupView(AuthorView):
    def __init__(self, ctx: Context):
        super().__init__(ctx=ctx, timeout=60)
        self.value: bool | None = None
        self.channel: discord.TextChannel | None = None

    @discord.ui.button(label="Use this channel", style=discord.ButtonStyle.blurple)
    async def use_this(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        self.value = True
        self.channel = self.ctx.channel  # type: ignore[assignment]
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Create new channel", style=discord.ButtonStyle.green)
    async def create_new(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        self.value = False
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


class Honeypot(Cog):
    """Honeypot channel instantly bans anyone who sends a message in it."""

    @commands.hybrid_group(
        name="honeypot",
        aliases=("honey-pot",),
        fallback="setup",
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True, ban_members=True)
    @commands.bot_has_guild_permissions(manage_channels=True, ban_members=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def honeypot(self, ctx: GuildContext):
        """Setup or manage a honeypot channel to catch spam bots."""
        if ctx.invoked_subcommand is not None:
            return

        view = HoneypotSetupView(ctx)
        view.message = await ctx.send(
            "Where should the honeypot be set up?",
            view=view,
        )
        await view.wait()

        if view.value is None:
            await ctx.send("Honeypot setup cancelled.")
            return

        if view.value is True:
            channel = ctx.channel
        else:
            try:
                channel = await ctx.guild.create_text_channel(
                    name="honeypot",
                    reason=f"Honeypot created by {ctx.author}",
                )
            except discord.Forbidden:
                await ctx.send("I don't have permission to create channels.")
                return

        if not isinstance(channel, discord.TextChannel):
            await ctx.send("The honeypot must use a text channel.")
            return

        await self.bot.pool.execute(
            "INSERT INTO honeypot_channels (guild_id, channel_id) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2",
            ctx.guild.id,
            channel.id,
        )
        self.bot.cached_honeypots[ctx.guild.id] = channel.id

        message_template = await self.bot.pool.fetchval(
            "SELECT message_template FROM honeypot_channels WHERE guild_id = $1",
            ctx.guild.id,
        )
        embed = discord.Embed(color=self.bot.embedcolor)
        embed.add_field(
            name="Watch your step!",
            value=message_template or DEFAULT_HONEYPOT_MESSAGE,
            inline=False,
        )
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(
            f"Honeypot set up in {channel.mention}. We recommend changing the channel name since bots have started to check the names of channels to ignore them.",
            delete_after=10,
        )

    @honeypot.command(name="remove", with_app_command=False)
    @commands.has_guild_permissions(manage_channels=True)
    async def honeypot_remove(self, ctx: GuildContext):
        """Remove the honeypot from this server."""
        row = await self.bot.pool.fetchrow(
            "DELETE FROM honeypot_channels WHERE guild_id = $1 RETURNING channel_id",
            ctx.guild.id,
        )
        if row:
            self.bot.cached_honeypots.pop(ctx.guild.id, None)
            await ctx.send("Honeypot removed.")
        else:
            await ctx.send("No honeypot is set up in this server.")

    @commands.Cog.listener("on_message")
    async def honeypot_on_message(self, message: discord.Message):
        if is_legacy_instance(self.bot):
            return
        if not message.guild or is_operational_guild(message) or message.author.bot:
            return
        if self.bot.cached_honeypots.get(message.guild.id) != message.channel.id:
            return
        if message.author.guild_permissions.ban_members or message.author.guild_permissions.manage_guild:  # type: ignore[union-attr]
            return

        try:
            await message.guild.ban(
                message.author,
                reason="Honeypot ban.",
                delete_message_seconds=604800,
            )
        except discord.HTTPException:
            pass
