from __future__ import annotations

import datetime
import random
import re
import textwrap
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils.vars.errors import DoNothing

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(GuildEvents(bot))


class GuildEvents(commands.Cog, name="guild_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_member_join")
    async def on_member_join(self, member: discord.Member):
        if "joins" in await self.bot.redis.smembers(f"opted_out:{member.id}"):
            return

        sql = """
        INSERT INTO member_join_logs(member_id, guild_id, time)
        VALUES ($1, $2, $3)
        """
        await self.bot.pool.execute(
            sql, member.id, member.guild.id, discord.utils.utcnow()
        )

    @commands.Cog.listener("on_member_ban")
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if self.bot.user is None:
            return

        if not guild.me:
            return

        if guild.me.guild_permissions.view_audit_log:
            mod_id = 0
            reason = "No reason given"
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.ban, limit=5
            ):
                if entry.target is None:
                    continue
                if entry.user is None:
                    continue

                if entry.target.id == user.id:
                    mod_id = entry.user.id
                    reason = entry.reason or "No reason given"
                    break
        else:
            mod_id = guild.me.id
            reason = "No reason given"

        sql = """
        INSERT INTO guild_bans(guild_id, mod_id, target_id, reason, time)
        VALUES ($1, $2, $3, $4, $5)
        """

        await self.bot.pool.execute(
            sql, guild.id, mod_id, user.id, reason, discord.utils.utcnow()
        )

    async def guild_method(self, embed: discord.Embed, guild: discord.Guild):
        embed.timestamp = (
            guild.me.joined_at
            if guild.me and guild.me.joined_at
            else discord.utils.utcnow()
        )
        embed.add_field(
            name="Created", value=discord.utils.format_dt(guild.created_at, "d")
        )
        bots = sum(1 for member in guild.members if member.bot)
        embed.add_field(name="Members", value=f"{guild.member_count:,} ({bots:,} bots)")
        embed.set_footer(
            text=f"ID: {guild.id}\nOwner ID: {guild.owner_id} \nJoined at "
        )
        await self.bot.webhooks["join_logs"].send(embed=embed)

    @commands.Cog.listener("on_guild_join")
    async def on_guild_join(self, guild: discord.Guild):
        if self.bot.user is None:
            return

        banned_guilds = await self.bot.redis.smembers("block_list")
        if str(guild.id) in banned_guilds:
            await guild.leave()
            return

        await self.bot.pool.execute(
            """
            INSERT INTO guild_join_logs(guild_id, time)
            VALUES ($1, $2)
            """,
            guild.id,
            datetime.datetime.now(),
        )

        embed = discord.Embed(
            title=guild.name,
        )

        embed.set_author(
            name="Joined Guild",
            icon_url=guild.icon.url if guild.icon else self.bot.user.display_avatar.url,
        )

        await self.guild_method(embed, guild)

        pattern = re.compile(r"(general|chat)")
        channel = discord.utils.find(
            lambda c: pattern.search(c.name)
            and c.permissions_for(guild.me).send_messages,
            guild.text_channels,
        )

        try:
            channel = (
                channel
                or [
                    c
                    for c in guild.text_channels
                    if c.permissions_for(guild.me).send_messages
                ][0]
            )
        except IndexError:
            return

        message = """
        Hello, thanks for adding me. (feel free to delete this message if it bothers you!)

        To enable Pokétwo auto-solving run the command `fish auto_solve`

        See more commands with `fish help`
        """
        await channel.send(textwrap.dedent(message))

    @commands.Cog.listener("on_guild_remove")
    async def on_guild_remove(self, guild: discord.Guild):
        if self.bot.user is None:
            return

        banned_guilds = await self.bot.redis.smembers("block_list")
        if str(guild.id) in banned_guilds:
            return

        embed = discord.Embed(
            title=guild.name,
        )

        embed.set_author(
            name="Left Guild",
            icon_url=guild.icon.url if guild.icon else self.bot.user.display_avatar.url,
        )

        await self.guild_method(embed, guild)

    @commands.Cog.listener("on_guild_update")
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.name == after.name:
            return

        sql = """INSERT INTO guild_name_logs (guild_id, name, created_at) VALUES($1,$2,$3)"""

        await self.bot.pool.execute(sql, after.id, after.name, discord.utils.utcnow())

    async def post_file(
        self,
        file: discord.File,
        webhook: discord.Webhook,
        guild: discord.Guild,
    ) -> discord.Message:
        return await webhook.send(
            f"{guild} | {guild.id} | {discord.utils.format_dt(discord.utils.utcnow())}",
            file=file,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def do_icon(
        self, guild: discord.Guild, asset: discord.Asset
    ) -> discord.Message:
        webhook = random.choice(
            [webhook for _, webhook in self.bot.icon_webhooks.items()]
        )

        size = 4096
        message = None
        for _ in range(9):
            try:
                avatar = await asset.replace(size=size).to_file()
            except (discord.NotFound, ValueError):
                break

            try:
                message = await self.post_file(
                    file=avatar, webhook=webhook, guild=guild
                )
                break
            except discord.HTTPException:
                size //= 2
                continue

        if message is None:
            channel: discord.TextChannel = self.bot.get_channel(1058221675306569748)  # type: ignore
            await channel.send(
                f"<@766953372309127168> Failed to post {guild.id}'s icon."
            )
            raise DoNothing()

        return message

    @commands.Cog.listener("on_guild_update")
    async def on_guild_icon_update(self, before: discord.Guild, after: discord.Guild):
        if before.icon == after.icon:
            return

        if after.icon is None:
            return

        message = await self.do_icon(guild=after, asset=after.icon)

        sql = """INSERT INTO guild_icons (guild_id, icon_key, created_at, icon) VALUES ($1, $2, $3, $4)"""

        now = discord.utils.utcnow()

        await self.bot.pool.execute(
            sql,
            after.id,
            after.icon.key,
            now,
            message.attachments[0].url,
        )
