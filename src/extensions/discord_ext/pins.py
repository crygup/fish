from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog
from utils import AuthorView

if TYPE_CHECKING:
    from extensions.context import GuildContext


async def add_pinboard_channel(ctx: GuildContext, channel: discord.TextChannel):
    sql = """
        INSERT INTO guild_settings (guild_id, pinboard) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET pinboard = $2
        WHERE guild_settings.guild_id = $1
        """

    await ctx.pool.execute(sql, channel.guild.id, channel.id)
    ctx.bot.db_cache.add_pinboard(ctx.guild.id, channel.id)


async def remove_pinboard_channel(ctx: GuildContext, channel: discord.TextChannel):
    sql = """UPDATE guild_settings SET pinboard = NULL WHERE guild_id = $1"""

    await ctx.pool.execute(sql, channel.guild.id)
    ctx.bot.db_cache.remove_pinboard(ctx.guild.id, channel.id)


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

        perms = channel.permissions_for(self.ctx.guild.me)
        if not perms.send_messages or not perms.attach_files or not perms.embed_links:
            return await interaction.response.edit_message(
                view=None,
                content="I do not have permissions to either send, attach files, or embed links in that channel. Please fix this and run the command again.",
                embeds=[],
            )

        await add_pinboard_channel(self.ctx, channel)

        await interaction.response.edit_message(
            content=f"Channel created and Pinboard set to: {channel.mention}",
            view=None,
            embeds=[],
        )


class PinBoardView(AuthorView):
    def __init__(self, ctx: GuildContext):
        super().__init__(ctx)
        self.ctx = ctx

        self.add_item(Dropdown(ctx))

    @discord.ui.button(label="Create channel", style=discord.ButtonStyle.blurple)
    async def btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            channel = await self.ctx.guild.create_text_channel(name="pinboard")
        except discord.HTTPException:
            await interaction.response.edit_message(
                content="I do not have permission to create channels, please select one or give me the required permissions.",
                view=None,
                embeds=[],
            )
            return

        await add_pinboard_channel(self.ctx, channel)

        await interaction.response.edit_message(
            content=f"Channel created and Pinboard set to: {channel.mention}",
            view=None,
            embeds=[],
        )


class DeletePinBoardView(AuthorView):
    def __init__(self, ctx: GuildContext):
        super().__init__(ctx)
        self.ctx = ctx

    @discord.ui.button(label="Unlink Pinboard?", style=discord.ButtonStyle.red)
    async def btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        stored_channel_id = await self.ctx.pool.fetchval(
            "SELECT pinboard FROM guild_settings WHERE guild_id = $1",
            self.ctx.guild.id,
        )
        item = int(stored_channel_id or 0)
        channel: discord.TextChannel | None = self.ctx.bot.get_channel(item)  # type: ignore

        if channel is None:
            await self.ctx.pool.execute(
                "UPDATE guild_settings SET pinboard = NULL WHERE guild_id = $1",
                self.ctx.guild.id,
            )
            self.ctx.bot.db_cache.remove_pinboard(self.ctx.guild.id, item)
            await interaction.response.edit_message(
                content="The missing Pinboard channel was removed from the settings.",
                view=None,
                embeds=[],
            )
            return

        await remove_pinboard_channel(self.ctx, channel)

        await interaction.response.edit_message(
            content=f"Unlinked Pinboard from: {channel.mention}. New pins will no longer be added.",
            view=None,
            embeds=[],
        )


class Pinboard(Cog):

    @commands.Cog.listener("on_message_edit")
    async def on_pin(self, old_message: discord.Message, message: discord.Message):
        guild = old_message.guild

        if guild is None:
            return

        if not old_message.pinned and message.pinned:
            try:
                pinboard = self.bot.db_cache.pinboard[guild.id]
            except KeyError:
                return

            channel: discord.TextChannel | None = guild.get_channel(pinboard)  # type: ignore
            if channel is None:
                self.bot.db_cache.remove_pinboard(guild.id, pinboard)
                return

            fm = await guild.fetch_member(message.author.id)

            PBEmbed = discord.Embed(
                color=fm.accent_color,
                description=discord.utils.escape_markdown(message.content),
                timestamp=discord.utils.utcnow(),
            )
            PBEmbed.set_author(
                name=f"{fm.display_name}", icon_url=fm.display_avatar.url
            )
            PBEmbed.add_field(
                name="Original message", value=f"[Click to view]({message.jump_url})"
            )

            embeds = []
            files = []
            embeds.append(PBEmbed)

            for embed in message.embeds:
                if str(embed.type) == "rich":
                    embeds.append(embed)

            if message.attachments:
                for attachment in message.attachments[:9]:
                    image = await attachment.to_file(filename=attachment.filename)
                    files.append(image)
                    new_embed = discord.Embed(color=fm.accent_color)
                    new_embed.set_image(url=f"attachment://{image.filename}")
                    embeds.append(new_embed)

            user = "<Blank>"
            pinner_id: int | None = None
            try:
                async for entry in guild.audit_logs(
                    limit=5, action=discord.AuditLogAction.message_pin
                ):
                    if entry.target and entry.target.id == fm.id and entry.user:
                        user = entry.user.display_name
                        pinner_id = entry.user.id
                        break
            except discord.Forbidden:
                pass

            msg = f"\U0001f4cc {user} pinned a message to the server's Pinboard"

            if user == "<Blank>":
                msg += '\n\n Seeing "<Blank>" and rather show who pinned? Give me "View Audit Log" permissions.'

            sql = """
            INSERT INTO pinboard_pins ( message_id, author_id,
                                        target_id, guild_id, 
                                        channel_id)
            VALUES ($1, $2, $3, $4 , $5)"""

            await self.bot.pool.execute(
                sql, message.id, pinner_id, fm.id, guild.id, pinboard
            )

            await channel.send(msg, embeds=embeds[:10], files=files[:10])
            pins = await channel.pins()

            if len(pins) > 40:
                try:
                    await pins[-1].unpin()
                except discord.HTTPException:
                    pass

    @commands.group(name="pinboard", aliases=("pb",), invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def pinboard(self, ctx: GuildContext):
        """Setup the Pinboard for your server."""
        item: int | None = (
            await ctx.pool.fetchval(
                "SELECT pinboard FROM guild_settings WHERE guild_id = $1", ctx.guild.id
            )
            or 0
        )

        embed = discord.Embed(color=0xFFFFFF, title="Pinboard setup")
        embed.add_field(
            name="What is Pinboard?",
            value="Pinboard is a feature meant to bring together Starboard and Discord's pin message feature. Just pin a message once Pinboard is setup and it will automatically get added to the Pinboard. You can add as many messages as you want.",
        )
        embed.add_field(
            name="How to setup Pinboard?",
            value="Just select one of the buttons down below to either create a channel or select one from your preexisting channels!",
        )
        await ctx.send(
            view=DeletePinBoardView(ctx) if item else PinBoardView(ctx), embed=embed
        )
