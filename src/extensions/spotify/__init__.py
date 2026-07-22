from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union
from urllib.parse import unquote, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import get_sp_cover, response_checker, spotify, to_image
from utils.credentials import decrypt_credential, encrypt_credential

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"


class Spotify(Cog):
    """Spotify integration."""

    emoji = spotify

    format_mode = {
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
        "track,album,artist": "albums",
    }

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    async def search(
        self,
        ctx: Context,
        mode: Union[
            Literal["track"], Literal["album"], Literal["artist"], Literal["all"]
        ],
        query: str,
    ) -> str:
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.bot.spotify_key}",
        }

        api_data = {"q": query, "type": mode, "limit": "10", "market": "US"}

        async with ctx.session.get(url, headers=headers, params=api_data) as resp:
            response_checker(resp)
            data: Optional[Dict[Any, Any]] = (
                (await resp.json()).get(self.format_mode[mode]).get("items")
            )

        if data == [] or data is None:
            raise commands.BadArgument("No info found for this query")

        return data[0]["external_urls"]["spotify"]

    @staticmethod
    def _spotify_link(value: str) -> tuple[str, str] | None:
        parsed = urlparse(value.strip())
        if parsed.scheme == "spotify":
            parts = parsed.path.split(":")
            if len(parts) == 2:
                kind, item_id = parts
            else:
                return None
        elif parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
            "open.spotify.com",
            "www.open.spotify.com",
            "play.spotify.com",
        }:
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            kind = next((part for part in ("track", "album") if part in parts), None)
            if kind is None:
                return None
            item_id = (
                parts[parts.index(kind) + 1]
                if parts.index(kind) + 1 < len(parts)
                else ""
            )
        else:
            return None
        if (
            kind not in {"track", "album"}
            or not item_id
            or not item_id.isascii()
            or not item_id.isalnum()
        ):
            return None
        return kind, item_id

    async def _spotify_catalog_get(
        self, token: str, path: str, *, params: dict[str, str] | None = None
    ) -> dict:
        async with self.bot.session.get(
            f"https://api.spotify.com/v1/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {}
        if response.status == 401:
            raise commands.BadArgument(
                "Spotify authorization expired. Please reconnect your account."
            )
        if response.status == 403:
            raise commands.BadArgument("Spotify denied access to that item.")
        if response.status == 429:
            raise commands.BadArgument(
                "Spotify is rate-limiting requests. Try again shortly."
            )
        if response.status != 200 or not isinstance(data, dict):
            raise commands.BadArgument("Spotify could not find that song or album.")
        return data

    async def _spotify_album(self, token: str, album_id: str) -> dict:
        album = await self._spotify_catalog_get(token, f"albums/{album_id}")
        page = await self._spotify_catalog_get(
            token,
            f"albums/{album_id}/tracks",
            params={"limit": "50", "offset": "0"},
        )
        items = page.get("items", []) if isinstance(page.get("items"), list) else []
        total = page.get("total", len(items))
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = len(items)
        while len(items) < total and page.get("items"):
            page = await self._spotify_catalog_get(
                token,
                f"albums/{album_id}/tracks",
                params={"limit": "50", "offset": str(len(items))},
            )
            next_items = page.get("items", [])
            if not isinstance(next_items, list) or not next_items:
                break
            items.extend(next_items)
        album["tracks"] = {"items": items}
        return album

    @staticmethod
    def _album_tracks(album: dict) -> list[dict]:
        tracks = album.get("tracks", {}).get("items", [])
        if not isinstance(tracks, list):
            return []
        return [
            track
            for track in tracks
            if isinstance(track, dict)
            and track.get("uri")
            and track.get("is_playable", True)
        ]

    async def _resolve_queue_items(
        self, token: str, query: str
    ) -> tuple[str, str, list[dict]]:
        value = query.strip()
        linked = self._spotify_link(value)
        if linked:
            kind, item_id = linked
            if kind == "album":
                data = await self._spotify_album(token, item_id)
                tracks = self._album_tracks(data)
                if not tracks:
                    raise commands.BadArgument("That album has no playable tracks.")
                return kind, str(data.get("name") or "that album"), tracks
            data = await self._spotify_catalog_get(token, f"{kind}s/{item_id}")
            if not data.get("uri") or not data.get("is_playable", True):
                raise commands.BadArgument("That track is not playable on Spotify.")
            return kind, str(data.get("name") or "that track"), [data]

        requested_kind: str | None = None
        for prefix in ("track:", "album:"):
            if value.casefold().startswith(prefix):
                requested_kind = prefix[:-1]
                value = value[len(prefix) :].strip()
                break
        if not value:
            raise commands.BadArgument(
                "Provide a song or album name, URL, or Spotify URI."
            )

        search = await self._spotify_catalog_get(
            token,
            "search",
            params={"q": value, "type": "track,album", "limit": "5"},
        )
        tracks_data = search.get("tracks", {})
        albums_data = search.get("albums", {})
        tracks = tracks_data.get("items", []) if isinstance(tracks_data, dict) else []
        albums = albums_data.get("items", []) if isinstance(albums_data, dict) else []
        tracks = [
            track for track in tracks if isinstance(track, dict) and track.get("uri")
        ]
        albums = [
            album for album in albums if isinstance(album, dict) and album.get("id")
        ]
        if requested_kind == "track" and tracks:
            track = tracks[0]
            return "track", str(track.get("name") or "that track"), [track]
        if requested_kind == "album" and albums:
            album = await self._spotify_album(token, albums[0]["id"])
            album_tracks = self._album_tracks(album)
            if not album_tracks:
                raise commands.BadArgument("That album has no playable tracks.")
            return "album", str(album.get("name") or "that album"), album_tracks

        normalized = " ".join(value.casefold().split())
        exact_track = next(
            (
                track
                for track in tracks
                if " ".join(str(track.get("name", "")).casefold().split()) == normalized
            ),
            None,
        )
        if exact_track:
            return "track", str(exact_track.get("name") or "that track"), [exact_track]
        exact_album = next(
            (
                album
                for album in albums
                if " ".join(str(album.get("name", "")).casefold().split()) == normalized
            ),
            None,
        )
        if exact_album:
            album = await self._spotify_album(token, exact_album["id"])
            album_tracks = self._album_tracks(album)
            if not album_tracks:
                raise commands.BadArgument("That album has no playable tracks.")
            return "album", str(album.get("name") or "that album"), album_tracks
        if tracks:
            track = tracks[0]
            return "track", str(track.get("name") or "that track"), [track]
        if albums:
            album = await self._spotify_album(token, albums[0]["id"])
            album_tracks = self._album_tracks(album)
            if not album_tracks:
                raise commands.BadArgument("That album has no playable tracks.")
            return "album", str(album.get("name") or "that album"), album_tracks
        raise commands.BadArgument("No Spotify song or album matched that query.")

    async def _queue_uri(self, token: str, uri: str) -> None:
        async with self.bot.session.post(
            "https://api.spotify.com/v1/me/player/queue",
            headers={"Authorization": f"Bearer {token}"},
            params={"uri": uri},
        ) as response:
            status = response.status
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {}
        if 200 <= status < 300:
            return
        if status == 401:
            raise commands.BadArgument(
                "Spotify authorization expired. Please reconnect your account."
            )
        if status == 403:
            raise commands.BadArgument(
                "Spotify requires Premium and playback permission to queue tracks."
            )
        if status == 404:
            raise commands.BadArgument("You have no active Spotify device.")
        if status == 429:
            raise commands.BadArgument(
                "Spotify is rate-limiting requests. Try again shortly."
            )
        reason = (
            data.get("error", {}).get("message") if isinstance(data, dict) else None
        )
        raise commands.BadArgument(reason or "Spotify could not queue that track.")

    async def _user_access_token(self, user_id: int) -> str:
        row = await self.bot.pool.fetchrow(
            "SELECT spotify_refresh_token FROM accounts WHERE user_id = $1", user_id
        )
        refresh_token = decrypt_credential(row["spotify_refresh_token"]) if row else None
        if not refresh_token:
            raise commands.BadArgument(
                "Connect your Spotify account first with `fish refresh spotify login`."
            )

        auth = aiohttp.BasicAuth(
            self.bot.config["keys"]["spotify_id"],
            self.bot.config["keys"]["spotify_secret"],
        )
        async with self.bot.session.post(
            SPOTIFY_TOKEN_URL,
            auth=auth,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {}
        if response.status != 200 or not data.get("access_token"):
            raise commands.BadArgument(
                "Spotify authorization expired. Please reconnect your account."
            )

        if new_refresh_token := data.get("refresh_token"):
            await self.bot.pool.execute(
                "UPDATE accounts SET spotify_refresh_token = $2 WHERE user_id = $1",
                user_id,
                encrypt_credential(new_refresh_token),
            )
        return data["access_token"]

    async def _current_playback(
        self, token: str, *, require_playing: bool = False
    ) -> tuple[dict, dict[str, str]]:
        headers = {"Authorization": f"Bearer {token}"}
        async with self.bot.session.get(
            "https://api.spotify.com/v1/me/player", headers=headers
        ) as response:
            if response.status == 204:
                raise commands.BadArgument("You have no current Spotify playback.")
            try:
                playback = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                playback = {}
        if response.status == 401:
            raise commands.BadArgument(
                "Spotify authorization expired. Please reconnect your account."
            )
        if (
            response.status != 200
            or not isinstance(playback, dict)
            or not playback.get("item")
        ):
            raise commands.BadArgument("You have no current Spotify playback.")
        if require_playing and not playback.get("is_playing"):
            raise commands.BadArgument("You are not actively listening on Spotify.")
        return playback, headers

    async def _wait_for_track_change(
        self, token: str, previous_track_id: str | None
    ) -> dict:
        for attempt in range(5):
            playback, _ = await self._current_playback(token)
            track = playback["item"]
            track_id = track.get("id") if isinstance(track, dict) else None
            if not previous_track_id or track_id != previous_track_id:
                return playback
            if attempt < 4:
                await asyncio.sleep(0.5)
        raise commands.BadArgument("Spotify did not return the updated playback.")

    @staticmethod
    def _track_label(track: dict) -> str:
        title = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(track.get("name") or "the current song"))
        )
        artists = track.get("artists") or []
        artist_names = ", ".join(
            discord.utils.escape_mentions(
                discord.utils.escape_markdown(str(artist.get("name")))
            )
            for artist in artists
            if isinstance(artist, dict) and artist.get("name")
        )
        return f"**{title}** by **{artist_names}**" if artist_names else f"**{title}**"

    async def _player_command(
        self,
        ctx: Context,
        token: str,
        endpoint: str,
        success_message: str,
        *,
        params: Optional[dict[str, str | int]] = None,
    ) -> None:
        status = await self._player_request(token, endpoint, params=params)
        if 200 <= status < 300:
            await ctx.send(success_message)
            return
        self._raise_player_error(status, endpoint)

    async def _player_request(
        self,
        token: str,
        endpoint: str,
        *,
        params: Optional[dict[str, str | int]] = None,
    ) -> int:
        headers = {"Authorization": f"Bearer {token}"}
        async with self.bot.session.put(
            f"https://api.spotify.com/v1/me/player/{endpoint}",
            headers=headers,
            params=params,
        ) as response:
            status = response.status
            try:
                await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                pass
        return status

    @staticmethod
    def _raise_player_error(status: int, endpoint: str) -> None:
        if status == 403:
            raise commands.BadArgument(
                "Spotify denied playback control. Please reconnect Spotify and grant playback permission."
            )
        if status == 404:
            raise commands.BadArgument("You have no active Spotify device.")
        raise commands.BadArgument(f"Spotify could not {endpoint.replace('_', ' ')}.")

    @staticmethod
    def _shuffle_state(value: Optional[str], current: bool) -> bool:
        if value is None:
            return not current
        normalized = value.casefold().strip()
        if normalized in {"on", "enable", "enabled", "true", "yes"}:
            return True
        if normalized in {"off", "disable", "disabled", "false", "no"}:
            return False
        raise commands.BadArgument(
            "Shuffle state must be `on`, `off`, or omitted to toggle it."
        )

    @staticmethod
    def _repeat_mode(value: Optional[str], current: str) -> str:
        if value is None:
            return "off" if current != "off" else "context"
        normalized = value.casefold().strip()
        modes = {
            "off": "off",
            "disable": "off",
            "disabled": "off",
            "none": "off",
            "on": "context",
            "enable": "context",
            "enabled": "context",
            "context": "context",
            "playlist": "context",
            "track": "track",
            "song": "track",
            "one": "track",
        }
        if normalized not in modes:
            raise commands.BadArgument(
                "Repeat mode must be `off`, `context`, or `track`."
            )
        return modes[normalized]

    async def _is_liked(self, token: str, track_id: str) -> bool:
        async with self.bot.session.get(
            "https://api.spotify.com/v1/me/library/contains",
            headers={"Authorization": f"Bearer {token}"},
            params={"uris": f"spotify:track:{track_id}"},
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = []
        if response.status == 401:
            raise commands.BadArgument(
                "Spotify authorization expired. Please reconnect your account."
            )
        if response.status != 200 or not isinstance(data, list):
            return False
        return bool(data and data[0])

    async def _lastfm_track_info(self, username: str, artist: str, title: str) -> dict:
        response = await self.bot.lfm_get(
            {
                "method": "track.getInfo",
                "artist": artist,
                "track": title,
                "username": username,
                "autocorrect": 1,
            }
        )
        track_response = response if isinstance(response, dict) else {}
        track_data = track_response.get("track")
        if isinstance(track_data, dict) and "userplaycount" in track_data:
            return track_response

        search = await self.bot.lfm_get(
            {
                "method": "track.search",
                "artist": artist,
                "track": title,
                "limit": 5,
            }
        )
        matches = search.get("results", {}).get("trackmatches", {}).get("track", [])
        if isinstance(matches, dict):
            matches = [matches]
        for match in matches if isinstance(matches, list) else []:
            if (
                not isinstance(match, dict)
                or not match.get("name")
                or not match.get("artist")
            ):
                continue
            try:
                candidate = await self.bot.lfm_get(
                    {
                        "method": "track.getInfo",
                        "artist": match["artist"],
                        "track": match["name"],
                        "username": username,
                        "autocorrect": 1,
                    }
                )
            except Exception:
                continue
            candidate_track = (
                candidate.get("track") if isinstance(candidate, dict) else None
            )
            if isinstance(candidate_track, dict) and "userplaycount" in candidate_track:
                return candidate
        return track_response

    async def _lastfm_footer(self, user_id: int, track: dict) -> str | None:
        username = self.bot.db_cache.lastfm.get(user_id)
        if not username:
            username = await self.bot.pool.fetchval(
                "SELECT lastfm FROM accounts WHERE user_id = $1", user_id
            )
        if not username:
            return None
        artists = track.get("artists") or []
        artist = (
            artists[0].get("name") if artists and isinstance(artists[0], dict) else None
        )
        title = track.get("name")
        if not artist or not title:
            return None
        try:
            track_response = await self._lastfm_track_info(username, artist, title)
        except Exception:
            track_response = {}
        track_data = (
            track_response.get("track", {}) if isinstance(track_response, dict) else {}
        )
        canonical_artist = (
            track_data.get("artist", {}).get("name")
            if isinstance(track_data, dict)
            and isinstance(track_data.get("artist"), dict)
            else artist
        )
        artist_response, user_response = await asyncio.gather(
            self.bot.lfm_get(
                {
                    "method": "artist.getInfo",
                    "artist": canonical_artist or artist,
                    "username": username,
                }
            ),
            self.bot.lfm_get({"method": "user.getInfo", "user": username}),
            return_exceptions=True,
        )
        if isinstance(artist_response, Exception):
            artist_response = {}
        if isinstance(user_response, Exception):
            user_response = {}

        def count(value: object) -> int:
            try:
                return int(str(value)) if value is not None else 0
            except (TypeError, ValueError):
                return 0

        track_info = (
            track_response.get("track") if isinstance(track_response, dict) else None
        )
        artist_info = (
            artist_response.get("artist") if isinstance(artist_response, dict) else None
        )
        artist_stats = (
            artist_info.get("stats") if isinstance(artist_info, dict) else None
        )
        user_info = (
            user_response.get("user") if isinstance(user_response, dict) else None
        )
        track_plays = count(
            track_info.get("userplaycount", 0) if isinstance(track_info, dict) else 0
        )
        artist_plays = count(
            artist_stats.get("userplaycount", 0)
            if isinstance(artist_stats, dict)
            else 0
        )
        total_plays = count(
            user_info.get("playcount", 0) if isinstance(user_info, dict) else 0
        )
        return (
            f"Track plays: {track_plays:,} • Artist plays: {artist_plays:,} "
            f"• Total plays: {total_plays:,}"
        )

    def _player_embed(
        self, playback: dict, *, lastfm_footer: str | None = None
    ) -> discord.Embed:
        track = playback["item"]
        title = str(track.get("name") or "Unknown song")
        artists = ", ".join(
            str(artist.get("name"))
            for artist in track.get("artists", [])
            if isinstance(artist, dict) and artist.get("name")
        )
        embed = discord.Embed(
            title=title,
            description=artists or "Unknown artist",
            url=track.get("external_urls", {}).get("spotify"),
            color=self.bot.embedcolor,
        )
        images = track.get("album", {}).get("images", [])
        if images and images[0].get("url"):
            embed.set_image(url=images[0]["url"])
        if lastfm_footer:
            embed.set_footer(text=lastfm_footer)
        return embed

    @commands.hybrid_command(name="spotify", aliases=("sp", "s", "song", "track"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the track")
    async def spotify(self, ctx: Context, *, query: str):
        """Search for a track on Spotify."""

        await ctx.typing()

        await ctx.send(await self.search(ctx=ctx, mode="track", query=query))

    @commands.hybrid_command(name="album", aliases=("ab",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the album")
    async def album(self, ctx: Context, *, query: str):
        """Search for an album on Spotify."""

        await ctx.send(await self.search(ctx=ctx, mode="album", query=query))

    @commands.hybrid_command(name="artist", aliases=("art",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the artist")
    async def artist(self, ctx: Context, *, query: str):
        """Search for an artist on Spotify."""

        await ctx.send(await self.search(ctx=ctx, mode="artist", query=query))

    @commands.hybrid_command(name="cover", aliases=("co",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the album")
    async def cover(self, ctx: Context, *, query: str):
        """Get the cover for an album on Spotify."""

        await ctx.typing()

        url, nsfw = await get_sp_cover(self.bot, query)
        fp = await to_image(ctx.session, url)
        await ctx.send(file=discord.File(fp=fp, filename="cover.png", spoiler=nsfw))

    async def queue(self, ctx: Context, *, query: str):
        """Add a Spotify song or album to your playback queue."""

        token = await self._user_access_token(ctx.author.id)
        async with ctx.typing():
            kind, name, tracks = await self._resolve_queue_items(token, query)
            queued = 0
            try:
                for track in tracks:
                    await self._queue_uri(token, track["uri"])
                    queued += 1
            except commands.BadArgument as error:
                if queued:
                    safe_name = discord.utils.escape_mentions(name)
                    raise commands.BadArgument(
                        f"Queued {queued} of {len(tracks)} tracks from **{safe_name}**, then stopped: {error}"
                    )
                raise

        if kind == "album":
            safe_name = discord.utils.escape_mentions(
                discord.utils.escape_markdown(name)
            )
            await ctx.send(
                f"Queued **{safe_name}** ({queued} tracks) on Spotify.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"Queued {self._track_label(tracks[0])} on Spotify.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def shuffle(self, ctx: Context, state: Optional[str] = None):
        """Enable, disable, or toggle Spotify shuffle."""

        token = await self._user_access_token(ctx.author.id)
        playback, _ = await self._current_playback(token)
        shuffle_state = self._shuffle_state(state, bool(playback.get("shuffle_state")))
        await self._player_command(
            ctx,
            token,
            "shuffle",
            f"Shuffle {'enabled' if shuffle_state else 'disabled'} on Spotify.",
            params={"state": str(shuffle_state).lower()},
        )

    async def repeat(self, ctx: Context, mode: Optional[str] = None):
        """Set, disable, or toggle Spotify repeat mode."""

        token = await self._user_access_token(ctx.author.id)
        playback, _ = await self._current_playback(token)
        repeat_mode = self._repeat_mode(
            mode, str(playback.get("repeat_state") or "off")
        )
        await self._player_command(
            ctx,
            token,
            "repeat",
            f"Repeat mode set to **{repeat_mode}** on Spotify.",
            params={"state": repeat_mode},
        )

    async def player(self, ctx: Context):
        """Show the current Spotify song and playback controls."""

        token = await self._user_access_token(ctx.author.id)
        playback, _ = await self._current_playback(token)
        track = playback["item"]
        track_id = track.get("id") if isinstance(track, dict) else None
        liked = await self._is_liked(token, track_id) if track_id else False
        lastfm_footer = await self._lastfm_footer(ctx.author.id, track)
        await ctx.send(
            embed=self._player_embed(playback, lastfm_footer=lastfm_footer),
            view=PlayerView(self, ctx, playback, liked=liked),
        )

    async def skip(self, ctx: Context):
        """Skip the current song on your active Spotify device."""

        token = await self._user_access_token(ctx.author.id)
        playback, headers = await self._current_playback(token, require_playing=True)
        track_label = self._track_label(playback["item"])

        async with self.bot.session.post(
            "https://api.spotify.com/v1/me/player/next",
            headers=headers,
        ) as response:
            if 200 <= response.status < 300:
                await ctx.send(f"Skipped {track_label} on Spotify.")
                return
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {}
        reason = data.get("error", {}).get("reason") if isinstance(data, dict) else None
        if response.status == 404 and reason == "NO_ACTIVE_DEVICE":
            raise commands.BadArgument("You are not actively listening on Spotify.")
        if response.status == 403:
            raise commands.BadArgument(
                "Spotify denied playback control. Please reconnect Spotify and grant playback permission."
            )
        raise commands.BadArgument("Spotify could not skip the current song.")

    async def pause(self, ctx: Context):
        """Pause your current Spotify playback."""

        token = await self._user_access_token(ctx.author.id)
        await self._current_playback(token)
        await self._player_command(ctx, token, "pause", "Paused your Spotify playback.")

    async def play(self, ctx: Context):
        """Resume your current Spotify playback."""

        token = await self._user_access_token(ctx.author.id)
        await self._current_playback(token)
        await self._player_command(ctx, token, "play", "Resumed your Spotify playback.")

    async def restart(self, ctx: Context):
        """Restart the current Spotify song."""

        token = await self._user_access_token(ctx.author.id)
        await self._current_playback(token)
        await self._player_command(
            ctx,
            token,
            "seek",
            "Restarted the current Spotify song.",
            params={"position_ms": 0},
        )

    async def rewind(self, ctx: Context):
        """Go back to the previous Spotify song."""

        token = await self._user_access_token(ctx.author.id)
        playback, headers = await self._current_playback(token)
        current_track = playback["item"]
        previous_track_id = (
            current_track.get("id") if isinstance(current_track, dict) else None
        )
        async with self.bot.session.post(
            "https://api.spotify.com/v1/me/player/previous", headers=headers
        ) as response:
            if not 200 <= response.status < 300:
                self._raise_player_error(response.status, "previous")
        playback = await self._wait_for_track_change(token, previous_track_id)
        await ctx.send(
            f"Went back to **{self._track_label(playback['item'])}** on Spotify."
        )

    async def like(self, ctx: Context):
        """Save the current Spotify song to your Liked Songs."""

        token = await self._user_access_token(ctx.author.id)
        playback, headers = await self._current_playback(token)
        track = playback["item"]
        track_id = track.get("id") if isinstance(track, dict) else None
        if not track_id:
            raise commands.BadArgument("Spotify did not provide a current track.")
        track_label = self._track_label(track)
        async with self.bot.session.put(
            "https://api.spotify.com/v1/me/library",
            headers=headers,
            params={"uris": f"spotify:track:{track_id}"},
        ) as response:
            if 200 <= response.status < 300:
                await ctx.send(f"Liked {track_label} on Spotify.")
                return
            if response.status == 403:
                raise commands.BadArgument(
                    "Spotify denied library access. Please reconnect Spotify and grant library permission."
                )
        raise commands.BadArgument("Spotify could not like the current song.")

    async def unlike(self, ctx: Context):
        """Remove the current Spotify song from your Liked Songs."""

        token = await self._user_access_token(ctx.author.id)
        playback, headers = await self._current_playback(token)
        track = playback["item"]
        track_id = track.get("id") if isinstance(track, dict) else None
        if not track_id:
            raise commands.BadArgument("Spotify did not provide a current track.")
        track_label = self._track_label(track)
        async with self.bot.session.delete(
            "https://api.spotify.com/v1/me/library",
            headers=headers,
            params={"uris": f"spotify:track:{track_id}"},
        ) as response:
            if 200 <= response.status < 300:
                await ctx.send(f"Removed {track_label} from Spotify Liked Songs.")
                return
            if response.status == 403:
                raise commands.BadArgument(
                    "Spotify denied library access. Please reconnect Spotify and grant library permission."
                )
        raise commands.BadArgument("Spotify could not unlike the current song.")


PLAY_EMOJI = "▶️"
PAUSE_EMOJI = "⏸️"
SKIP_EMOJI = "⏩"
REWIND_EMOJI = "⏪"
LIKE_EMOJI = "❤️"
UNLIKE_EMOJI = "💔"
SHUFFLE_EMOJI = "🔀"
REPEAT_EMOJI = "🔁"
REPEAT_TRACK_EMOJI = "🔂"


class PlayerView(discord.ui.View):
    def __init__(self, cog: Spotify, ctx: Context, playback: dict, *, liked: bool):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx
        self.liked = liked
        self.shuffle_state = bool(playback.get("shuffle_state"))
        self.repeat_state = str(playback.get("repeat_state") or "off")
        self.play_pause.emoji = (
            PAUSE_EMOJI if playback.get("is_playing") else PLAY_EMOJI
        )
        self.like_button.emoji = UNLIKE_EMOJI if liked else LIKE_EMOJI
        self._sync_mode_buttons()

    def _sync_mode_buttons(self) -> None:
        self.shuffle_button.style = (
            discord.ButtonStyle.success
            if self.shuffle_state
            else discord.ButtonStyle.secondary
        )
        self.repeat_button.style = (
            discord.ButtonStyle.success
            if self.repeat_state != "off"
            else discord.ButtonStyle.secondary
        )
        self.repeat_button.emoji = (
            REPEAT_TRACK_EMOJI if self.repeat_state == "track" else REPEAT_EMOJI
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the user who opened this player can control it.", ephemeral=True
        )
        return False

    async def _refresh(
        self,
        interaction: discord.Interaction,
        *,
        previous_track_id: str | None = None,
    ) -> None:
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback = await self.cog._wait_for_track_change(token, previous_track_id)
            track = playback["item"]
            track_id = track.get("id") if isinstance(track, dict) else None
            self.liked = (
                await self.cog._is_liked(token, track_id) if track_id else False
            )
            lastfm_footer = await self.cog._lastfm_footer(self.ctx.author.id, track)
            self.play_pause.emoji = (
                PAUSE_EMOJI if playback.get("is_playing") else PLAY_EMOJI
            )
            self.like_button.emoji = UNLIKE_EMOJI if self.liked else LIKE_EMOJI
            self.shuffle_state = bool(playback.get("shuffle_state"))
            self.repeat_state = str(playback.get("repeat_state") or "off")
            self._sync_mode_buttons()
            if interaction.message:
                await interaction.message.edit(
                    embed=self.cog._player_embed(playback, lastfm_footer=lastfm_footer),
                    view=self,
                )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)

    @discord.ui.button(emoji=REWIND_EMOJI, style=discord.ButtonStyle.secondary)
    async def rewind_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback, headers = await self.cog._current_playback(token)
            current_track = playback["item"]
            previous_track_id = (
                current_track.get("id") if isinstance(current_track, dict) else None
            )
            async with self.cog.bot.session.post(
                "https://api.spotify.com/v1/me/player/previous", headers=headers
            ) as response:
                if not 200 <= response.status < 300:
                    self.cog._raise_player_error(response.status, "previous")
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._refresh(interaction, previous_track_id=previous_track_id)

    @discord.ui.button(emoji=PLAY_EMOJI, style=discord.ButtonStyle.secondary)
    async def play_pause(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback, _ = await self.cog._current_playback(token)
            endpoint = "pause" if playback.get("is_playing") else "play"
            status = await self.cog._player_request(token, endpoint)
            if not 200 <= status < 300:
                self.cog._raise_player_error(status, endpoint)
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._refresh(interaction)

    @discord.ui.button(emoji=SHUFFLE_EMOJI, style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback, _ = await self.cog._current_playback(token)
            shuffle_state = not bool(playback.get("shuffle_state"))
            status = await self.cog._player_request(
                token,
                "shuffle",
                params={"state": str(shuffle_state).lower()},
            )
            if not 200 <= status < 300:
                self.cog._raise_player_error(status, "shuffle")
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._refresh(interaction)

    @discord.ui.button(emoji=REPEAT_EMOJI, style=discord.ButtonStyle.secondary, row=1)
    async def repeat_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback, _ = await self.cog._current_playback(token)
            current_mode = str(playback.get("repeat_state") or "off")
            repeat_mode = {
                "off": "context",
                "context": "track",
                "track": "off",
            }.get(current_mode, "context")
            status = await self.cog._player_request(
                token,
                "repeat",
                params={"state": repeat_mode},
            )
            if not 200 <= status < 300:
                self.cog._raise_player_error(status, "repeat")
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._refresh(interaction)

    @discord.ui.button(emoji=SKIP_EMOJI, style=discord.ButtonStyle.secondary)
    async def skip_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback, headers = await self.cog._current_playback(
                token, require_playing=True
            )
            current_track = playback["item"]
            previous_track_id = (
                current_track.get("id") if isinstance(current_track, dict) else None
            )
            async with self.cog.bot.session.post(
                "https://api.spotify.com/v1/me/player/next", headers=headers
            ) as response:
                status = response.status
            if not 200 <= status < 300:
                self.cog._raise_player_error(status, "skip")
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._refresh(interaction, previous_track_id=previous_track_id)

    @discord.ui.button(emoji=LIKE_EMOJI, style=discord.ButtonStyle.secondary)
    async def like_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            token = await self.cog._user_access_token(self.ctx.author.id)
            playback, headers = await self.cog._current_playback(token)
            track = playback["item"]
            track_id = track.get("id") if isinstance(track, dict) else None
            if not track_id:
                raise commands.BadArgument("Spotify did not provide a current track.")
            uri = f"spotify:track:{track_id}"
            if self.liked:
                async with self.cog.bot.session.delete(
                    "https://api.spotify.com/v1/me/library",
                    headers=headers,
                    params={"uris": uri},
                ) as response:
                    status = response.status
            else:
                async with self.cog.bot.session.put(
                    "https://api.spotify.com/v1/me/library",
                    headers=headers,
                    params={"uris": uri},
                ) as response:
                    status = response.status
            if not 200 <= status < 300:
                raise commands.BadArgument(
                    "Spotify denied library access. Please reconnect Spotify and grant library permission."
                )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._refresh(interaction)


async def setup(bot: Fishie):
    await bot.add_cog(Spotify(bot))
