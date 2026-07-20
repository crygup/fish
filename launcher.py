import argparse
import asyncio
import logging.handlers
import os
import sys
import tomllib

import aiohttp
import uvicorn

from api import app as api_app
from api import init as api_init
from core import Fishie
from utils import Config, base_header, create_pool


async def start(testing: bool):
    logger = logging.getLogger("fishie")
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

    logger.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    config_path = os.getenv("FISHIE_CONFIG", "config.toml")
    with open(config_path, "rb") as fileObj:
        config: Config = Config(**tomllib.load(fileObj))

    database_url = os.getenv("DATABASE_URL") or config["databases"][
        "psql_testing" if testing else "psql"
    ]
    token = os.getenv(
        "DISCORD_TESTING_BOT_TOKEN" if testing else "DISCORD_BOT_TOKEN"
    ) or config["tokens"]["testing_bot" if testing else "bot"]
    if not database_url:
        raise RuntimeError("A PostgreSQL URL is required")
    if not token:
        raise RuntimeError("A Discord bot token is required")
    if not os.getenv("FISHIE_CREDENTIAL_KEY"):
        raise RuntimeError(
            "FISHIE_CREDENTIAL_KEY is required; generate one with Fernet.generate_key()"
        )

    jsk_envs = [
        "JISHAKU_RETAIN",
        "JISHAKU_HIDE",
        "JISHAKU_NO_DM_TRACEBACK",
        "JISHAKU_NO_UNDERSCORE",
        "JISHAKU_FORCE_PAGINATOR",
    ]

    for env in jsk_envs:
        os.environ[env] = "True"

    pool = await create_pool(database_url)
    logger.info("Connected to Postgres")

    timeout = aiohttp.ClientTimeout(total=30)
    async with (
        aiohttp.ClientSession(headers=base_header, timeout=timeout) as session,
        Fishie(
            config=config, logger=logger, pool=pool, session=session, testing=testing
        ) as bot,
    ):
        api_init(bot)
        api_cfg = uvicorn.Config(
            api_app,
            host=os.getenv("FISHIE_API_HOST", "127.0.0.1"),
            port=int(os.getenv("FISHIE_API_PORT", "8001")),
            log_level=os.getenv("FISHIE_API_LOG_LEVEL", "warning"),
        )
        api_server = uvicorn.Server(api_cfg)
        api_task = asyncio.create_task(api_server.serve())
        bot_task = asyncio.create_task(
            bot.start(token)
        )
        logger.info("Fishie API server started")
        done, _ = await asyncio.wait(
            {api_task, bot_task}, return_when=asyncio.FIRST_COMPLETED
        )
        try:
            for task in done:
                task.result()
        finally:
            api_server.should_exit = True
            if not api_task.done():
                try:
                    await asyncio.wait_for(api_task, timeout=10)
                except asyncio.TimeoutError:
                    api_task.cancel()
            if not bot.is_closed():
                await bot.close()
            await asyncio.gather(api_task, bot_task, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testing", "-t", action="store_true")

    parsed = parser.parse_args()

    asyncio.run(start(parsed.testing))
