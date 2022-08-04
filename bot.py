from __future__ import annotations

import datetime
import logging
import os
import pathlib
import re
from typing import TYPE_CHECKING, Dict, List

import aiohttp
import aioredis
import asyncpg
import cachetools
import discord
from discord.ext import commands
from ossapi import OssapiV2

from cogs.context import Context
from utils import (
    setup_cache,
    setup_pokemon,
    setup_prefixes,
    setup_webhooks,
    setup_accounts,
)

if TYPE_CHECKING:
    from utils import Context

initial_extensions = ["jishaku", "cogs.owner", "cogs.context", "cogs.events.errors"]
cogs_path = pathlib.Path("./cogs")
cogs = [
    x.as_posix().replace("/", ".")[:-3]
    for x in cogs_path.glob("**/*.py")
    if x.parent.name != "examples"
]
os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_NO_DM_TRACEBACK"] = "True"


async def get_prefix(bot: Bot, message: discord.Message) -> List[str]:
    default = ["fish "] if not bot.testing else ["fish. "]
    if message.guild is None:
        return commands.when_mentioned_or(*default)(bot, message)

    try:
        prefixes = bot.prefixes[message.guild.id]
    except KeyError:
        prefixes = []

    packed = default + prefixes

    return commands.when_mentioned_or(*packed)(bot, message)


class Bot(commands.Bot):
    session: aiohttp.ClientSession
    pool: asyncpg.Pool
    redis: aioredis.Redis

    async def no_dms(self, ctx: Context):
        return ctx.guild is not None

    async def user_blacklist(self, ctx: Context):
        blacklisted_users = await self.redis.smembers("blacklisted_users")
        return str(ctx.author.id) not in blacklisted_users

    async def guild_blacklist(self, ctx: Context):
        blacklisted_guilds = await self.redis.smembers("blacklisted_guilds")
        return str(ctx.guild.id) not in blacklisted_guilds

    async def guild_owner_blacklist(self, ctx: Context):
        blacklisted_owners = await self.redis.smembers("blacklisted_users")
        return str(ctx.guild.owner_id) not in blacklisted_owners

    async def no_auto_commands(self, ctx: Context):
        return str(ctx.channel.id) not in await self.redis.smembers(
            "auto_download_channels"
        )

    def __init__(
        self,
        intents: discord.Intents,
        config: Dict,
        testing: bool,
        logger: logging.Logger,
    ):
        prefix: List = ["fish ", "f"] if not testing else ["fish. ", "f."]
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            case_insensitive=False,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.config: Dict = config
        self.logger = logger
        self.uptime: datetime.datetime
        self.embedcolor = 0xFAA0C1
        self.webhooks: Dict[str, discord.Webhook] = {}
        self.testing = testing
        self.pokemon: List[str] = []
        self.prefixes: Dict[int, List[str]] = {}
        self.e_reply = "<:reply:972280355136606209>"
        self.e_replies = "<:replies:972280398874824724>"
        self._context = Context
        self._global_cooldown = commands.CooldownMapping.from_cooldown(
            20.0, 30.0, commands.BucketType.user
        )
        self.messages: cachetools.TTLCache[str, discord.Message] = cachetools.TTLCache(
            maxsize=1000, ttl=300.0
        )
        self.add_check(self.no_dms)
        self.add_check(self.user_blacklist)
        self.add_check(self.guild_blacklist)
        self.add_check(self.guild_owner_blacklist)
        self.add_check(self.no_auto_commands)

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:

        if before.content == after.content:
            return

        await self.process_commands(after)

    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """|coro|
        Called every time a message is deleted.
        Parameters
        ----------
        message: :class:`~discord.Message`
            The message that was deleted.
        """
        _repr_regex = rf"<utils\.Context bound to message \({payload.channel_id}-{payload.message_id}-[0-9]+\)>"
        pattern = re.compile(_repr_regex)
        messages = {r: m for r, m in self.messages.items() if pattern.fullmatch(r)}
        for _repr, message in messages.items():
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                del self.messages[_repr]
            except KeyError:
                pass

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
        print("Connected to postre database")

        self.redis = await aioredis.from_url(
            self.config["databases"]["testing_redis_dns"]
            if self.testing
            else self.config["databases"]["redis_dns"],
            encoding="utf-8",
            decode_responses=True,
        )
        print("Connected to Redis database")

        self.osu = OssapiV2(
            self.config["keys"]["osu_id"], self.config["keys"]["osu_secret"]
        )
        print("Connected to osu! account")

        await setup_cache(self.pool, self.redis)
        print("Setup cache")

        await setup_webhooks(self)
        print("Setup webhooks")

        await setup_pokemon(self)
        print("Loaded pokemon")

        await setup_prefixes(self, self.pool)
        print("Setup prefixes")

        await setup_accounts(self.pool, self.redis)
        print("Setup accounts")

        if not self.testing:
            initial_extensions.extend([cog for cog in cogs])

        for extension in set(initial_extensions):
            try:
                await self.load_extension(extension)
                print(f"Loaded extension {extension}")
            except Exception as e:
                print(f"Failed to load {extension}: {e}")

    async def get_context(self, message: discord.Message, *, cls=None):
        new_cls = cls or self._context
        return await super().get_context(message, cls=new_cls)

    async def on_ready(self):
        if not hasattr(self, "uptime"):
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
        await self.redis.close()

        await super().close()

    async def getch_user(self, user_id: int) -> discord.User:
        user = self.get_user(user_id)

        if user is None:
            user = await self.fetch_user(user_id)

        return user


if __name__ == "__main__":
    print("Hello, please run the launcher.py file!")
