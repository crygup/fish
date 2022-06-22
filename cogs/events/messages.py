import discord
from discord.ext import commands
from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(MessageEvents(bot))


class MessageEvents(commands.Cog, name="message_event"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.content == '' and message.attachments == []:
            return

        if message.attachments:
            sql = """
            INSERT INTO message_attachment_logs(message_id, attachment)
            VALUES($1, $2)
            """
            sql_many = [(message.id, await attachment.read()) for attachment in message.attachments]
            await self.bot.pool.executemany(sql, sql_many)

        sql = """
        INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at)
        VALUES($1, $2, $3, $4, $5, $6)
        """

        await self.bot.pool.execute(
            sql,
            message.author.id,
            message.guild.id,
            message.channel.id,
            message.id,
            message.content,
            message.created_at,
        )
