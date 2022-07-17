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
        self._joins: List[Tuple[int, int, datetime.datetime]] = []

    async def _bulk_insert(self):
        if self._joins:
            sql = """
            INSERT INTO member_join_logs(member_id, guild_id, time)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, self._joins)
            self._joins.clear()

    async def cog_unload(self):
        await self._bulk_insert()
        self.bulk_insert.cancel()

    async def cog_load(self) -> None:
        self.bulk_insert.start()

    @tasks.loop(minutes=3.0)
    async def bulk_insert(self):
        await self._bulk_insert()

    @commands.Cog.listener("on_member_join")
    async def on_member_join(self, member: discord.Member):
        self._joins.append((member.id, member.guild.id, datetime.datetime.now()))

    async def guild_method(self, embed: discord.Embed, guild: discord.Guild):
        embed.timestamp = (
            guild.me.joined_at if guild.me and guild.me.joined_at else discord.utils.utcnow()
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

        if guild.id in self.bot.blacklisted_guilds:
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
