from __future__ import annotations

import datetime
import logging
import os
import re
import textwrap
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import aiohttp
import aioredis
import asyncpg
import discord
from cachetools import TTLCache
from discord.ext import commands
from lastfm import AsyncClient as LastfmAsyncClient
from ossapi import OssapiV2

from cogs.context import Context
from utils import (
    block_list,
    create_pool,
    get_extensions,
    get_prefix,
    google_cooldown_check,
    initial_extensions,
    no_auto_commands,
    no_dms,
    owner_only,
    setup_accounts,
    setup_cache,
    setup_pokemon,
    setup_webhooks,
)

if TYPE_CHECKING:
    from utils import Context

os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_NO_DM_TRACEBACK"] = "True"

intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.members = True


class Bot(commands.Bot):
    session: aiohttp.ClientSession
    pool: asyncpg.Pool
    redis: aioredis.Redis
    exts: Set[str]
    lastfm: LastfmAsyncClient

    def __init__(
        self,
        config: Dict,
        testing: bool,
        logger: logging.Logger,
    ):
        # fmt: off
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            case_insensitive=True,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
        )
        self.messages: TTLCache[str, discord.Message] = TTLCache(maxsize=1000, ttl=300.0)
        self.owner_only_mode: bool = True if testing else False

        # webhooks
        self.avatar_webhooks: Dict[str, discord.Webhook] = {}
        self.image_webhooks: Dict[str, discord.Webhook] = {}
        self.icon_webhooks: Dict[str, discord.Webhook] = {}
        self.webhooks: Dict[str, discord.Webhook] = {}

        # ids
        self.owner_id = 766953372309127168
        self.owner_ids = {}

        # config
        self.exts = set(initial_extensions + get_extensions())
        self.cached_covers: Dict[str, Tuple[str, bool]] = {}
        self.prefixes: Dict[int, List[str]] = {}
        self.current_downloads: List[str] = []
        self.spotify_key: Optional[str] = None
        self.config: Dict[str, Any] = config
        self.uptime: datetime.datetime
        self.pokemon: List[str] = []
        self.embedcolor = 0xFAA0C1
        self._context = Context
        self.testing = testing
        self.logger = logger

        # checks
        self.add_check(no_dms)
        self.add_check(block_list)
        self.add_check(no_auto_commands)
        self.add_check(owner_only)
        self.add_check(google_cooldown_check)

        # cooldowns
        self.google_cooldown = commands.CooldownMapping.from_cooldown(100, 86400, commands.BucketType.default)
        self.global_cooldown = commands.CooldownMapping.from_cooldown(60, 30, commands.BucketType.user)
        self.error_message_cooldown = commands.CooldownMapping.from_cooldown(3, 15, commands.BucketType.user)
        # fmt: on

    async def post_error(self, ctx: Context, excinfo: str):
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

        embed.description = f"```py\n{excinfo}\n```"
        embed.timestamp = discord.utils.utcnow()
        await self.webhooks["error_logs"].send(embed=embed)

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

    async def load_extensions(self):
        for extension in self.exts if not self.testing else initial_extensions:
            try:
                await self.load_extension(extension)
                self.logger.info(f"Loaded extension {extension}")
            except Exception as e:
                self.logger.warn(f"Failed to load {extension}: {e}")

    async def unload_extensions(self):
        for extension in self.exts if not self.testing else initial_extensions:
            try:
                await self.unload_extension(extension)
                self.logger.info(f"Unloaded extension {extension}")
            except Exception as e:
                self.logger.warn(f"Failed to unload {extension}: {e}")

    async def reload_extensions(self):
        for extension in self.exts if not self.testing else initial_extensions:
            try:
                await self.reload_extension(extension)
                self.logger.info(f"Reoaded extension {extension}")
            except Exception as e:
                self.logger.warn(f"Failed to reload {extension}: {e}")

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()

        await create_pool(
            self,
            self.config["databases"]["testing_psql"]
            if self.testing
            else self.config["databases"]["psql"],
        )

        with open("schema.sql", "r") as f:
            await self.pool.execute(f.read())

        self.redis = await aioredis.from_url(
            self.config["databases"]["testing_redis_dns"]
            if self.testing
            else self.config["databases"]["redis_dns"],
            encoding="utf-8",
            decode_responses=True,
        )

        # fmt:off
        self.lastfm = LastfmAsyncClient(self.config["keys"]["lastfm-key"], session=self.session)
        self.osu = OssapiV2(self.config["keys"]["osu-id"], self.config["keys"]["osu-secret"])
        await setup_cache(self)
        await setup_webhooks(self)
        await setup_pokemon(self)
        await setup_accounts(self)
        await self.load_extensions()
        # fmt:on

    async def get_context(self, message: discord.Message, *, cls=None):
        new_cls = cls or self._context
        return await super().get_context(message, cls=new_cls)

    async def on_ready(self):
        if not hasattr(self, "uptime"):
            self.uptime = discord.utils.utcnow()

        self.logger.info(f"Logged in as {self.user}")

    async def close(self):
        await self.unload_extensions()

        await self.session.close()
        await self.pool.close()
        await self.redis.close()

        await super().close()


if __name__ == "__main__":
    print("Hello, please run the launcher.py file!")
