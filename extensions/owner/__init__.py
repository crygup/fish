from __future__ import annotations

import os
import re
import time
from pathlib import Path
import discord
from typing import TYPE_CHECKING, List, Literal, Optional, Union
from discord.abc import Messageable
from discord.ext import commands

from core import Cog
from utils import (
    fish_owner,
    greenTick,
    AllMsgbleChannels,
    update_pokemon,
    fish_x,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Owner(Cog):
    emoji = fish_owner
    hidden: bool = True

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self._last_reload: float = time.time()

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

        ref = ctx.message.reference

        if not ref or not isinstance(ref.resolved, discord.Message):
            raise commands.BadArgument("Reply to a Pokétwo hint message to solve it.")

        try:
            found = events.auto_solve(ref.resolved.content)
        except commands.BadArgument:
            raise commands.BadArgument("Could not find a Pokémon hint in that message.")

        if not found:
            await ctx.send("No matching Pokémon found.")
            return

        for name in found:
            await events._log_solve(
                ctx.author.id, name, "command", ctx.guild.id if ctx.guild else None
            )

        await ctx.send("\n".join(found))

    @commands.command(name="banip")
    async def banip(self, ctx: Context, ip: str):
        """Ban an IP from sending messages through the website."""
        sql = "INSERT INTO banned_ips (ip) VALUES ($1) ON CONFLICT DO NOTHING"
        await self.bot.pool.execute(sql, ip)
        self.bot.cached_banned_ips.add(ip)
        await self._add_reaction(ctx, ctx.message)

    @commands.command(name="reload")
    async def reload(self, ctx: Context, *extensions: str):
        """Reload extensions. '~' reloads all. No args reloads recently modified."""

        if extensions == ("~",):
            extensions = tuple(self.bot._extensions)

        if not extensions:
            modified = []
            for ext in self.bot._extensions:
                pkg_dir = Path("extensions") / ext.split(".", 1)[1]
                if not pkg_dir.is_dir():
                    continue
                for py_file in pkg_dir.rglob("*.py"):
                    if py_file.stat().st_mtime > self._last_reload:
                        modified.append(ext)
                        break

            if not modified:
                await ctx.send("No extensions modified since last reload.")
                return

            extensions = tuple(modified)

        results = []
        for ext in extensions:
            if ext not in self.bot.extensions:
                results.append(f"\u274c `{ext}` not loaded")
                continue
            try:
                await self.bot.reload_extension(ext)
                results.append(f"\U0001f504 `{ext}`")
            except Exception as e:
                results.append(f"\u274c `{ext}` \n```{e}```")

        self._last_reload = time.time()
        e = discord.Embed(color=self.bot.embedcolor, description="\n".join(results))
        await ctx.send(embed=e)

    async def cog_check(self, ctx: commands.Context[Fishie]) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True

        raise commands.BadArgument("You are not allowed to use this command.")


async def setup(bot: Fishie):
    await bot.add_cog(Owner(bot))
