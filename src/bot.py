from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import textwrap
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Set, Optional

import aiohttp
import aioredis
import asyncpg
import discord
from cachetools import TTLCache
from discord.ext import commands
from ossapi import OssapiV2
from tweepy.asynchronous import AsyncClient, AsyncStreamingClient
from lastfm import AsyncClient as LastfmAsyncClient

from cogs.context import Context
from utils import (
    setup_accounts,
    setup_cache,
    setup_pokemon,
    setup_webhooks,
    create_pool,
)

if TYPE_CHECKING:
    from utils import Context

initial_extensions = [
    "jishaku",
    "cogs.owner",
    "cogs.context",
    "cogs.events.errors",
    "cogs.help",
    "cogs.tasks",
    "utils.discord_",
    "utils.helpers",
    "utils.vars",
]

extensions = [
    "cogs.discord_",
    "cogs.image",
    "cogs.search",
    "cogs.tools",
    "cogs.lastfm",
    "cogs.misc",
    "cogs.osu",
    "cogs.pokemon",
    "cogs.roblox",
    "cogs.settings",
    "cogs.moderation",
    # servers
    "cogs.servers.egg",
    "cogs.servers.jawntards",
    "cogs.servers.table",
    # events
    "cogs.events.auto_downloads",
    "cogs.events.auto_reactions",
    "cogs.events.commands",
    "cogs.events.errors",
    "cogs.events.guilds",
    "cogs.events.avatars",
    "cogs.events.messages",
    "cogs.events.users",
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

    comp = re.compile("^(" + "|".join(map(re.escape, packed)) + ").*", flags=re.I)  # type: ignore
    match = comp.match(message.content)

    if match is not None:
        return commands.when_mentioned_or(*[match.group(1)])(bot, message)

    return commands.when_mentioned_or(*packed)(bot, message)


class Bot(commands.Bot):
    session: aiohttp.ClientSession
    pool: asyncpg.Pool
    redis: aioredis.Redis
    exts: Set[str]
    lastfm: LastfmAsyncClient

    async def no_dms(self, ctx: Context):
        return ctx.guild is not None

    async def owner_only(self, ctx: Context):
        if ctx.author.id == self.owner_id:
            return True

        return not self.owner_only_mode

    async def block_list(self, ctx: Context):
        blocked = await self.redis.smembers("block_list")

        if str(ctx.author.id) in blocked:
            return False

        if str(ctx.guild.id) in blocked:
            return False

        if str(ctx.guild.owner_id) in blocked:
            return False

        return True

    async def no_auto_commands(self, ctx: Context):
        if ctx.command.name == "download":
            return str(ctx.channel.id) not in await self.redis.smembers(
                "auto_download_channels"
            )

        return True

    def __init__(
        self,
        intents: discord.Intents,
        config: Dict,
        testing: bool,
        logger: logging.Logger,
    ):
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            case_insensitive=True,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self._global_cooldown = commands.CooldownMapping.from_cooldown(
            20.0, 30.0, commands.BucketType.user
        )
        self.messages: TTLCache[str, discord.Message] = TTLCache(
            maxsize=1000, ttl=300.0
        )
        self.owner_only_mode: bool = True if testing else False
        self.avatar_webhooks: Dict[str, discord.Webhook] = {}
        self.image_webhooks: Dict[str, discord.Webhook] = {}
        self.icon_webhooks: Dict[str, discord.Webhook] = {}
        self.webhooks: Dict[str, discord.Webhook] = {}
        self.owner_id = 766953372309127168
        self.owner_ids = {}

        self.config: Dict[str, Any] = config
        self.logger = logger
        self.uptime: datetime.datetime
        self.embedcolor = 0xFAA0C1
        self.testing = testing
        self.pokemon: List[str] = []
        self.prefixes: Dict[int, List[str]] = {}
        self._context = Context
        self.spotify_key: Optional[str] = None

        self.add_check(self.no_dms)
        self.add_check(self.block_list)
        self.add_check(self.no_auto_commands)
        self.add_check(self.owner_only)

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:

        if before.content == after.content:
            return

        await self.process_commands(after)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
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

        await create_pool(
            self,
            self.config["databases"]["testing_psql"]
            if self.testing
            else self.config["databases"]["psql"],
        )
        print("Connected to postgre database")

        with open("schema.sql", "r") as f:
            await self.pool.execute(f.read())

        self.redis = await aioredis.from_url(
            self.config["databases"]["testing_redis_dns"]
            if self.testing
            else self.config["databases"]["redis_dns"],
            encoding="utf-8",
            decode_responses=True,
        )
        print("Connected to redis database")

        self.lastfm = LastfmAsyncClient(
            self.config["keys"]["lastfm-key"], session=self.session
        )
        print("Connected to lastfm client")

        self.osu = OssapiV2(
            self.config["keys"]["osu-id"], self.config["keys"]["osu-secret"]
        )
        print("Connected to osu! account")

        await setup_cache(self)
        print("Setup cache")

        await setup_webhooks(self)
        print("Setup webhooks")

        await setup_pokemon(self)
        print("Loaded pokemon")

        await setup_accounts(self)
        print("Setup accounts")

        self.exts = set(initial_extensions + extensions)

        for extension in self.exts if not self.testing else initial_extensions:
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

    async def send_error(self, ctx: Context, error: commands.CommandError | Exception):
        await ctx.send(f"An unhandled error occured, this error has been reported.")
        embed = discord.Embed(title="Command Error", colour=self.embedcolor)
        embed.add_field(name="Name", value=ctx.command.qualified_name)
        embed.add_field(name="Author", value=f"{ctx.author} (ID: {ctx.author.id})")

        fmt = f"Channel: {ctx.channel} (ID: {ctx.channel.id})"

        if ctx.guild:
            fmt = f"{fmt}\nGuild: {ctx.guild} (ID: {ctx.guild.id})"

        embed.add_field(name="Location", value=fmt, inline=False)
        embed.add_field(
            name="Content", value=textwrap.shorten(ctx.message.content, 512)
        )

        exc = "".join(
            traceback.format_exception(
                type(error), error, error.__traceback__, chain=False
            )
        )
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )
        embed.description = f"```py\n{exc}\n```"
        embed.timestamp = discord.utils.utcnow()
        await self.webhooks["error_logs"].send(embed=embed)
        return

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


if __name__ == "__main__":
    print("Hello, please run the launcher.py file!")
