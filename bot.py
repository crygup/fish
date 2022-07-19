from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import aiohttp
import asyncpg
import discord
import pandas as pd
from discord.ext import commands

if TYPE_CHECKING:
    from utils import GuildContext

initial_extensions = {
    "jishaku",
    "cogs.owner",
}
bot_extensions = {
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

    async def no_dms(self, ctx: GuildContext):
        return ctx.guild is not None

    async def user_blacklist(self, ctx: GuildContext):
        return ctx.author.id not in self.blacklisted_users

    async def guild_blacklist(self, ctx: GuildContext):
        if ctx.guild is None:
            return True

        return ctx.guild.id not in self.blacklisted_guilds

    async def guild_owner_blacklist(self, ctx: GuildContext):
        if ctx.guild is None:
            return True

        return ctx.guild.owner_id not in self.blacklisted_users

    async def cooldown_check(self, ctx: GuildContext):
        if ctx.author.id == self.owner_id:
            return True

        bucket = self._global_cooldown.get_bucket(ctx.message)

        if bucket is None:
            return True

        retry_after = bucket.update_rate_limit()

        if retry_after:
            sql = """
            INSERT INTO user_blacklist(user_id, reason, time)
            """
            await self.pool.execute(
                sql,
                ctx.author.id,
                "Auto-blacklist from command spam",
                discord.utils.utcnow(),
            )
            self.user_blacklist.append(ctx.author.id)

            if ctx.guild.owner_id == ctx.author.id:
                sql = """
                INSERT INTO guild_blacklist(guild_id, reason, time)
                """
                await self.pool.execute(
                    sql,
                    ctx.guild.id,
                    "Auto-blacklist from command spam",
                    discord.utils.utcnow(),
                )
                self.blacklisted_guilds.append(ctx.guild.id)

            await ctx.send(
                "You have been automatically blacklisted for spamming commands, contact cr#0333 if you think this was a mistake."
            )

            return False

        return True

    def __init__(
        self,
        intents: discord.Intents,
        config: Dict,
        testing: bool,
        logger: logging.Logger,
    ):
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
        self.logger = logger
        self.uptime: Optional[datetime.datetime] = None
        self.embedcolor = 0xFAA0C1
        self.webhooks: Dict[str, discord.Webhook] = {}
        self.testing = testing
        self.pokemon: List[str] = []
        self.blacklisted_guilds: List[int] = []
        self.blacklisted_users: List[int] = []
        self.whitelisted_users: List[int] = []
        self.poketwo_guilds: List[int] = []
        self._global_cooldown = commands.CooldownMapping.from_cooldown(
            20.0, 30.0, commands.BucketType.member
        )
        self.add_check(self.no_dms)
        self.add_check(self.user_blacklist)
        self.add_check(self.guild_blacklist)
        self.add_check(self.guild_owner_blacklist)
        self.add_check(self.cooldown_check)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        _database = (
            self.config["databases"]["testing_psql"]
            if self.testing
            else self.config["databases"]["psql"]
        )
        connection = await asyncpg.create_pool(_database)

        if connection is None:
            self.logger.error("Failed to connect to database")
            return

        self.pool = connection
        print("Connected to database")

        blacklisted_guilds = await self.pool.fetch(
            "SELECT guild_id FROM guild_blacklist"
        )
        self.blacklisted_guilds = [guild["guild_id"] for guild in blacklisted_guilds]
        print(f"Loaded {len(self.blacklisted_guilds)} blacklisted guilds")

        blacklisted_users = await self.pool.fetch("SELECT user_id FROM user_blacklist")
        self.blacklisted_users = [user["user_id"] for user in blacklisted_users]
        print(f"Loaded {len(self.blacklisted_users)} blacklisted users")

        whitelisted_users = await self.pool.fetch("SELECT user_id FROM user_whitelist")
        self.whitelisted_users = [user["user_id"] for user in whitelisted_users]
        print(f"Loaded {len(self.whitelisted_users)} whitelisted users")

        poketwo_guilds = await self.pool.fetch("SELECT guild_id FROM poketwo_whitelist")
        self.poketwo_guilds = [guild["guild_id"] for guild in poketwo_guilds]
        print(f"Loaded {len(self.poketwo_guilds)} poketwo guilds")

        self.webhooks["error_logs"] = discord.Webhook.from_url(
            url=self.config["webhooks"]["error_logs"], session=self.session
        )

        self.webhooks["join_logs"] = discord.Webhook.from_url(
            url=self.config["webhooks"]["join_logs"], session=self.session
        )
        print("Loaded webhooks")

        url = "https://raw.githubusercontent.com/poketwo/data/master/csv/pokemon.csv"
        data = pd.read_csv(url)
        pokemon = [str(p).lower() for p in data["name.en"]]

        for p in pokemon:
            if re.search(r"[\U00002640\U0000fe0f|\U00002642\U0000fe0f]", p):
                pokemon[pokemon.index(p)] = re.sub(
                    "[\U00002640\U0000fe0f|\U00002642\U0000fe0f]", "", p
                )
            if re.search(r"[\U000000e9]", p):
                pokemon[pokemon.index(p)] = re.sub("[\U000000e9]", "e", p)

        self.pokemon = pokemon
        print("Loaded pokemon")

        if not self.testing:
            for extension in bot_extensions:
                initial_extensions.add(extension)

        for extension in initial_extensions:
            try:
                await self.load_extension(extension)
                print(f"Loaded extension {extension}")
            except Exception as e:
                print(f"Failed to load {extension}: {e}")

    async def on_ready(self):
        if self.uptime is None:
            self.uptime = discord.utils.utcnow()

        print(f"Logged in as {self.user}")

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
