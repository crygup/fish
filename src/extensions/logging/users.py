from __future__ import annotations

from datetime import datetime

import discord
from discord.ext import commands

from core import Cog


class User(Cog):
    @staticmethod
    def _server_tag_values(
        user: discord.User,
    ) -> tuple[str | None, int | None, datetime | None, str | None]:
        primary_guild = getattr(user, "primary_guild", None)
        if primary_guild is None:
            return None, None, None, None
        badge = getattr(primary_guild, "badge", None)
        return (
            primary_guild.tag,
            primary_guild.id,
            primary_guild.created_at,
            badge.url if badge is not None else None,
        )

    async def add_server_tag(self, user: discord.User) -> None:
        tag, guild_id, guild_created_at, badge_url = self._server_tag_values(user)
        await self.bot.pool.execute(
            """
            INSERT INTO stag_logs(
                user_id,
                tag,
                guild_id,
                guild_created_at,
                badge_url,
                created_at
            )
            VALUES($1, $2, $3, $4, $5, $6)
            """,
            user.id,
            tag,
            guild_id,
            guild_created_at,
            badge_url,
            discord.utils.utcnow(),
        )

    @commands.Cog.listener("on_user_update")
    async def server_tag_update(
        self, before_u: discord.User, after_u: discord.User
    ) -> None:
        if self._server_tag_values(before_u) == self._server_tag_values(after_u):
            return

        if self.bot.db_cache.user_tracking_opted_out(after_u.id, "stag"):
            return

        await self.add_server_tag(after_u)

    async def add_username(self, user: discord.User):
        sql = """
        INSERT INTO username_logs(user_id, username, created_at)
        VALUES ($1, $2, $3)
        """

        await self.bot.pool.execute(sql, user.id, user.name, discord.utils.utcnow())

    @commands.Cog.listener("on_user_update")
    async def username_update(self, before_u: discord.User, after_u: discord.User):
        if before_u.name == after_u.name:
            return

        if self.bot.db_cache.user_tracking_opted_out(after_u.id, "username"):
            return

        await self.add_username(after_u)

    async def add_display_name(self, user: discord.User):
        sql = """
        INSERT INTO display_name_logs(user_id, display_name, created_at)
        VALUES($1, $2, $3)
        """

        await self.bot.pool.execute(
            sql, user.id, user.display_name, discord.utils.utcnow()
        )

    @commands.Cog.listener("on_user_update")
    async def display_name_update(self, before_u: discord.User, after_u: discord.User):
        if before_u.display_name == after_u.display_name:
            return

        if self.bot.db_cache.user_tracking_opted_out(after_u.id, "display"):
            return

        await self.add_display_name(after_u)

    async def add_nickname(self, member: discord.Member):
        sql = """
        INSERT INTO nickname_logs(user_id, guild_id, nickname, created_at)
        VALUES($1, $2, $3, $4)
        """

        await self.bot.pool.execute(
            sql, member.id, member.guild.id, member.nick, discord.utils.utcnow()
        )

    @commands.Cog.listener("on_member_update")
    async def nickname_update(self, before_m: discord.Member, after_m: discord.Member):
        if before_m.nick == after_m.nick:
            return

        if not after_m.nick:
            return

        if self.bot.db_cache.user_tracking_opted_out(after_m.id, "nickname"):
            return

        await self.add_nickname(after_m)

    async def add_join(self, member: discord.Member):
        sql = """
        INSERT INTO member_join_logs(member_id, guild_id, time)
        VALUES($1, $2, $3)
        """

        await self.bot.pool.execute(
            sql, member.id, member.guild.id, discord.utils.utcnow()
        )

    @commands.Cog.listener("on_member_join")
    async def member_join_logs(self, member: discord.Member):
        if self.bot.db_cache.user_tracking_opted_out(member.id, "joins"):
            return

        await self.add_join(member)
