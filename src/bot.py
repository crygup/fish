from __future__ import annotations

import datetime
import logging
import os
import pathlib
import re
import sys
import textwrap
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Set
from unittest import result

import aiohttp
import aioredis
import asyncpg
import discord
from cachetools import TTLCache
from discord.ext import commands
from ossapi import OssapiV2

from cogs.context import Context
from utils import (
    setup_accounts,
    setup_cache,
    setup_pokemon,
    setup_prefixes,
    setup_webhooks,
)

if TYPE_CHECKING:
    from utils import Context

initial_extensions = [
    "jishaku",
    "cogs.owner",
    "cogs.context",
    "cogs.events.errors",
    "cogs.help",
]
cogs_path = pathlib.Path("./src/cogs")


def fix_cog(results) -> str:
    results = re.sub(r"[/]", ".", results)
    results = re.sub(r"(src(/|.)|[.]py$)", "", results)

    return results


module_extensions = ["examples", "discord_", "tools", "image"]
cogs = [
    fix_cog(x.as_posix())
    for x in cogs_path.glob("**/*.py")
    if x.parent.name not in module_extensions
]
cogs.extend([f"cogs.{x}" for x in module_extensions])

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
    exts: Set[str]

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
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            case_insensitive=False,
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
        self.e_reply = "<:reply:972280355136606209>"
        self.e_replies = "<:replies:972280398874824724>"
        self._context = Context
        self.select_filler = "\u2800" * 47
        self.spotify_key: str | None = None

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

        with open("schema.sql") as f:
            await self.pool.execute(f.read())

        self.redis = await aioredis.from_url(
            self.config["databases"]["testing_redis_dns"]
            if self.testing
            else self.config["databases"]["redis_dns"],
            encoding="utf-8",
            decode_responses=True,
        )
        print("Connected to Redis database")

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

        await setup_prefixes(self)
        print("Setup prefixes")

        await setup_accounts(self)
        print("Setup accounts")

        self.exts = set(initial_extensions + cogs)

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

    async def send_error(self, ctx: Context, error: commands.CommandError):
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


if __name__ == "__main__":
    print("Hello, please run the launcher.py file!")
