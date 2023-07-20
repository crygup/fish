from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from discord.abc import Messageable

from core import Cog

if TYPE_CHECKING:
    from context import Context


class Guilds(Cog):
    async def post_guild(self, embed: discord.Embed, guild: discord.Guild):
        embed.add_field(
            name="Created", value=discord.utils.format_dt(guild.created_at, "d")
        )
        embed.add_field(
            name="Members",
            value=f"{guild.member_count:,} ({sum(m.bot for m in guild.members)} bots)",
        )

        embed.set_footer(
            text=f"ID: {guild.id} \nOwner ID: {guild.owner_id} \nCreated at"
        )

        embed.color = [discord.Colour.green(), discord.Colour.red()][
            sum(m.bot for m in guild.members) > sum(not m.bot for m in guild.members)
        ]

        channel = self.bot.get_channel(self.bot.config["ids"]["join_logs_id"])

        if not isinstance(channel, Messageable):
            raise commands.BadArgument("Join logs channel is not messageable.")

        await channel.send(embed=embed)

    @commands.Cog.listener("on_guild_join")
    async def on_guild_join(self, guild: discord.Guild):
        embed = discord.Embed(title=guild.name, timestamp=discord.utils.utcnow())
        embed.set_author(
            name="Joined Guild", icon_url=guild.icon.url if guild.icon else None
        )

        await self.post_guild(embed, guild)

        sql = """
        INSERT INTO guild_join_logs(guild_id, owner_id, time) 
        VALUES($1, $2, $3)
        """

        await self.bot.pool.execute(
            sql, guild.id, guild.owner_id, discord.utils.utcnow()
        )

    @commands.Cog.listener("on_guild_remove")
    async def on_guild_remove(self, guild: discord.Guild):
        embed = discord.Embed(title=guild.name, timestamp=discord.utils.utcnow())
        embed.set_author(
            name="Left Guild", icon_url=guild.icon.url if guild.icon else None
        )

        await self.post_guild(embed, guild)
