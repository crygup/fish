from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Literal, Optional, Union

import discord
from discord.abc import Messageable
from discord.ext import commands

from core import Cog
from utils import (
    fish_owner,
    greenTick,
    AllMsgbleChannels,
    update_pokemon,
    fish_x,
    ROBLOX_ASSET_RE,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

roblox_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


class Owner(Cog):
    emoji = fish_owner
    hidden: bool = True

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    async def _add_reaction(
        self, ctx: Context, msg: discord.Message, check: bool = True
    ):
        try:
            await ctx.message.add_reaction(greenTick if check else fish_x)
        except:
            pass

    @commands.command(name="reply")
    async def reply(
        self,
        ctx: Context,
        message: Union[str, int],
        channel: Optional[Messageable] = None,
        *,
        text: str,
    ):
        """Reply to a message"""
        _message = await ctx.bot.fetch_message(message=message, channel=channel)

        await _message.reply(text)

        await self._add_reaction(ctx, ctx.message)

    @commands.command(name="message", aliases=("send", "msg", "dm"))
    async def message(
        self,
        ctx: Context,
        channel: Optional[Union[AllMsgbleChannels, discord.User]] = None,
        *,
        text: str,
    ):
        """Send a message"""
        channel = channel or ctx.channel  # type: ignore
        await channel.send(text, allowed_mentions=discord.AllowedMentions.all())  # type: ignore

        await self._add_reaction(ctx, ctx.message)

    @commands.group(name="pokemon", invoke_without_command=True)
    async def pokemon(self, ctx: Context):
        await ctx.send(f"There are currently {len(self.bot.pokemon):,} cached.")

    @pokemon.command(name="update")
    async def pokemon_update(self, ctx: Context):
        await update_pokemon(self.bot)
        await self._add_reaction(ctx, ctx.message)

    @pokemon.command(name="add")
    async def pokemon_add(self, ctx: Context, *, name: str):
        sql = """
        INSERT INTO added_pokemon (name, created_at) VALUES ($1, $2)
        """

        try:
            await self.bot.pool.execute(sql, name.lower(), discord.utils.utcnow())
            await update_pokemon(self.bot)
            await self._add_reaction(ctx, ctx.message)
        except:
            await self._add_reaction(ctx, ctx.message, check=False)

    @pokemon.command(name="solve")
    async def pokemon_solve(self, ctx: Context):
        events = self.bot.events
        if not events:
            raise commands.BadArgument(
                "Events cog is not loaded, could possibly have failed to load."
            )

        msg = ctx.message.reference

        if not msg:
            raise commands.BadArgument("Reply to a message to solve it")

        found = events.auto_solve(msg.resolved.content.lower())  # type: ignore

        if bool(found) == False:
            return await self._add_reaction(ctx, ctx.message, check=False)

        await ctx.send("\n".join(found))

    @commands.hybrid_group("roblox")
    async def roblox_group(self, ctx: Context): ...

    @roblox_group.command("asset")
    async def roblox_asset(self, ctx: Context, asset_id: str):
        try:
            aid = int(asset_id)
        except ValueError:
            result = ROBLOX_ASSET_RE.search(asset_id)

            if not result:
                raise commands.CommandError(
                    "Could not find ID in the url(?) provided, please provide a valid URL or simply provide the ID"
                )
            else:
                aid = result.group(4)

        aid = str(aid)

        async with ctx.bot.session.get(
            f"https://economy.roblox.com/v2/assets/{aid}/details", headers=roblox_headers
        ) as resp:
            if resp.status != 200:
                raise commands.CommandError(
                    "Could not find valid asset matching that ID, are you sure you copied the correct one?"
                )
            data = await resp.json()

            asset_type = data.get("AssetTypeId")
            if asset_type not in (11, 12):  # 11/12 = Shirt/Pants
                raise commands.CommandError(
                    "ID provided is not a classic shirt or pants."
                )

            async with ctx.bot.session.get(
                f"https://assetdelivery.roblox.com/v1/asset/?id={aid}",headers=roblox_headers,
                allow_redirects=True,
            ) as asset_resp:
                final_url = str(asset_resp.url)

            # Step 3: Parse the XML to extract the actual texture asset ID
            async with ctx.bot.session.get(final_url) as xml_resp:
                xml_text = await xml_resp.text()

            # The texture URL is embedded in the XML like: <url>http://www.roblox.com/asset/?id=XXXXXXX</url>
            import re

            match = re.search(r"<url>.*?id=(\d+)</url>", xml_text)
            if not match:
                raise commands.CommandError(
                    "Unable to parse texture id from link, if this persist, contact developers"
                )

            texture_id = match.group(1)

            # Step 4: Get the renderable image URL via thumbnails API
            async with ctx.bot.session.get(
                f"https://thumbnails.roblox.com/v1/assets?assetIds={texture_id}&size=420x420&format=Png", headers=roblox_headers
            ) as thumb_resp:
                thumb_data = await thumb_resp.json()

            image_url = thumb_data["data"][0]["imageUrl"]

            await ctx.send(image_url)

    async def cog_check(self, ctx: commands.Context[Fishie]) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True

        raise commands.BadArgument("You are not allowed to use this command.")


async def setup(bot: Fishie):
    await bot.add_cog(Owner(bot))
