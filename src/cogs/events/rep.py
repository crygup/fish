from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional
import discord
from discord.ext import commands
from utils import TATSU_ID, REP_RE, DISCORD_ID_RE

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Rep(bot))


class Rep(commands.Cog, name="rep"):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def from_interaction(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.id != TATSU_ID:
            return

        if message.interaction is None:
            return

        if message.interaction.name != "reputation":
            return

        user_id = DISCORD_ID_RE.search(message.content)

        if user_id is None:
            return

        sql = """
        INSERT INTO tatsu_rep_logs(user_id, target_id, guild_id, created_at)
        VALUES($1, $2, $3, $4)
        """

        await self.bot.pool.execute(
            sql,
            message.interaction.user.id,
            int(user_id.group(0)),
            message.guild.id,
            discord.utils.utcnow(),
        )

    async def get_member(
        self,
        message: discord.Message,
        members: List[discord.Member],
    ) -> Optional[int]:
        users = [
            msg.author.id
            for msg in self.bot.cached_messages
            if msg.channel.id == message.channel.id
            and "rep" in msg.content
            and msg.author.id in [m.id for m in members]
        ]

        if not users:
            async for msg in message.channel.history(before=message, limit=2):
                if msg.author.id not in [m.id for m in members]:
                    return None
                if not "rep" in msg.content:
                    return None

                return msg.author.id
        else:
            return users[0]

        return None

    async def from_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.id != TATSU_ID:
            return

        if message.interaction:
            return

        rep_match = REP_RE.fullmatch(message.content)

        if rep_match is None:
            return

        target_id = int(rep_match.group(2))

        members = await message.guild.query_members(rep_match.group(1), cache=True)

        if not members:
            return

        if len(members) > 1:
            check = await self.get_member(message, members)

            if check is None:
                return

            user_id = check
        else:
            user_id = members[0].id

        sql = """
        INSERT INTO tatsu_rep_logs(user_id, target_id, guild_id, created_at)
        VALUES($1, $2, $3, $4)
        """

        await self.bot.pool.execute(
            sql,
            user_id,
            target_id,
            message.guild.id,
            discord.utils.utcnow(),
        )

    @commands.Cog.listener("on_message")
    async def interaction_rep(self, message: discord.Message):
        await self.from_interaction(message)

    @commands.Cog.listener("on_message")
    async def message_rep(self, message: discord.Message):
        await self.from_message(message)
