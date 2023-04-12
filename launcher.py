import argparse
import asyncio
import logging.handlers
import os
import sys
import tomllib

from redis import asyncio as aioredis

from core import Fishie, create_pool
from utils import Config


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

    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    with open("config.toml", "rb") as fileObj:
        config: Config = tomllib.load(fileObj)  # type: ignore # using my own TypedDict instead of Dict[str, Any]

    jsk_envs = [
        "JISHAKU_RETAIN",
        "JISHAKU_HIDE",
        "JISHAKU_NO_DM_TRACEBACK",
        "JISHAKU_NO_UNDERSCORE",
        "JISHAKU_FORCE_PAGINATOR",
    ]

    for env in jsk_envs:
        os.environ[env] = "True"

    pool = await create_pool(
        config["databases"]["testing_postgre_dsn" if testing else "postgre_dsn"]
    )
    logger.info("Connected to Postgres")

    redis = await aioredis.from_url(config["databases"]["testing_redis_dsn"] if testing else "redis_dsn")  # type: ignore
    logger.info("Connected to Redis")

    async with Fishie(config=config, logger=logger) as fishie:
        token = fishie.config["tokens"]["bot"]
        fishie.pool = pool
        fishie.redis = redis
        await fishie.start(token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testing", "-t", required=False, default=False, type=bool)

    parsed = parser.parse_args()

    asyncio.run(start(parsed.testing))
