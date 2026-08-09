from __future__ import annotations

import asyncio
import hashlib
import random
import re
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    apple_music,
    format_millis,
    lastfm_command,
    lfm_emoji,
    spotify,
    youtube,
)
from utils.credentials import decrypt_credential

from .charts import Charts, search_spotify, search_spotify_data
from .top import Top
from .topster import Topster

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_BLANK_COVER = "2a96cbd8b46e442fc41c2b86b821562f.png"

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


def _clean_lastfm_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _lastfm_image(data: dict[str, Any]) -> str | None:
    images = data.get("image")
    if not isinstance(images, list):
        return None
    for image in reversed(images):
        if not isinstance(image, dict):
            continue
        url = str(image.get("#text") or image.get("url") or "").strip()
        if url and LASTFM_BLANK_COVER not in url:
            return url
    return None


def _lastfm_stat(data: dict[str, Any], key: str) -> int:
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _lastfm_value(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        for key in keys:
            result = value.get(key)
            if result:
                return str(result)
        return ""
    return str(value or "")


def _lastfm_link(value: Any, *keys: str) -> str:
    name = _lastfm_value(value, *keys)
    if not name:
        return "Unknown"
    escaped = discord.utils.escape_markdown(name)
    url = value.get("url") if isinstance(value, dict) else None
    if not url and isinstance(value, dict):
        external_urls = value.get("external_urls")
        if isinstance(external_urls, dict):
            url = external_urls.get("spotify")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        return f"[{escaped}]({url})"
    return escaped


class LastfmInfoView(discord.ui.LayoutView):
    """Components V2 presentation shared by Last.fm item lookups."""

    def __init__(
        self,
        ctx: Context,
        title: str,
        body: str,
        details: str,
        *,
        image_url: str | None = None,
        url: str | None = None,
        footer: str | None = None,
        links: list[tuple[discord.PartialEmoji, str]] | None = None,
        title_prefix: str = "##",
        title_lead: str | None = None,
        title_suffix: str = "",
    ) -> None:
        super().__init__(timeout=120)
        safe_title = discord.utils.escape_markdown(title)
        title_display = (
            f"{title_prefix} [{safe_title}]({url}){title_suffix}"
            if url
            else f"{title_prefix} {safe_title}{title_suffix}"
        )
        title_items: list[discord.ui.Item[Any]] = []
        if title_lead:
            title_items.append(discord.ui.TextDisplay(title_lead))
        title_items.append(discord.ui.TextDisplay(title_display))
        title_items.append(discord.ui.TextDisplay(body or "No description available."))
        first: list[discord.ui.Item[Any]]
        if image_url:
            first = [
                discord.ui.Section(
                    *title_items,
                    accessory=discord.ui.Thumbnail(image_url),
                )
            ]
        else:
            first = title_items
        if details:
            first.extend([discord.ui.Separator(), discord.ui.TextDisplay(details)])
        if footer:
            first.extend(
                [discord.ui.Separator(), discord.ui.TextDisplay(f"-# {footer}")]
            )
        if links:
            buttons = [
                discord.ui.Button(
                    emoji=emoji,
                    style=discord.ButtonStyle.link,
                    url=link,
                )
                for emoji, link in links
            ]
            first.extend([discord.ui.Separator(), discord.ui.ActionRow(*buttons)])
        self.add_item(discord.ui.Container(*first, accent_color=ctx.bot.embedcolor))


class Lastfm(Top, Charts, Topster):
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

    async def _lastfm_recent_item(self, username: str) -> dict[str, Any]:
        response = await self.bot.lfm_get(
            {"method": "user.getrecenttracks", "user": username, "limit": 1}
        )
        tracks = response.get("recenttracks", {}).get("track")
        if not tracks:
            raise commands.BadArgument(f"No recent tracks found for **{username}**.")
        track = tracks[0] if isinstance(tracks, list) else tracks
        if not isinstance(track, dict):
            raise commands.BadArgument("Last.fm did not provide a usable recent track.")
        return track

    async def _lastfm_current_track(self, username: str) -> tuple[str, str]:
        track = await self._lastfm_recent_item(username)
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

    async def _lastfm_current_album(self, username: str) -> tuple[str, str]:
        track = await self._lastfm_recent_item(username)
        artist_data = track.get("artist")
        artist = (
            artist_data.get("#text") or artist_data.get("name")
            if isinstance(artist_data, dict)
            else artist_data
        )
        album_data = track.get("album")
        album = (
            album_data.get("#text") or album_data.get("title")
            if isinstance(album_data, dict)
            else album_data
        )
        if not artist or not album:
            raise commands.BadArgument(
                "Last.fm did not provide an album for your current track."
            )
        return str(artist), str(album)

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

            author_name = "was listening to" if lt.get("date") else "is listening to"
            thumbnail_url = _lastfm_image(lt)

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

            t = await _get_track_info()
            detail_lines: list[str] = []
            track_plays = _lastfm_stat(t or {}, "userplaycount")
            duration_millis = _lastfm_stat(t or {}, "duration")
            if not duration_millis:
                duration_millis = _lastfm_stat(lt, "duration")
            duration = format_millis(duration_millis) if duration_millis else "0:00"
            loved_value = t.get("userloved") if isinstance(t, dict) else None
            is_loved = str(loved_value).casefold() in {"1", "true", "yes"}

            if lt.get("date"):
                detail_lines.append(f"Last play: <t:{int(lt['date']['uts'])}:R>")

            track_name = str(lt.get("name") or "Unknown")
            track_url = str(lt.get("url") or "https://www.last.fm")
            artist_data = lt.get("artist")
            artist_name = _lastfm_value(artist_data, "#text", "name")
            if not artist_name and t:
                artist_name = _lastfm_value(t.get("artist"), "#text", "name")
            album_data = t.get("album") if t else None
            album = _lastfm_value(album_data, "title", "name", "#text")
            if not album:
                album = _lastfm_value(lt.get("album"), "title", "name", "#text")
            album_or_track = album or track_name
            status_line = (
                f"-# {discord.utils.escape_markdown(user.display_name)} "
                f"{author_name}"
            )
            view = LastfmInfoView(
                ctx,
                track_name,
                f"**{discord.utils.escape_markdown(artist_name or 'Unknown')}** · "
                f"*{discord.utils.escape_markdown(album_or_track)}*",
                "\n".join(detail_lines),
                image_url=thumbnail_url,
                url=track_url,
                footer=f"{track_plays:,} track plays · 🕑 {duration}",
                title_prefix="##",
                title_lead=status_line,
                title_suffix=" ❤️" if is_loved else "",
            )
            await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="plays", aliases=("playcount",))
    @lastfm_command()
    async def plays(self, ctx: Context, *, artist: str | None = None) -> None:
        """Show your total Last.fm plays and an artist's play count."""
        async with ctx.typing():
            lfm_user = self.bot.db_cache.lastfm[ctx.author.id]
            user_data = await self.bot.lfm_get(
                {"method": "user.getinfo", "user": lfm_user}
            )
            profile = user_data.get("user", {})
            total = _lastfm_stat(profile, "playcount")
            lines = [f"{ctx.author.display_name} has `{total:,}` plays."]

            artist_name = artist.strip() if artist and artist.strip() else None
            if artist_name:
                artist_response = await self.bot.lfm_get(
                    {
                        "method": "artist.getInfo",
                        "artist": artist_name,
                        "username": lfm_user,
                    }
                )
                artist_data = artist_response.get("artist", {})
                artist_plays = _lastfm_stat(
                    artist_data.get("stats", {}), "userplaycount"
                )
                display_artist = str(artist_data.get("name") or artist_name)
                lines.append(f"-# {artist_plays:,} {display_artist} plays.")
            else:
                top_response = await self.bot.lfm_get(
                    {
                        "method": "user.gettopartists",
                        "user": lfm_user,
                        "limit": 1,
                        "period": "overall",
                    }
                )
                top_artists = top_response.get("topartists", {}).get("artist", [])
                if isinstance(top_artists, dict):
                    top_artists = [top_artists]
                if top_artists:
                    top_artist = top_artists[0]
                    lines.append(
                        f"-# {_lastfm_stat(top_artist, 'playcount'):,} "
                        f"{top_artist.get('name', 'Top artist')} plays."
                    )
            await ctx.send(
                "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
            )

    def _linked_username(self, user: discord.User | discord.Member) -> str | None:
        return self.bot.db_cache.lastfm.get(user.id)

    async def _entity_response(
        self, mode: str, query: str, *, artist: str | None = None, username: str | None
    ) -> tuple[dict[str, Any], str]:
        name = query.strip()
        if not name:
            raise commands.BadArgument("Give me a Last.fm name to look up.")

        if mode == "track" and not artist and " - " in name:
            artist, name = name.split(" - ", 1)
        if mode == "track" and not artist:
            search = await self.bot.lfm_get(
                {"method": "track.search", "track": name, "limit": 1}
            )
            matches = search.get("results", {}).get("trackmatches", {}).get("track", [])
            if isinstance(matches, dict):
                matches = [matches]
            if not matches:
                raise commands.BadArgument(f"No Last.fm track found for **{query}**.")
            match = matches[0]
            name = str(match.get("name") or name)
            artist = str(match.get("artist") or "") or None

        if mode == "album" and not artist and " - " in name:
            artist, name = name.split(" - ", 1)
        if mode == "album" and not artist:
            search = await self.bot.lfm_get(
                {"method": "album.search", "album": name, "limit": 1}
            )
            matches = search.get("results", {}).get("albummatches", {}).get("album", [])
            if isinstance(matches, dict):
                matches = [matches]
            if not matches:
                raise commands.BadArgument(f"No Last.fm album found for **{query}**.")
            match = matches[0]
            name = str(match.get("name") or name)
            artist = str(match.get("artist") or "") or None

        params: dict[str, Any] = {"method": f"{mode}.getInfo", mode: name}
        if artist:
            params["artist"] = artist
        if username:
            params["username"] = username
        response = await self.bot.lfm_get(params)
        item = response.get(mode)
        if not isinstance(item, dict):
            raise commands.BadArgument(
                f"Last.fm did not return a {mode} for **{query}**."
            )
        return item, name

    async def _spotify_link(
        self,
        ctx: Context,
        mode: str,
        title: str,
        artist: str | None = None,
    ) -> str | None:
        token = str(getattr(self.bot, "spotify_key", "") or "")
        if not token:
            return None
        query = title
        if artist and mode in {"track", "album"}:
            query = f'{mode}:"{title}" artist:"{artist}"'
        try:
            async with ctx.session.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": query, "type": mode, "limit": 1, "market": "US"},
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        except Exception:
            return None
        items = data.get(f"{mode}s", {}).get("items", [])
        if not isinstance(items, list) or not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        url = first.get("external_urls", {}).get("spotify")
        return str(url) if isinstance(url, str) and url.startswith("https://") else None

    async def _itunes_link(
        self,
        ctx: Context,
        mode: str,
        title: str,
        artist: str | None = None,
    ) -> str | None:
        entity = {"track": "song", "album": "album", "artist": "musicArtist"}[mode]
        term = f"{artist} {title}" if artist else title
        try:
            async with ctx.session.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": entity, "limit": 1},
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        except Exception:
            return None
        results = data.get("results", [])
        if not isinstance(results, list) or not results:
            return None
        result = results[0]
        if not isinstance(result, dict):
            return None
        key = {
            "track": "trackViewUrl",
            "album": "collectionViewUrl",
            "artist": "artistLinkUrl",
        }[mode]
        url = result.get(key)
        return str(url) if isinstance(url, str) and url.startswith("https://") else None

    async def _youtube_link(
        self,
        ctx: Context,
        mode: str,
        title: str,
        artist: str | None = None,
    ) -> str | None:
        keys = self.bot.config["keys"].get("google", [])
        if not isinstance(keys, list) or not keys:
            return None

        youtube_type = {"track": "video", "album": "playlist", "artist": "channel"}[
            mode
        ]
        id_key = {"video": "videoId", "playlist": "playlistId", "channel": "channelId"}[
            youtube_type
        ]
        query = f"{artist} {title}" if artist else title
        try:
            async with ctx.session.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "q": query,
                    "key": random.choice(keys),
                    "part": "snippet",
                    "type": youtube_type,
                    "maxResults": 1,
                },
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        except Exception:
            return None

        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        identifier = item.get("id", {}).get(id_key) if isinstance(item, dict) else None
        if not isinstance(identifier, str) or not identifier:
            return None
        if youtube_type == "video":
            return f"https://www.youtube.com/watch?v={identifier}"
        if youtube_type == "playlist":
            return f"https://www.youtube.com/playlist?list={identifier}"
        return f"https://www.youtube.com/channel/{identifier}"

    async def _external_links(
        self,
        ctx: Context,
        mode: str,
        title: str,
        artist: str | None = None,
    ) -> list[tuple[discord.PartialEmoji, str]]:
        spotify_url, itunes_url, youtube_url = await asyncio.gather(
            self._spotify_link(ctx, mode, title, artist),
            self._itunes_link(ctx, mode, title, artist),
            self._youtube_link(ctx, mode, title, artist),
        )
        links: list[tuple[discord.PartialEmoji, str]] = []
        if spotify_url:
            links.append((spotify, spotify_url))
        if itunes_url:
            links.append((apple_music, itunes_url))
        if youtube_url:
            links.append((youtube, youtube_url))
        return links

    async def _resolve_cover(
        self,
        ctx: Context,
        mode: str,
        item: dict[str, Any],
        title: str,
        artist: str | None = None,
    ) -> str | None:
        image_url = _lastfm_image(item)
        if not image_url and mode == "track":
            album = item.get("album")
            if isinstance(album, dict):
                image_url = _lastfm_image(album)
        if image_url:
            return image_url
        try:
            return await search_spotify(ctx, mode, title, artist)
        except Exception:
            return None

    async def _send_entity(
        self,
        ctx: Context,
        mode: str,
        query: str,
    ) -> None:
        username = self._linked_username(ctx.author)
        item, name = await self._entity_response(mode, query, username=username)
        url = str(item.get("url") or "https://www.last.fm")
        details: list[str] = []
        body = ""
        artist_data = item.get("artist")
        artist_name = _lastfm_value(artist_data, "name", "#text")
        spotify_item: dict[str, Any] | None = None
        if mode == "track" and not _lastfm_value(
            item.get("album"), "title", "name", "#text"
        ):
            # Last.fm occasionally omits the album object from track.getInfo.
            # Reuse the existing Spotify metadata/cache lookup to fill that gap.
            try:
                spotify_item = await search_spotify_data(
                    ctx,
                    "track",
                    str(item.get("name") or name),
                    artist_name or None,
                )
            except Exception:
                spotify_item = None
        if mode == "artist":
            bio = item.get("bio", {})
            body = _clean_lastfm_text(
                bio.get("summary") if isinstance(bio, dict) else ""
            )
            stats = item.get("stats", {})
            details.extend(
                [
                    f"Listeners: **{_lastfm_stat(stats, 'listeners'):,}**",
                    f"Playcount: **{_lastfm_stat(stats, 'playcount'):,}**",
                ]
            )
            user_plays = _lastfm_stat(stats, "userplaycount")
            if username:
                details.append(f"Your plays: **{user_plays:,}**")
            tags_data = item.get("tags", {})
            tags = tags_data.get("tag", []) if isinstance(tags_data, dict) else []
            if isinstance(tags, dict):
                tags = [tags]
            tag_names = [str(tag.get("name")) for tag in tags if isinstance(tag, dict)]
            if tag_names:
                details.append("Tags: " + ", ".join(tag_names[:5]))
            title = str(item.get("name") or name)
        elif mode == "track":
            artist_text = _lastfm_link(artist_data, "name", "#text")
            album_data = item.get("album")
            album_name = _lastfm_value(album_data, "title", "name", "#text")
            if not album_name and spotify_item:
                spotify_album = spotify_item.get("album")
                if isinstance(spotify_album, dict):
                    album_data = spotify_album
                    album_name = _lastfm_value(spotify_album, "title", "name", "#text")
            body = f"By **{artist_text}**"
            if album_name:
                body += (
                    f"\nAlbum: *{_lastfm_link(album_data, 'title', 'name', '#text')}*"
                )
            wiki = item.get("wiki", {})
            description = _clean_lastfm_text(
                wiki.get("summary") if isinstance(wiki, dict) else ""
            )
            if description:
                body += f"\n\n{description}"
            details.extend(
                [
                    f"Listeners: **{_lastfm_stat(item, 'listeners'):,}**",
                    f"Playcount: **{_lastfm_stat(item, 'playcount'):,}**",
                ]
            )
            user_plays = _lastfm_stat(item, "userplaycount")
            if username:
                details.append(f"Your plays: **{user_plays:,}**")
            duration = _lastfm_stat(item, "duration")
            if duration:
                details.append(f"Duration: **{format_millis(duration)}**")
            title = str(item.get("name") or name)
        else:
            body = f"By **{_lastfm_link(artist_data, 'name', '#text')}**"
            wiki = item.get("wiki", {})
            if isinstance(wiki, dict):
                description = _clean_lastfm_text(wiki.get("summary"))
                if description:
                    body += "\n\n" + description
            details.extend(
                [
                    f"Listeners: **{_lastfm_stat(item, 'listeners'):,}**",
                    f"Playcount: **{_lastfm_stat(item, 'playcount'):,}**",
                ]
            )
            user_plays = _lastfm_stat(item, "userplaycount")
            if username:
                details.append(f"Your plays: **{user_plays:,}**")
            tracks_data = item.get("tracks", {})
            tracks = (
                tracks_data.get("track", []) if isinstance(tracks_data, dict) else []
            )
            if isinstance(tracks, dict):
                tracks = [tracks]
            if tracks:
                details.append(f"Tracks: **{len(tracks):,}**")
            title = str(item.get("name") or name)

        image_url = await self._resolve_cover(ctx, mode, item, title, artist_name)
        links = await self._external_links(
            ctx,
            mode,
            title,
            artist_name if mode in {"track", "album"} else None,
        )
        view = LastfmInfoView(
            ctx,
            title,
            body,
            "\n".join(details),
            image_url=image_url,
            url=url,
            footer=f"Last.fm{f' · {username}' if username else ''}",
            links=links,
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="track")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="Track name, or Artist - Track. Leave blank for your current track."
    )
    async def track(self, ctx: Context, *, query: str | None = None) -> None:
        """Look up a track, or show your current Last.fm track when blank."""
        async with ctx.typing():
            if not query or not query.strip():
                username = self._linked_username(ctx.author)
                if not username:
                    raise commands.BadArgument(
                        "Connect your Last.fm account first to use `track` without a query."
                    )
                artist, title = await self._lastfm_current_track(username)
                query = f"{artist} - {title}"
            await self._send_entity(ctx, "track", query)

    @commands.hybrid_command(name="artist")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Artist name. Leave blank for your current artist.")
    async def artist(self, ctx: Context, *, query: str | None = None) -> None:
        """Look up an artist, or show your current Last.fm artist when blank."""
        async with ctx.typing():
            if not query or not query.strip():
                username = self._linked_username(ctx.author)
                if not username:
                    raise commands.BadArgument(
                        "Connect your Last.fm account first to use `artist` without a query."
                    )
                query, _ = await self._lastfm_current_track(username)
            await self._send_entity(ctx, "artist", query)

    @commands.hybrid_command(name="album")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="Album name, or Artist - Album. Leave blank for your current album."
    )
    async def album(self, ctx: Context, *, query: str | None = None) -> None:
        """Look up an album, or show the album from your current track when blank."""
        async with ctx.typing():
            if not query or not query.strip():
                username = self._linked_username(ctx.author)
                if not username:
                    raise commands.BadArgument(
                        "Connect your Last.fm account first to use `album` without a query."
                    )
                artist, album = await self._lastfm_current_album(username)
                query = f"{artist} - {album}"
            await self._send_entity(ctx, "album", query)

    @commands.hybrid_command(name="love", aliases=("loved",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def love(self, ctx: Context):
        """Love the current or most recently listened to Last.fm track."""
        async with ctx.typing():
            await self._toggle_love(ctx, True)

    @commands.hybrid_command(name="unlove", aliases=("unloved",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def unlove(self, ctx: Context):
        """Remove the current or most recently listened to Last.fm track from loved tracks."""
        async with ctx.typing():
            await self._toggle_love(ctx, False)


async def setup(bot: Fishie):
    await bot.add_cog(Lastfm(bot))
