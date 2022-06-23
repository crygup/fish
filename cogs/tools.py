import imghdr
from io import BytesIO
import textwrap
from typing import List, Optional

import discord
from bot import Bot
from discord.ext import commands


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))


class Tools(commands.Cog, name="tools"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name="first_message", aliases=("fm", "oldest"))
    async def first_message(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        *,
        member: discord.Member = commands.Author,
    ):
        """Sends a url to the first message from a member in a channel.

        If the url seems to lead nowhere the message might've been deleted."""

        if ctx.guild is None:
            return

        channel = channel or ctx.channel # type: ignore

        if channel is None:
            return

        await self.bot.get_cog("message_event")._bulk_insert()  # type: ignore

        record = await self.bot.pool.fetchrow(
            "SELECT * FROM message_logs WHERE author_id = $1 AND guild_id = $2 AND channel_id = $3 ORDER BY created_at ASC LIMIT 1",
            member.id,
            ctx.guild.id,
            channel.id,
        )
        if record is None:
            await ctx.send(
                f"It seems I have no records for {str(member)} in this channel"
            )
            return

        url = f'https://discordapp.com/channels/{record["guild_id"]}/{record["channel_id"]}/{record["message_id"]}'
        await ctx.send(url)

    @commands.command(name="snipe")
    async def snipe(
        self,
        ctx: commands.Context,
        index: Optional[int] = 1,
        channel: Optional[discord.TextChannel] = commands.CurrentChannel,
        *,
        member: Optional[discord.Member] = None,
    ):
        """Shows a deleted message"""
        index = index or 1

        if ctx.guild is None or channel is None:
            return

        await self.bot.get_cog("message_event")._bulk_insert()  # type: ignore

        if member:
            sql = """
            SELECT * FROM snipe_logs where channel_id = $1 AND author_id = $2 ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id, member.id)
        else:
            sql = """
            SELECT * FROM snipe_logs where channel_id = $1 ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id)

        if index - 1 >= len(results):
            await ctx.send("Index out of range.")
            return

        if results == []:
            await ctx.send("Nothing was deleted here...")
            return

        user = self.bot.get_user(results[index - 1]["author_id"]) or "Unknown"

        embeds: List[discord.Embed] = []
        files: List[discord.File] = []

        embed = discord.Embed(
            color=self.bot.embedcolor, timestamp=results[index - 1]["created_at"]
        )
        embed.description = (
            textwrap.shorten(
                results[index - 1]["message_content"], width=300, placeholder="..."
            )
            or "Message did not contain any content."
        )
        embed.set_author(
            name=f"{str(user)}",
            icon_url=user.display_avatar.url
            if isinstance(user, discord.User)
            else ctx.guild.me.display_avatar.url,
        )
        message_id = results[index - 1]["message_id"]
        embed.set_footer(text=f"Index {index} of {len(results)}\nMessage deleted ")
        embeds.append(embed)

        attachment_sql = """SELECT * FROM snipe_attachment_logs where message_id = $1"""
        attachment_results = await self.bot.pool.fetch(attachment_sql, message_id)
        for _index, result in enumerate(attachment_results):
            file = discord.File(
                BytesIO(result["attachment"]),
                filename=f'{message_id}_{_index}.{imghdr.what(None, result["attachment"])}',
            )
            files.append(file)
            embed = discord.Embed(
                color=self.bot.embedcolor, timestamp=results[index - 1]["created_at"]
            )
            embed.set_image(
                url=f'attachment://{message_id}_{_index}.{imghdr.what(None, result["attachment"])}'
            )
            embeds.append(embed)

        await ctx.send(embeds=embeds, files=files)

    @commands.command(name='invite', aliases=('join',))
    async def invite(self, ctx: commands.Context):
        """Sends an invite link to the bot"""
        bot = self.bot
        if bot.user is None:
            return

        permissions = discord.Permissions.none()
        permissions.read_messages = True
        permissions.send_messages = True
        permissions.read_message_history = True
        permissions.embed_links = True
        permissions.attach_files = True

        await ctx.send(
            f'{discord.utils.oauth_url(bot.user.id, permissions=permissions, scopes=("bot",))}'
        )
