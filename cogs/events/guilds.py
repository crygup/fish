import datetime
from typing import List, Tuple

import discord
from bot import Bot
from discord.ext import commands, tasks


async def setup(bot: Bot):
    await bot.add_cog(GuildEvents(bot))


class GuildEvents(commands.Cog, name="guild_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_member_join")
    async def on_member_join(self, member: discord.Member):
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

    @commands.Cog.listener("on_member_remove")
    async def on_member_remove(self, member: discord.Member):
        if self.bot.user is None:
            return

        if member.guild.me.guild_permissions.view_audit_log:
            mod_id = 0
            reason = "No reason given"
            async for entry in member.guild.audit_logs(
                action=discord.AuditLogAction.kick, limit=5
            ):
                if entry.target is None:
                    continue
                if entry.user is None:
                    continue

                if entry.target.id == member.id:
                    mod_id = entry.user.id
                    reason = entry.reason or "No reason given"
                    break

            sql = """
            INSERT INTO guild_kicks(guild_id, mod_id, target_id, reason, time)
            VALUES ($1, $2, $3, $4, $5)
            """

            await self.bot.pool.execute(
                sql, member.guild.id, mod_id, member.id, reason, discord.utils.utcnow()
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

        banned_guilds = await self.bot.redis.smembers("blacklisted_guilds")
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

    @commands.Cog.listener("on_guild_remove")
    async def on_guild_remove(self, guild: discord.Guild):
        if self.bot.user is None:
            return

        embed = discord.Embed(
            title=guild.name,
        )

        embed.set_author(
            name="Left Guild",
            icon_url=guild.icon.url if guild.icon else self.bot.user.display_avatar.url,
        )

        await self.guild_method(embed, guild)
