from __future__ import annotations

import asyncio
import datetime
import hashlib
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    format_millis,
    lastfm_command,
    lfm_emoji,
    plural,
    to_image,
)
from utils.credentials import decrypt_credential

from .charts import Charts
from .top import Top

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Lastfm(Top, Charts):
    """Last.fm integration"""

    emoji = lfm_emoji

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    async def _lastfm_account(self, user_id: int) -> tuple[str, str]:
        row = await self.bot.pool.fetchrow(
            "SELECT lastfm, lastfm_session_key FROM accounts WHERE user_id = $1",
            user_id,
        )
        username = row["lastfm"] if row else None
        session_key = decrypt_credential(row["lastfm_session_key"]) if row else None
        if not username:
            raise commands.BadArgument(
                "Connect your Last.fm account first with `fish link lastfm`."
            )
        if not session_key:
            raise commands.BadArgument(
                "Reconnect your Last.fm account to enable loving tracks."
            )
        return str(username), str(session_key)

    async def _lastfm_current_track(self, username: str) -> tuple[str, str]:
        response = await self.bot.lfm_get(
            {"method": "user.getrecenttracks", "user": username, "limit": 1}
        )
        tracks = response.get("recenttracks", {}).get("track")
        if not tracks:
            raise commands.BadArgument(f"No recent tracks found for **{username}**.")
        track = tracks[0] if isinstance(tracks, list) else tracks
        artist_data = track.get("artist") if isinstance(track, dict) else None
        artist = (
            artist_data.get("#text") or artist_data.get("name")
            if isinstance(artist_data, dict)
            else artist_data
        )
        title = track.get("name") if isinstance(track, dict) else None
        if not artist or not title:
            raise commands.BadArgument("Last.fm did not provide a usable track.")
        return str(artist), str(title)

    @staticmethod
    def _lastfm_signature(params: dict[str, str], secret: str) -> str:
        signature = "".join(
            f"{key}{params[key]}" for key in sorted(params) if key != "format"
        )
        return hashlib.md5(f"{signature}{secret}".encode()).hexdigest()

    async def _set_loved(
        self, username: str, session_key: str, loved: bool
    ) -> tuple[str, str]:
        artist, title = await self._lastfm_current_track(username)
        params = {
            "api_key": self.bot.config["keys"]["lastfm_cb"],
            "artist": artist,
            "method": "track.love" if loved else "track.unlove",
            "sk": session_key,
            "track": title,
        }
        params["api_sig"] = self._lastfm_signature(
            params, self.bot.config["keys"]["lastfm_cb_secret"]
        )
        params["format"] = "json"
        async with self.bot.session.post(LASTFM_API_URL, data=params) as response:
            try:
                result = await response.json(content_type=None)
            except (ValueError, TypeError):
                result = None
        if (
            response.status != 200
            or not isinstance(result, dict)
            or result.get("error")
        ):
            raise commands.BadArgument(
                f"Last.fm could not {'love' if loved else 'unlove'} the current track."
            )
        return artist, title

    async def _toggle_love(self, ctx: Context, loved: bool) -> None:
        username, session_key = await self._lastfm_account(ctx.author.id)
        artist, title = await self._set_loved(username, session_key, loved)
        verb = "Loved" if loved else "Unloved"
        await ctx.send(
            f"{verb} **{discord.utils.escape_markdown(title)}** by "
            f"**{discord.utils.escape_markdown(artist)}** on Last.fm.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="fm", enabled=True, aliases=("np", "nowplaying", "fuckyoutony")
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def justfmmealreadybruh(
        self, ctx: Context, user: discord.User = commands.Author
    ):
        """Get your currently playing or most recently listened to song from last.fm"""
        async with ctx.typing():
            try:
                lfm_user = self.bot.db_cache.lastfm[user.id]
            except KeyError:
                raise commands.BadArgument(
                    "This user has not connected their last.fm account"
                )

            data = {"method": "user.getrecenttracks", "user": lfm_user}

            response = await self.bot.lfm_get(data)
            tracks = response.get("recenttracks", {}).get("track")
            if not tracks:
                raise commands.BadArgument(
                    f"No recent tracks found for **{lfm_user}**."
                )
            lt = tracks[0] if isinstance(tracks, list) else tracks

            files = []
            embed = discord.Embed(color=self.bot.embedcolor)
            author_name = "was listening to" if lt.get("date") else "is listening to"

            thumbnail_url = re.sub(r"/u/.*/", "/u/", lt["image"][-1]["#text"])

            cover_task = None
            if not re.search("2a96cbd8b46e442fc41c2b86b821562f.png", thumbnail_url):
                cover_task = asyncio.create_task(to_image(ctx.session, thumbnail_url))

            embed.set_author(
                name=f"{user.display_name} {author_name}"[:256],
                icon_url=user.display_avatar.url,
                url=f"https://last.fm/user/{lfm_user}",
            )

            embed.url = lt["url"]
            embed.description = (
                f"**{lt['artist']['#text']}** - *{lt['album']['#text']}*"
            )

            tData = (
                {"method": "track.getInfo", "mbid": lt["mbid"], "user": lfm_user}
                if lt.get("mbid")
                else {
                    "method": "track.getInfo",
                    "artist": lt["artist"]["#text"],
                    "track": lt["name"],
                    "user": lfm_user,
                }
            )

            async def _get_track_info():
                try:
                    t_response = await self.bot.lfm_get(tData)
                except Exception:
                    return None
                return t_response.get("track") if isinstance(t_response, dict) else None

            track_task = asyncio.create_task(_get_track_info())
            if cover_task is not None:
                fp, t = await asyncio.gather(cover_task, track_task)
                file = discord.File(fp=fp, filename="cover.png")
                files.append(file)
                embed.set_thumbnail(url="attachment://cover.png")
            else:
                t = await track_task

            footer_text = ""
            if t:
                tp = int(t.get("userplaycount", 0))
                dur = format_millis(int(t.get("duration", 0)))
                if tp != 0:
                    splitter = " - " if dur != "0" else ""
                    footer_text += f"{tp:,} track {plural(tp, False):play} {splitter}"
                if dur != "0":
                    footer_text += f"\U0001f551 {dur}"

            if lt.get("date"):
                embed.timestamp = datetime.datetime.fromtimestamp(
                    int(lt["date"]["uts"])
                )
                footer_text += "\nLast play"

            if footer_text:
                embed.set_footer(text=footer_text)

            loved = " \U00002764\U0000fe0f" if t and t.get("userloved") != "0" else ""
            embed.title = f'{lt["name"]}{loved}'

            await ctx.send(embed=embed, files=files)

    @commands.hybrid_command(name="love", aliases=("loved",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def love(self, ctx: Context):
        """Love the current or most recently listened to Last.fm track."""
        await self._toggle_love(ctx, True)

    @commands.hybrid_command(name="unlove", aliases=("unloved",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def unlove(self, ctx: Context):
        """Remove the current or most recently listened to Last.fm track from loved tracks."""
        await self._toggle_love(ctx, False)


async def setup(bot: Fishie):
    await bot.add_cog(Lastfm(bot))
