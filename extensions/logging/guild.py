from __future__ import annotations

import asyncio
import base64
import random
from io import BytesIO
from typing import TYPE_CHECKING

import asyncpg
import discord
from discord.ext import commands

from core import Cog
from utils import resize_to_limit

if TYPE_CHECKING:
    from core import Fishie


class Guild(Cog):
    async def add_icon(
        self,
        guild: discord.Guild,
        asset: discord.Asset,
    ):
        webhook = discord.Webhook.from_url(
            random.choice(self.bot.config["webhooks"]["images"]),
            session=self.bot.session,
        )
        try:
            image = await asyncio.to_thread(
                resize_to_limit, BytesIO(await asset.read()), 8388608
            )
            message = await webhook.send(
                f"{guild.name} | {guild.id} | {guild.member_count:,} | {discord.utils.format_dt(discord.utils.utcnow())}",
                file=discord.File(image, filename=f"{guild.id}_{asset.key}.png"),
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            return

        sql = """
        INSERT INTO guild_icons(guild_id, icon_key, created_at, icon)
        VALUES($1, $2, $3, $4)"""

        try:
            await self.bot.pool.execute(
                sql,
                guild.id,
                asset.key,
                discord.utils.utcnow(),
                message.attachments[0].url,
            )
        except asyncpg.UniqueViolationError:
            pass

    @commands.Cog.listener("on_guild_update")
    async def icon_update(self, before_g: discord.Guild, after_g: discord.Guild):
        if after_g.icon is None:
            return

        if before_g.icon and before_g.icon == after_g.icon:
            return

        if "icon" in await self.bot.redis.smembers(f"guild_opted_out:{after_g.id}"):
            return

        await self.add_icon(after_g, after_g.icon)

    async def add_name(self, guild: discord.Guild):
        sql = """
        INSERT INTO guild_name_logs(guild_id, name, created_at)
        VALUES($1, $2, $3)"""

        await self.bot.pool.execute(sql, guild.id, guild.name, discord.utils.utcnow())

    @commands.Cog.listener("on_guild_update")
    async def name_update(self, before_g: discord.Guild, after_g: discord.Guild):
        if before_g.name == after_g.name:
            return

        if "name" in await self.bot.redis.smembers(f"guild_opted_out:{after_g.id}"):
            return

        await self.add_name(after_g)
