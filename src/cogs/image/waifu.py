import re
from typing import Dict

import aiohttp
import discord
from dateutil.parser import parse
from discord.ext import commands

from bot import Context
from utils import (
    BoolConverter,
    response_checker,
    to_bytesio,
    default_headers as headers,
)

from ._base import CogBase


class Waifus(CogBase):
    async def make_request(self, tag: str, /) -> Dict:
        url = "https://api.waifu.im/random/"

        params = {"is_nsfw": "false", "selected_tags": tag}

        async with self.bot.session.get(url, params=params, headers=headers) as r:
            response_checker(r)

            data = await r.json()

        return data["images"][0]

    async def do_command(self, ctx: Context, tag: str, details: bool = False):
        await ctx.trigger_typing()
        data = await self.make_request(tag)
        color = (
            int(re.sub("#", "0x", data["dominant_color"]), 0)
            if data.get("dominant_color")
            else self.bot.embedcolor
        )

        embed = discord.Embed(
            color=color, timestamp=parse(data["uploaded_at"]) if details else None
        )
        image_name = f"{data['image_id']}{data['extension']}"
        image_file = discord.File(
            fp=await to_bytesio(ctx.session, data["url"]), filename=image_name
        )
        embed.set_image(url=f"attachment://{image_name}")

        if details:
            embed.set_author(
                name="Source",
                url=data["source"] if data.get("source") else None,
            )
            embed.set_footer(text=f"\U00002764  {int(data['favourites']):,}\nUploaded ")

        await ctx.send(file=image_file, embed=embed)

    @commands.command(name="maid")
    async def maid(
        self,
        ctx: Context,
        details: bool = commands.parameter(
            converter=BoolConverter, default=False, displayed_default="[bool=False]"
        ),
    ):
        await self.do_command(ctx, ctx.command.name, details)

    @commands.command(name="waifu")
    async def waifu(
        self,
        ctx: Context,
        details: bool = commands.parameter(
            converter=BoolConverter, default=False, displayed_default="[bool=False]"
        ),
    ):
        await self.do_command(ctx, ctx.command.name, details)

    @commands.command(name="uniform")
    async def uniform(
        self,
        ctx: Context,
        details: bool = commands.parameter(
            converter=BoolConverter, default=False, displayed_default="[bool=False]"
        ),
    ):
        await self.do_command(ctx, ctx.command.name, details)

    @commands.command(name="raiden-shogun", aliases=("raiden",))
    async def raiden(
        self,
        ctx: Context,
        details: bool = commands.parameter(
            converter=BoolConverter, default=False, displayed_default="[bool=False]"
        ),
    ):
        await self.do_command(ctx, ctx.command.name, details)

    @commands.command(name="selfies")
    async def selfies(
        self,
        ctx: Context,
        details: bool = commands.parameter(
            converter=BoolConverter, default=False, displayed_default="[bool=False]"
        ),
    ):
        await self.do_command(ctx, ctx.command.name, details)
