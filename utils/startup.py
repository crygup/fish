import re
from typing import TYPE_CHECKING

import aioredis
import asyncpg
import discord
import pandas as pd

from .helpers import add_prefix

if TYPE_CHECKING:
    from bot import Bot
else:
    from discord.ext.commands import Bot


async def setup_cache(pool: asyncpg.Pool, redis: aioredis.Redis):
    blacklisted_guilds = await pool.fetch("SELECT guild_id FROM guild_blacklist")
    for guild in blacklisted_guilds:
        await redis.sadd("blacklisted_guilds", guild["guild_id"])

    blacklisted_users = await pool.fetch("SELECT user_id FROM user_blacklist")
    for user in blacklisted_users:
        await redis.sadd("blacklisted_users", user["user_id"])

    guild_settings = await pool.fetch("SELECT * FROM guild_settings")
    for guild in guild_settings:
        if guild["poketwo"]:
            await redis.sadd("poketwo_guilds", guild["guild_id"])
        if guild["auto_download"]:
            await redis.sadd("auto_download_channels", guild["guild_id"])


async def setup_webhooks(bot: Bot):
    bot.webhooks["error_logs"] = discord.Webhook.from_url(
        url=bot.config["webhooks"]["error_logs"], session=bot.session
    )

    bot.webhooks["join_logs"] = discord.Webhook.from_url(
        url=bot.config["webhooks"]["join_logs"], session=bot.session
    )


async def setup_pokemon(bot: Bot):
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

    bot.pokemon = pokemon


async def setup_accounts(pool: asyncpg.Pool, redis: aioredis.Redis):
    accounts = await pool.fetch("SELECT * FROM accounts")
    for record in accounts:
        if record["osu"]:
            await redis.hset(f"accounts:{record['user_id']}", "osu", record["osu"])
        if record["lastfm"]:
            await redis.hset(
                f"accounts:{record['user_id']}", "lastfm", record["lastfm"]
            )
        if record["steam"]:
            await redis.hset(f"accounts:{record['user_id']}", "steam", record["steam"])
        if record["roblox"]:
            await redis.hset(
                f"accounts:{record['user_id']}", "roblox", record["roblox"]
            )


async def setup_prefixes(bot: Bot, pool: asyncpg.Pool):
    prefixes = await pool.fetch("SELECT * FROM guild_prefixes")
    for record in prefixes:
        add_prefix(bot, record["guild_id"], record["prefix"])
