from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord
from discord.abc import Messageable
from discord.ext import commands

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

    async def guild_too_small(self, guild: discord.Guild):
        self.bot.logger.info(
            f"I left the guild '{guild}' (ID: {guild.id}) due to its small membership size. There were only {guild.member_count} members."
        )
        url = discord.utils.oauth_url(
            self.bot.config["ids"]["bot_id"], permissions=self.bot.bot_permissions
        )

        embed = discord.Embed(
            color=discord.Color.red(),
            title="NOTICE | Message from developers",
            timestamp=discord.utils.utcnow(),
        )

        msg = f"This server currently has only {guild.member_count} members, which is insufficient for using this bot. The solution to this issue is to either add the bot to a larger server or join the support server and utilize the bot there."

        embed.add_field(name="Member count issue", value=msg)
        embed.add_field(
            name="Links",
            inline=False,
            value=f"[Support Server](https://discord.gg/Fct5UGadcb)\n"
            f"[Bot Invite URL]({url})",
        )

        channel: Optional[discord.TextChannel] = discord.utils.find(
            lambda ch: re.search("(general|main|chat|lounge)", ch.name.lower()),
            guild.text_channels,
        )

        try:
            channel = channel or guild.text_channels[0]
        except IndexError:
            await guild.leave()
            return

        await channel.send(embed=embed)

        await guild.leave()

    @commands.Cog.listener("on_guild_join")
    async def on_guild_join(self, guild: discord.Guild):
        mc = guild.member_count or sum(1 for _ in guild.members)

        if mc <= 5:
            await self.guild_too_small(guild)
            return

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
    async def on_guild_remove(self, old_guild, guild: discord.Guild):
        embed = discord.Embed(title=guild.name, timestamp=discord.utils.utcnow())
        embed.set_author(
            name="Left Guild", icon_url=guild.icon.url if guild.icon else None
        )

        await self.post_guild(embed, guild)
