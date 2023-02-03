import asyncio
import logging
import logging.handlers
import sys

import discord
import toml
from discord import gateway

from bot import Bot
from utils import mobile

# monkey patching
gateway.DiscordWebSocket.identify = mobile

testing = False


async def main():
    logger = logging.getLogger("discord")
    logger.setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.INFO)

    handlers = [
        logging.handlers.RotatingFileHandler(
            filename="discord.log",
            encoding="utf-8",
            maxBytes=32 * 1024 * 1024,  # 32 MiB
            backupCount=5,  # Rotate through 5 files
        ),
        logging.StreamHandler(sys.stdout),
    ]

    formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{"
    )

    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    config = toml.load("config.toml")

    bot = Bot(config, testing, logger)

    async with bot:
        await bot.start(config["tokens"]["evi"] if testing else config["tokens"]["bot"])


asyncio.run(main())
