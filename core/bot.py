import datetime
import pkgutil
from logging import Logger
from typing import Optional, Union

import aiohttp
import discord
from discord.abc import Messageable
from discord.ext import commands

from utils import MESSAGE_RE, Config


class Fishie(commands.Bot):
    session: aiohttp.ClientSession

    def __init__(self, config: Config, logger: Logger):
        self.config: Config = config
        self.logger: Logger = logger
        self.start_time: datetime.datetime
        self._extensions = [
            m.name for m in pkgutil.iter_modules(["./extensions"], prefix="extensions.")
        ]
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
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
            }
        )

        await self.load_extensions()

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
