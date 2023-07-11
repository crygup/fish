from __future__ import annotations

import datetime
import pkgutil
from logging import Logger
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, Type, TypeVar, Union

import aiohttp
import asyncpg
import discord
from discord.abc import Messageable
from discord.ext import commands
from redis import asyncio as aioredis

from utils import MESSAGE_RE, Config, emojis

if TYPE_CHECKING:
    from extensions.context import Context
    from extensions.tools import Tools
    from extensions.events import Events
    from extensions.logging import Logging

FCT = TypeVar("FCT", bound="Context")


class Fishie(commands.Bot):
    redis: aioredis.Redis[Any]
    custom_emojis = emojis
    cached_covers: Dict[str, Tuple[str, bool]] = {}

    def __init__(
        self,
        config: Config,
        logger: Logger,
        pool: "asyncpg.Pool[asyncpg.Record]",
        session: aiohttp.ClientSession,
    ):
        self.config: Config = config
        self.logger: Logger = logger
        self.pool = pool
        self.session = session
        self.start_time: datetime.datetime
        self.embed_color = 0x2B2D31
        self.context_cls: Type[commands.Context[Fishie]] = commands.Context
        self._extensions = [
            m.name for m in pkgutil.iter_modules(["./extensions"], prefix="extensions.")
        ]
        self.spotify_key: Optional[str] = None
        self.cached_covers: Dict[str, Tuple[str, bool]] = {}
        super().__init__(
            command_prefix=commands.when_mentioned_or(";"),
            intents=discord.Intents.all(),
        )

    async def load_extensions(self):
        for ext in self._extensions:
            try:
                await self.load_extension(ext)
                self.logger.info(f"Loaded extension: {ext}")
            except Exception as e:
                self.logger.warn(f"Failed to load extension: {ext}")
                self.logger.warn(f"{e.__class__.__name__}: {str(e)}")
                continue

    async def unload_extensions(self):
        for ext in self._extensions:
            try:
                await self.unload_extension(ext)
                self.logger.info(f"Unloaded extension: {ext}")
            except Exception as e:
                self.logger.warn(f"Failed to unload extension: {ext}")
                self.logger.warn(f"{e.__class__.__name__}: {str(e)}")
                continue

    async def reload_extensions(self):
        for ext in self._extensions:
            try:
                await self.reload_extension(ext)
                self.logger.info(f"Reloaded extension: {ext}")
            except Exception as e:
                self.logger.warn(f"Failed to reload extension: {ext}")
                self.logger.warn(f"{e.__class__.__name__}: {str(e)}")
                continue

    async def setup_hook(self) -> None:
        with open("schema.sql") as fp:
            await self.pool.execute(fp.read())

        await self.load_extensions()
        await self.populate_cache()

    async def on_ready(self):
        if not hasattr(self, "start_time"):
            self.uptime = discord.utils.utcnow()
            self.logger.info(f"Logged into {str(self.user)}")

    async def fetch_message(
        self, *, message: Union[str, int], channel: Optional[Messageable] = None
    ) -> discord.Message:
        if isinstance(message, int):
            if channel is None:
                raise TypeError("Channel is required when providing message ID")

            return await channel.fetch_message(message)

        msg_match = MESSAGE_RE.match(message)

        if msg_match:
            _channel = self.get_channel(int(msg_match.group(2)))

            if _channel is None:
                raise commands.ChannelNotFound(msg_match.group(2))

            if not isinstance(_channel, Messageable):
                raise TypeError("Channel is not messageable")

            return await _channel.fetch_message(int(msg_match.group(3)))

        raise ValueError("Could not find channel with provided arguments, try again.")

    async def get_context(
        self,
        message: discord.Message | discord.Interaction[Fishie],
        *,
        cls: Type[FCT] = None,
    ) -> Context | commands.Context[Fishie]:
        new_cls = cls or self.context_cls
        return await super().get_context(message, cls=new_cls)

    async def close(self) -> None:
        self.logger.info("Logging out")
        await self.unload_extensions()
        await self.close_sessions()
        await super().close()

    async def close_sessions(self):
        await self.pool.close()
        self.logger.info("Closed Postgres session")
        await self.redis.close()
        self.logger.info("Closed Redis session")
        await self.session.close()
        self.logger.info("Closed aiohttp session")

    async def populate_cache(self):
        sql = """SELECT * FROM accounts"""
        results = await self.pool.fetch(sql)

        for record in results:
            user_id = record["user_id"]
            fm = record["last_fm"]

            if fm:
                await self.redis.set(f"fm:{user_id}", fm)
                self.logger.info(f'Added user "{user_id}"\'s last.fm account "{fm}"')

    @property
    def tools(self) -> Optional[Tools]:
        return self.get_cog("Tools")  # type: ignore

    @property
    def events(self) -> Optional[Events]:
        return self.get_cog("Events")  # type: ignore

    @property
    def logging(self) -> Optional[Logging]:
        return self.get_cog("Logging")  # type: ignore
