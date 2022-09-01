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


async def setup_cache(bot: Bot):
    guild_settings = await bot.pool.fetch("SELECT * FROM guild_settings")
    for guild in guild_settings:
        if guild["poketwo"]:
            await bot.redis.sadd("poketwo_guilds", guild["guild_id"])
        if guild["auto_download"]:
            await bot.redis.sadd("auto_download_channels", guild["auto_download"])

        if guild["auto_reactions"]:
            await bot.redis.sadd("auto_reactions", guild["guild_id"])

    blacklisted = await bot.pool.fetch("SELECT snowflake FROM block_list")
    for snowflake in blacklisted:
        await bot.redis.sadd("block_list", snowflake["snowflake"])

    afk = await bot.pool.fetch("SELECT * FROM afk")
    for row in afk:
        await bot.redis.sadd("afk_users", row["user_id"])


async def setup_webhooks(bot: Bot):
    for name, webhook in bot.config["webhooks"].items():
        bot.webhooks[name] = discord.Webhook.from_url(url=webhook, session=bot.session)

    for name, webhook in bot.config["avatar_webhooks"].items():
        bot.avatar_webhooks[name] = discord.Webhook.from_url(
            url=webhook, session=bot.session
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


async def setup_accounts(bot: Bot):
    accounts = await bot.pool.fetch("SELECT * FROM accounts")
    for record in accounts:
        if record["osu"]:
            await bot.redis.hset(f"accounts:{record['user_id']}", "osu", record["osu"])
        if record["lastfm"]:
            await bot.redis.hset(
                f"accounts:{record['user_id']}", "lastfm", record["lastfm"]
            )
        if record["steam"]:
            await bot.redis.hset(
                f"accounts:{record['user_id']}", "steam", record["steam"]
            )
        if record["roblox"]:
            await bot.redis.hset(
                f"accounts:{record['user_id']}", "roblox", record["roblox"]
            )
        if record["genshin"]:
            await bot.redis.hset(
                f"accounts:{record['user_id']}", "genshin", record["genshin"]
            )


async def setup_prefixes(bot: Bot):
    prefixes = await bot.pool.fetch("SELECT * FROM guild_prefixes")
    for record in prefixes:
        add_prefix(bot, record["guild_id"], record["prefix"])
