import discord
from bot import Bot
from discord.ext import commands
from utils import GuildContext, get_video


async def setup(bot: Bot):
    await bot.add_cog(Downloads(bot))


class Downloads(commands.Cog, name="downloads"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.cooldown = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.user)

    async def try_delete(self, message: discord.Message):
        try:
            await message.delete()
        except:
            pass

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        me = self.bot.user

        if me is None:
            return

        if message.author.bot:
            await self.try_delete(message)
            return

        if message.author.id == me.id:
            return

        if message.channel.id not in self.bot.auto_download_channels:
            return

        ctx: GuildContext = await self.bot.get_context(message) # type: ignore

        video = await get_video(ctx, message.content)

        if video is None:
            await self.try_delete(message)
            return

        bucket = self.cooldown.get_bucket(message)

        if bucket is None:
            await self.try_delete(message)
            return

        retry_after = bucket.update_rate_limit()

        if retry_after:
            await self.try_delete(message)
            await message.channel.send(f"Please wait {round(retry_after)} seconds before sending another video.", delete_after=retry_after)
            return

        command = self.bot.get_command("download")

        if command is None:
            return

        await command(ctx, url=video, flags=None)