import datetime
import os
import re
from typing import Dict, List, Optional, Tuple

import aiohttp
import asyncpg
import discord
import pandas as pd
from discord.ext import commands

initial_extensions = {
    "jishaku",
    "cogs.owner",
    "cogs.tools",
    "cogs.events.errors",
    "cogs.events.guilds",
    "cogs.events.members",
    "cogs.events.messages",
    "cogs.events.users",
    "cogs.pokemon",
}
os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_NO_DM_TRACEBACK"] = "True"


class Bot(commands.Bot):
    session: aiohttp.ClientSession
    pool: asyncpg.Pool

    async def no_dms(self, ctx: commands.Context):
        return ctx.guild is not None

    def __init__(self, intents: discord.Intents, config: Dict, testing: bool):
        prefix: List = ["fish ", "f"] if not testing else ["fish. ", "f."]
        super().__init__(
            command_prefix=commands.when_mentioned_or(*prefix),
            intents=intents,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.config: Dict = config
        self.uptime: Optional[datetime.datetime] = None
        self.embedcolor = 0xFAA0C1
        self.webhooks: Dict[str, discord.Webhook] = {}
        self.testing = testing
        self.pokemon: List = []
        self.add_check(self.no_dms)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        _database = (
            self.config["databases"]["testing_psql"]
            if self.testing
            else self.config["databases"]["psql"]
        )
        connection = await asyncpg.create_pool(_database)

        if connection is None:
            raise asyncpg.ConnectionFailureError("Failed to connect to database")

        self.pool = connection

        self.webhooks["error_logs"] = discord.Webhook.from_url(
            url=self.config["webhooks"]["error_logs"], session=self.session
        )

        url = "https://raw.githubusercontent.com/poketwo/data/master/csv/pokemon.csv"
        data = pd.read_csv(url)
        pokemon = [str(p).lower() for p in data["name.en"]]

        for p in pokemon:
            if re.search(r"[♀️|♂️]", p):
                pokemon[pokemon.index(p)] = re.sub(r"[♀️|♂️]", "", p)
            if re.search(r"[é]", p):
                pokemon[pokemon.index(p)] = re.sub(r"[é]", "e", p)

        self.pokemon = pokemon

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
        for extension in initial_extensions:
            try:
                await self.unload_extension(extension)
            except Exception:
                pass

        await self.session.close()
        await self.pool.close()

        await super().close()


if __name__ == "__main__":
    print("Hello, please run the launcher.py file!")
