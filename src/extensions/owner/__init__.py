from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Literal, Optional, Union, cast

import discord
from discord.abc import Messageable
from discord.ext import commands

from core import Cog
from extensions.settings import _spotify_authorization_url, _spotify_link_view
from extensions.spotify import Spotify
from utils import (
    AllMsgbleChannels,
    fish_owner,
    fish_x,
    greenTick,
    update_pokemon,
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
        self.spotify = Spotify(bot)
        self._last_reload: float = time.time()

    async def _add_reaction(
        self, ctx: Context, msg: discord.Message, check: bool = True
    ):
        try:
            await ctx.message.add_reaction(greenTick if check else fish_x)
        except discord.HTTPException:
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
        channel: Optional[
            Union[AllMsgbleChannels, discord.User]
        ] = commands.CurrentChannel,
        *,
        text: str,
    ):
        """Send a message"""
        target = cast(Messageable | discord.User, channel or ctx.channel)
        await target.send(text, allowed_mentions=discord.AllowedMentions.all())

        await self._add_reaction(ctx, ctx.message)

    @commands.group(name="pokemon", invoke_without_command=True)
    async def pokemon(self, ctx: Context):
        """Show how many Pokémon names are currently cached."""
        await ctx.send(f"There are currently {len(self.bot.pokemon):,} cached.")

    @pokemon.command(name="update")
    async def pokemon_update(self, ctx: Context):
        """Refresh the cached Pokémon name list."""
        await update_pokemon(self.bot)
        await self._add_reaction(ctx, ctx.message)

    @pokemon.command(name="add")
    async def pokemon_add(self, ctx: Context, *, name: str):
        """Add a Pokémon name to the solver cache."""
        sql = """
        INSERT INTO added_pokemon (name, created_at) VALUES ($1, $2)
        """

        try:
            await self.bot.pool.execute(sql, name.lower(), discord.utils.utcnow())
            await update_pokemon(self.bot)
            await self._add_reaction(ctx, ctx.message)
        except Exception:
            await self._add_reaction(ctx, ctx.message, check=False)

    @pokemon.command(name="solve")
    async def pokemon_solve(self, ctx: Context):
        """Solve a replied Pokétwo hint message."""
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

    @commands.command(name="botstatus")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def status(self, ctx: Context, *, text: str):
        """Updates the bot's custom status."""
        text = text.strip()
        if not text:
            raise commands.BadArgument("The custom status cannot be empty.")
        if len(text) > 128:
            raise commands.BadArgument(
                "The custom status cannot be longer than 128 characters."
            )
        if self.bot.user is None:
            raise commands.BadArgument("The bot user is not available.")

        activity = discord.CustomActivity(name=text)
        self.bot.activity = activity
        await self.bot.change_presence(activity=activity)
        await ctx.send(f"Custom status updated to: {text}")

    async def _send_spotify_login(self, ctx: Context) -> None:
        url = _spotify_authorization_url(
            self.bot,
            ctx.author.id,
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
        )
        await ctx.send(
            "Authorize Fishie on Spotify to refresh your account login.",
            view=_spotify_link_view(url),
        )

    @commands.group(name="refresh", invoke_without_command=True)
    async def refresh(self, ctx: Context):
        """Refresh an owner-only external account login."""
        await ctx.send("Use `fish refresh spotify login` to refresh Spotify.")

    @refresh.group(name="spotify", invoke_without_command=True)
    async def refresh_spotify(self, ctx: Context):
        """Open the Spotify login refresh link."""
        await self._send_spotify_login(ctx)

    @refresh_spotify.command(name="login")
    async def refresh_spotify_login(self, ctx: Context):
        """Open the Spotify login refresh link."""
        await self._send_spotify_login(ctx)

    @commands.command(name="queue")
    async def spotify_queue(self, ctx: Context, *, query: str):
        """Add a Spotify song or album to the playback queue."""
        await self.spotify.queue(ctx, query=query)

    @commands.command(name="spotifyshuffle", aliases=("spshuffle",))
    async def spotify_shuffle(self, ctx: Context, state: Optional[str] = None):
        """Enable, disable, or toggle Spotify shuffle."""
        await self.spotify.shuffle(ctx, state=state)

    @commands.command(name="repeat", aliases=("loop",))
    async def spotify_repeat(self, ctx: Context, mode: Optional[str] = None):
        """Set, disable, or toggle Spotify repeat mode."""
        await self.spotify.repeat(ctx, mode=mode)

    @commands.command(name="player")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def spotify_player(self, ctx: Context):
        """Show the current Spotify song and playback controls."""
        await self.spotify.player(ctx)

    @commands.command(name="skip")
    async def spotify_skip(self, ctx: Context):
        """Skip the current song on the active Spotify device."""
        await self.spotify.skip(ctx)

    @commands.command(name="pause", aliases=("stop",))
    async def spotify_pause(self, ctx: Context):
        """Pause the current Spotify playback."""
        await self.spotify.pause(ctx)

    @commands.command(name="play", aliases=("resume",))
    async def spotify_play(self, ctx: Context):
        """Resume the current Spotify playback."""
        await self.spotify.play(ctx)

    @commands.command(name="restart")
    async def spotify_restart(self, ctx: Context):
        """Restart the current Spotify song."""
        await self.spotify.restart(ctx)

    @commands.command(name="previous", aliases=("rewind", "backwards", "back", "prev"))
    async def spotify_rewind(self, ctx: Context):
        """Go back to the previous Spotify song."""
        await self.spotify.rewind(ctx)

    @commands.command(name="like", aliases=("favourite", "favorite"))
    async def spotify_like(self, ctx: Context):
        """Save the current Spotify song to Liked Songs."""
        await self.spotify.like(ctx)

    @commands.command(name="unlike", aliases=("unfavourite", "unfavorite"))
    async def spotify_unlike(self, ctx: Context):
        """Remove the current Spotify song from Liked Songs."""
        await self.spotify.unlike(ctx)

    @commands.command(name="shutdown")
    async def shutdown(self, ctx: Context) -> None:
        """Gracefully stop Fishie so Docker can restart it."""
        message = await ctx.send("Restarting Fishie...")
        await self.bot.pool.execute(
            """
            INSERT INTO bot_restart_state (
                singleton,
                channel_id,
                message_id,
                requested_at
            )
            VALUES (TRUE, $1, $2, now())
            ON CONFLICT (singleton) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                message_id = EXCLUDED.message_id,
                requested_at = EXCLUDED.requested_at
            """,
            message.channel.id,
            message.id,
        )
        await asyncio.sleep(1)
        await self.bot.close()

    @commands.command(name="reload")
    async def reload(self, ctx: Context, *extensions: str):
        """Reload extensions. '~' reloads all. No args reloads recently modified."""

        if extensions == ("~",):
            extensions = tuple(self.bot._extensions)

        if not extensions:
            modified = []
            extension_root = Path(__file__).resolve().parents[1]
            for ext in self.bot._extensions:
                pkg_dir = extension_root / ext.split(".", 1)[1]
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
