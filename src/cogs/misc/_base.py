from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Dict, List

import discord
import psutil
from discord.ext.commands import Cog
from ossapi.ossapiv2 import Beatmap, Beatmapset

from utils import BlankException, to_thread

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class CogBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        self.process = psutil.Process()

        perms = discord.Permissions(1074055232)
        self.invite_url = discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot",))  # type: ignore

    async def get_app_id_from_name(self, ctx: Context, name: str) -> int:
        records: List[Dict] = await ctx.bot.pool.fetch("SELECT * FROM steam_games")

        matching_dicts = [
            record for record in records if str(record["name"]).lower() == name.lower()
        ]

        if matching_dicts:
            return matching_dicts[0]["app_id"]

        match = difflib.get_close_matches(name, [record["name"] for record in records])

        prompt = await ctx.prompt(f"Did you mean **{match[0]}**?")

        if not prompt:
            raise BlankException(
                f"Well you can run `fish steam search game {name}` and browse for the correct one."
            )

        matching_dicts = [record for record in records if record["name"] == match[0]]

        return matching_dicts[0]["app_id"]

    @to_thread
    def to_bms(self, beatmap: Beatmap) -> Beatmapset:
        return beatmap.beatmapset()
