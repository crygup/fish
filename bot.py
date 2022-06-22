import datetime, os
from typing import Dict, Optional

import aiohttp
import asyncpg
import discord
from discord.ext import commands

initial_extensions = {
    "jishaku",
    "cogs.owner",
    "cogs.events.members",
    "cogs.events.messages",
    "cogs.events.users",
    "cogs.events.errors",
}
os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_NO_DM_TRACEBACK"] = "True"


class Bot(commands.Bot):
    session: aiohttp.ClientSession
    pool: asyncpg.Pool

    def __init__(self, intents: discord.Intents, config: Dict):
        super().__init__(
            command_prefix=commands.when_mentioned_or("fish "),
            intents=intents,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.config: Dict = config
        self.uptime: Optional[datetime.datetime] = None
        self.embedcolor = 0xfaa0c1
        self.webhooks: Dict[str, discord.Webhook] = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        connection = await asyncpg.create_pool(self.config["databases"]["psql"])

        if connection is None:
            raise asyncpg.ConnectionFailureError("Failed to connect to database")

        self.pool = connection

        self.webhooks["error_logs"] = discord.Webhook.from_url(url=self.config["webhooks"]["error_logs"], session=self.session)

        for extension in initial_extensions:
            try:
                await self.load_extension(extension)
            except Exception as e:
                print(f"Failed to load {extension}: {e}")

    async def on_ready(self):
        if self.uptime is None:
            self.uptime = discord.utils.utcnow()

        print(f"Logged in as {str(self.user)}")

    async def close(self):
        for extension in self.extensions:
            try:
                await self.unload_extension(extension)
            except Exception:
                pass

        await self.session.close()
        await self.pool.close()

        await super().close()


if __name__ == "__main__":
    print("Hello, please run the launcher.py file!")
