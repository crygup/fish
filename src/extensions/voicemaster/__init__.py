from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, cast
from urllib.parse import unquote, urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from core.handoff import is_legacy_instance
from extensions.context import Context
from extensions.media_effects.audio_effects import AudioEffect, find_audio_effect
from utils.network import refresh_discord_attachment_url, validate_public_url
from utils.paginator import build_layout_pagination_row
from utils.paths import FILES_ROOT

if TYPE_CHECKING:
    from core import Fishie


MAX_TRACK_SECONDS = 60 * 60
MAX_QUEUE_SIZE = 50
IDLE_DISCONNECT_SECONDS = 10 * 60
QUEUE_PAGE_SIZE = 10
STREAM_REFRESH_SECONDS = 300.0
EXTRACT_TIMEOUT = 45.0
PREMATURE_PLAYBACK_SECONDS = 1.0
PLAYBACK_RETRY_ATTEMPTS = 2
SPOTIFY_PLAYBACK_RETRY_ATTEMPTS = 3
DEFAULT_OUTPUT_GAIN = 0.85
MAX_VOLUME = 150.0
DIRECT_MEDIA_SUFFIXES = frozenset(
    {
        ".aac",
        ".flac",
        ".gif",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
    }
)
IMAGE_SUFFIXES = frozenset({".gif", ".jpg", ".jpeg", ".png", ".webp"})
SPOTIFY_TRACK_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?track/"
    r"(?P<id>[A-Za-z0-9]{22})(?:[/?#]|$)",
    re.IGNORECASE,
)
SPOTIFY_ALBUM_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?album/"
    r"(?P<id>[A-Za-z0-9]{22})(?:[/?#]|$)",
    re.IGNORECASE,
)
SPOTIFY_EPISODE_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?episode/"
    r"(?P<id>[A-Za-z0-9]{22})(?:[/?#]|$)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
SFX_QUERY_RE = re.compile(r"^\s*sfx\s*:\s*(?P<selector>.+?)\s*$", re.IGNORECASE)


def _clean_query(value: str) -> str:
    """Remove Discord's surrounding URL markup and trailing punctuation."""

    value = str(value or "").strip()
    value = value.rstrip(".,!?;:)")
    return value.strip("<>")


def _volume_gain(volume: float) -> float:
    """Map the 0-150 control to a quieter, perceptual PCM gain."""

    normalized = max(0.0, min(float(volume), MAX_VOLUME))
    if normalized <= 100:
        # Perceived loudness is not linear with PCM amplitude. Squaring the
        # lower range makes values such as 5 behave much closer to a true
        # 5/100 control while retaining 100 as the normal listening level.
        return DEFAULT_OUTPUT_GAIN * (normalized / 100) ** 2
    # Above 100 is intentionally gentler and capped at a 1.275 PCM gain. This
    # is enough to help quiet sources without applying an aggressive boost.
    return DEFAULT_OUTPUT_GAIN * (1 + (normalized - 100) / 100)


def _search_text(value: object) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(value or "").casefold()).split())


def _spotify_search_score(
    item: dict[str, Any],
    *,
    title: str,
    artists: tuple[str, ...],
    album: str,
    duration: float | None,
) -> float:
    """Rank a YouTube result against authoritative Spotify metadata."""

    candidate_title = _search_text(item.get("title"))
    uploader = _search_text(item.get("channel") or item.get("uploader"))
    expected_title = _search_text(title)
    expected_album = _search_text(album)
    score = 0.0
    if expected_title:
        if candidate_title == expected_title:
            score += 140
        elif expected_title in candidate_title:
            score += 110
        else:
            expected_words = set(expected_title.split())
            candidate_words = set(candidate_title.split())
            if expected_words:
                score += (
                    50 * len(expected_words & candidate_words) / len(expected_words)
                )
    for artist in artists:
        expected_artist = _search_text(artist)
        if expected_artist and (
            expected_artist in candidate_title or expected_artist in uploader
        ):
            score += 35
            break
    if expected_album and expected_album in candidate_title:
        score += 15
    if "official audio" in candidate_title:
        score += 15
    elif "official" in candidate_title:
        score += 8
    candidate_duration = item.get("duration")
    if duration and isinstance(candidate_duration, (int, float)):
        difference = abs(float(candidate_duration) - duration)
        score += max(0.0, 30.0 - difference / 2)
        if float(candidate_duration) > duration * 2:
            score -= 100
    return score


def _rank_spotify_results(
    entries: list[dict[str, Any]],
    track: VoiceTrack,
) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda item: _spotify_search_score(
            item,
            title=track.search_title,
            artists=track.search_artists,
            album=track.search_album,
            duration=track.duration,
        ),
        reverse=True,
    )


def _ffmpeg_before_options(track: VoiceTrack) -> str | None:
    if track.local_path is not None:
        return None
    options = ["-reconnect 1", "-reconnect_streamed 1", "-reconnect_delay_max 5"]
    user_agent = (
        track.http_headers.get("User-Agent", "").replace("\r", "").replace("\n", "")
    )
    if user_agent:
        options.append(f"-user_agent {shlex.quote(user_agent)}")
    allowed_headers = {"Accept", "Accept-Language", "Origin", "Referer"}
    header_lines = [
        f"{name}: {value.replace(chr(13), '').replace(chr(10), '')}"
        for name, value in track.http_headers.items()
        if name in allowed_headers and value
    ]
    if header_lines:
        header_blob = "\r\n".join(header_lines) + "\r\n"
        options.append(f"-headers {shlex.quote(header_blob)}")
    return " ".join(options)


def _is_direct_media_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and Path(parsed.path).suffix.casefold() in DIRECT_MEDIA_SUFFIXES
    )


def _is_image_url(value: str) -> bool:
    try:
        suffix = Path(urlsplit(value).path).suffix.casefold()
    except ValueError:
        return False
    return suffix in IMAGE_SUFFIXES


def _is_youtube_query(value: str) -> bool:
    lowered = value.casefold()
    return (
        "youtube.com" in lowered
        or "youtu.be" in lowered
        or lowered.startswith("ytsearch")
    )


def _copy_youtube_cookies() -> Path | None:
    """Copy mounted cookies somewhere yt-dlp is allowed to update."""

    source = FILES_ROOT / "cookies" / "youtube-cookies.txt"
    if not source.is_file():
        return None
    handle = tempfile.NamedTemporaryFile(
        prefix="fishie-youtube-cookies-",
        suffix=".txt",
        delete=False,
    )
    destination = Path(handle.name)
    try:
        with source.open("rb") as cookie_source, handle:
            shutil.copyfileobj(cookie_source, handle)
        destination.chmod(0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


class _YtDlpPipeAudio(discord.FFmpegPCMAudio):
    """Stream yt-dlp's downloader output into FFmpeg.

    YouTube currently returns some extracted CDN URLs to yt-dlp that reject
    FFmpeg's direct request with HTTP 403. yt-dlp can still read those streams
    using its own ranged downloader, so this is used as the bounded playback
    retry without waiting for the complete file to download first.
    """

    def __init__(self, source: str) -> None:
        self.playback_started = asyncio.Event()
        self._event_loop = asyncio.get_running_loop()
        self._cookie_path = _copy_youtube_cookies()
        command = [
            sys.executable,
            "-m",
            "utils.ytdlp_safe",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--quiet",
            "-f",
            "bestaudio/best",
            "--js-runtimes",
            "deno",
            "--socket-timeout",
            "15",
        ]
        if self._cookie_path is not None:
            command.extend(["--cookies", str(self._cookie_path)])
            command.extend(["--extractor-args", "youtube:player_client=web_embedded"])
        command.extend(["-o", "-", source])
        self._downloader = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        if self._downloader.stdout is None:
            self._terminate_downloader()
            raise RuntimeError("yt-dlp did not expose a media stream.")
        try:
            super().__init__(
                cast(Any, self._downloader.stdout),
                pipe=True,
                options="-vn -sn -dn -t 3600 -loglevel warning",
            )
        except Exception:
            self._terminate_downloader()
            raise

    def read(self) -> bytes:
        data = super().read()
        if data and not self.playback_started.is_set():
            self._event_loop.call_soon_threadsafe(self.playback_started.set)
        return data

    def _terminate_downloader(self) -> None:
        downloader = getattr(self, "_downloader", None)
        if downloader is not None and downloader.poll() is None:
            downloader.terminate()
            try:
                downloader.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                downloader.kill()
                downloader.wait(timeout=0.5)
        stdout = getattr(downloader, "stdout", None)
        if stdout is not None:
            stdout.close()
        cookie_path = getattr(self, "_cookie_path", None)
        if cookie_path is not None:
            cookie_path.unlink(missing_ok=True)
            self._cookie_path = None

    def cleanup(self) -> None:
        self._terminate_downloader()
        super().cleanup()


def _direct_title(value: str) -> str:
    try:
        name = unquote(Path(urlsplit(value).path).name)
    except ValueError:
        name = ""
    name = name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return name or "Audio stream"


def _public_error(error: object) -> str:
    """Keep extractor diagnostics useful without echoing URLs or huge output."""

    raw = str(error)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    detail = ""
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            detail = line
            break
        if "DownloadError:" in line:
            detail = line.split("DownloadError:", 1)[1].strip()
            break
        if re.match(r"(?:OSError|URLError|RuntimeError|ValueError):", line):
            detail = line
            break
    if not detail:
        detail = lines[-1] if lines else raw
    detail = " ".join(detail.split())
    detail = URL_RE.sub("<url>", detail)
    return detail[:400] or "The source could not be played."


@dataclass(slots=True)
class VoiceTrack:
    request: str
    lookup: str
    title: str
    requester_id: int
    duration: float | None = None
    webpage_url: str | None = None
    stream_url: str | None = None
    resolved_at: float = 0.0
    local_path: Path | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    search_title: str = ""
    search_artists: tuple[str, ...] = ()
    search_album: str = ""
    search_candidate_index: int = 0


def _format_track_duration(track: VoiceTrack) -> str:
    if track.duration is None or track.duration < 0:
        return "Unknown"
    total_seconds = int(track.duration)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


@dataclass(slots=True)
class VoicePlayer:
    guild_id: int
    queue: deque[VoiceTrack] = field(default_factory=deque)
    current: VoiceTrack | None = None
    voice: discord.VoiceClient | None = None
    volume: float = 100.0
    source: discord.PCMVolumeTransformer | None = field(default=None, repr=False)
    repeat_mode: Literal["off", "one", "queue"] = "off"
    # ``None`` means infinite repeats while in one-track mode. An integer is
    # the number of additional plays remaining after the current one.
    repeat_remaining: int | None = None
    # A repeated current track is kept outside the pending queue so looping
    # can never push the visible queue beyond its hard 50-entry limit.
    repeat_track: VoiceTrack | None = None
    worker: asyncio.Task[None] | None = field(default=None, repr=False)
    skip_current: bool = False
    skip_notice: bool = False
    # ``None`` means no transition announcement. An empty string announces a
    # normal transition; a non-empty value prefixes notices such as a skip.
    pending_notice: str | None = None
    text_channel: discord.abc.Messageable | None = None
    dj_id: int | None = None
    skip_votes: set[int] = field(default_factory=set)
    idle_disconnect_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Queue mutations and worker hand-off happen after network/audio awaits.
    # Keep the worker reference transition atomic so an enqueue racing with
    # cleanup cannot strand a track or create two consumers for one queue.
    worker_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Keep one worker alive for the lifetime of the voice session.  Enqueueing
    # wakes it instead of depending on a task finishing and being replaced at
    # exactly the right moment after a track ends or is skipped.
    queue_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class VoiceMaster(commands.Cog, name="Voice Master"):
    """Play remote audio and video soundtracks in a guild voice channel."""

    emoji = discord.PartialEmoji(name="🔊")

    def __init__(self, bot: Fishie) -> None:
        self.bot = bot
        self.players: dict[int, VoicePlayer] = {}
        self._opus_loaded = False
        self._voice_dependencies = all(
            importlib.util.find_spec(name) is not None for name in ("nacl", "davey")
        )
        try:
            if not discord.opus.is_loaded():
                discord.opus.load_opus("libopus.so.0")
            self._opus_loaded = discord.opus.is_loaded()
        except (OSError, AttributeError, discord.opus.OpusNotLoaded):
            self.bot.logger.warning(
                "libopus could not be loaded; voice playback will be unavailable",
                exc_info=True,
            )
        self._voice_available = self._opus_loaded and self._voice_dependencies

    @staticmethod
    def _voice_channel(ctx: Context) -> discord.VoiceChannel | discord.StageChannel:
        voice_state = getattr(ctx.author, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise commands.BadArgument("You need to join a voice channel first.")
        return channel

    async def _ensure_voice(
        self,
        ctx: Context,
        player: VoicePlayer,
    ) -> discord.VoiceClient:
        if not self._voice_available:
            raise commands.BadArgument(
                "Voice playback is unavailable because the voice dependencies "
                "are not installed."
            )

        channel = self._voice_channel(ctx)
        guild = ctx.guild
        if guild is None:
            raise commands.NoPrivateMessage(
                "Voice commands can only be used in servers."
            )

        me = guild.me
        if me is not None:
            permissions = channel.permissions_for(me)
            if not permissions.connect:
                raise commands.BadArgument(
                    "I need the Connect permission in that voice channel."
                )
            if not permissions.speak:
                raise commands.BadArgument(
                    "I need the Speak permission in that voice channel."
                )

        existing = guild.voice_client
        if isinstance(existing, discord.VoiceClient) and existing.is_connected():
            if existing.channel is not None and existing.channel.id != channel.id:
                raise commands.BadArgument(
                    f"I am already playing in {existing.channel.mention}."
                )
            player.voice = existing
            return existing

        try:
            player.voice = cast(
                discord.VoiceClient,
                await channel.connect(self_deaf=True),
            )
        except (discord.ClientException, discord.Forbidden, RuntimeError) as error:
            raise commands.BadArgument(
                "I could not connect to that voice channel. Check my Connect "
                "and Speak permissions."
            ) from error
        return player.voice

    def _player(self, guild_id: int) -> VoicePlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = VoicePlayer(guild_id=guild_id)
            self.players[guild_id] = player
        return player

    @staticmethod
    def _voice_member_channel(
        member: discord.Member | discord.User,
    ) -> discord.VoiceChannel | discord.StageChannel | None:
        """Return a member's current voice channel without trusting stale state."""

        return getattr(getattr(member, "voice", None), "channel", None)

    def _require_voice_member(self, member: discord.Member | discord.User) -> Any:
        """Require a member to be connected to a voice channel."""

        channel = self._voice_member_channel(member)
        if channel is None:
            raise commands.BadArgument("You need to join a voice channel first.")
        return channel

    def _require_voice_caller(self, ctx: Context) -> Any:
        """Require the command author to be connected to a voice channel.

        Voice controls are text commands and may be sent from any text channel,
        but they still belong to a voice session.  Keeping this check separate
        from the active-player check lets commands such as ``queue`` return an
        empty queue without creating a player while consistently requiring the
        caller to be in voice.
        """

        return self._require_voice_member(ctx.author)

    @staticmethod
    def _is_privileged_dj(ctx: Context) -> bool:
        """Return whether a member can control playback without being the DJ."""

        guild = ctx.guild
        author = ctx.author
        if guild is not None and guild.owner_id == author.id:
            return True
        permissions = getattr(author, "guild_permissions", None)
        return bool(
            permissions
            and (
                getattr(permissions, "administrator", False)
                or getattr(permissions, "move_members", False)
                or getattr(permissions, "manage_guild", False)
                or getattr(permissions, "ban_members", False)
                or getattr(permissions, "kick_members", False)
            )
        )

    @staticmethod
    def _is_privileged_user(
        guild: discord.Guild, user: discord.User | discord.Member
    ) -> bool:
        """Check DJ override permissions for a user outside a command context."""

        if guild.owner_id == user.id:
            return True
        member = guild.get_member(user.id)
        permissions = getattr(member or user, "guild_permissions", None)
        return bool(
            permissions
            and (
                getattr(permissions, "administrator", False)
                or getattr(permissions, "move_members", False)
                or getattr(permissions, "manage_guild", False)
                or getattr(permissions, "ban_members", False)
                or getattr(permissions, "kick_members", False)
            )
        )

    def _can_control_user(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
        player: VoicePlayer,
    ) -> bool:
        """Return whether a user can manage controls for a queue view."""

        return self._is_privileged_user(guild, user) or player.dj_id == user.id

    def _refresh_dj(self, ctx: Context, player: VoicePlayer) -> None:
        """Release the DJ slot when the current DJ leaves the bot's channel."""

        if (
            player.dj_id is None
            or player.voice is None
            or not player.voice.is_connected()
        ):
            return
        guild = ctx.guild
        if guild is None:
            return
        member = guild.get_member(player.dj_id)
        if member is None and player.voice.channel is not None:
            member = next(
                (
                    candidate
                    for candidate in getattr(player.voice.channel, "members", ())
                    if candidate.id == player.dj_id
                ),
                None,
            )
        member_channel = (
            self._voice_member_channel(member) if member is not None else None
        )
        bot_channel = player.voice.channel
        if (
            member_channel is None
            or bot_channel is None
            or member_channel.id != bot_channel.id
        ):
            player.dj_id = None
            player.skip_votes.clear()

    def _claim_dj(self, ctx: Context, player: VoicePlayer) -> None:
        """Claim an unowned DJ slot for a member in the bot's voice channel."""

        self._refresh_dj(ctx, player)
        if player.dj_id is not None:
            return
        author_channel = self._voice_member_channel(ctx.author)
        bot_channel = player.voice.channel if player.voice is not None else None
        if author_channel is not None and (
            bot_channel is None or author_channel.id == bot_channel.id
        ):
            player.dj_id = ctx.author.id

    def _can_control(self, ctx: Context, player: VoicePlayer) -> bool:
        self._claim_dj(ctx, player)
        return self._is_privileged_dj(ctx) or player.dj_id == ctx.author.id

    @staticmethod
    def _human_voice_member_ids(voice: discord.VoiceClient) -> set[int]:
        channel = voice.channel
        if channel is None:
            return set()
        return {
            member.id
            for member in getattr(channel, "members", ())
            if not getattr(member, "bot", False)
        }

    async def _disconnect_if_empty(self, guild_id: int) -> None:
        """Stop and clear a voice session once its channel has no users."""

        player = self.players.get(guild_id)
        if player is None or player.voice is None:
            return
        voice = player.voice
        worker: asyncio.Task[None] | None = None

        async with player.worker_lock:
            channel = voice.channel
            humans = {
                member.id
                for member in getattr(channel, "members", ())
                if not getattr(member, "bot", False)
            }
            if humans:
                return

            self._cancel_idle_disconnect(player)
            player.queue.clear()
            player.repeat_track = None
            player.pending_notice = None
            player.skip_votes.clear()
            player.dj_id = None
            player.skip_current = True
            player.skip_notice = False
            worker = player.worker
            if worker is not None and not worker.done():
                worker.cancel()
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            try:
                if voice.is_connected():
                    await voice.disconnect(force=True)
            except discord.DiscordException:
                self.bot.logger.debug(
                    "Could not disconnect an empty voice channel", exc_info=True
                )
            player.current = None
            player.source = None
            player.voice = None
            player.queue_event.set()
            if self.players.get(guild_id) is player:
                self.players.pop(guild_id, None)

        if worker is not None and worker is not asyncio.current_task():
            try:
                await worker
            except asyncio.CancelledError:
                pass

    @commands.Cog.listener("on_voice_state_update")
    async def voicemaster_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Leave and clear a session after the last human leaves its channel."""

        if is_legacy_instance(self.bot):
            return
        player = self.players.get(member.guild.id)
        if player is None or player.voice is None:
            return
        channel = player.voice.channel
        channel_id = getattr(channel, "id", None)
        if channel_id is None or all(
            getattr(candidate, "id", None) != channel_id
            for candidate in (before.channel, after.channel)
        ):
            return
        await self._disconnect_if_empty(member.guild.id)

    def _skip_vote_requirement(self, player: VoicePlayer) -> tuple[int, int]:
        if player.voice is None:
            return 0, 0
        members = self._human_voice_member_ids(player.voice)
        # "More than 70%" means the first integer strictly above 70 percent.
        required = max(1, math.floor(len(members) * 0.70) + 1)
        return len(player.skip_votes), required

    @staticmethod
    def _cancel_idle_disconnect(player: VoicePlayer) -> None:
        task = player.idle_disconnect_task
        if task is not None and not task.done():
            task.cancel()
        player.idle_disconnect_task = None

    def _schedule_idle_disconnect(self, player: VoicePlayer) -> None:
        if player.voice is None or not player.voice.is_connected():
            return
        if (
            player.idle_disconnect_task is not None
            and not player.idle_disconnect_task.done()
        ):
            return
        voice = player.voice

        async def disconnect_when_idle() -> None:
            try:
                await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
                # Serialize the idle check with queue hand-offs.  Without
                # this lock a new request could cancel the sleep, append a
                # track, and then still be disconnected by this task before
                # its worker starts.
                async with player.worker_lock:
                    if (
                        player.current is None
                        and not player.queue
                        and player.repeat_track is None
                        and player.voice is voice
                        and voice.is_connected()
                    ):
                        await voice.disconnect()
                        player.voice = None
                        player.dj_id = None
                        player.skip_votes.clear()
                        player.queue_event.set()
            except asyncio.CancelledError:
                raise
            except discord.DiscordException:
                self.bot.logger.debug(
                    "Could not disconnect idle voice player", exc_info=True
                )
            finally:
                if player.idle_disconnect_task is asyncio.current_task():
                    player.idle_disconnect_task = None

        player.idle_disconnect_task = asyncio.create_task(
            disconnect_when_idle(), name=f"voice-idle-{player.guild_id}"
        )

    @staticmethod
    def _start_worker_locked(player: VoicePlayer, runner: Any) -> None:
        """Start a player worker while ``player.worker_lock`` is held."""

        if player.worker is None or player.worker.done():
            player.worker = asyncio.create_task(
                runner(player),
                name=f"voice-player-{player.guild_id}",
            )

    @staticmethod
    def _needs_skip_worker_recovery(player: VoicePlayer) -> bool:
        """Return whether a skipped worker is stuck waiting for its callback."""

        voice = player.voice
        return bool(
            player.skip_current
            and player.current is not None
            and voice is not None
            and not voice.is_playing()
            and not voice.is_paused()
        )

    async def _start_worker(self, player: VoicePlayer) -> None:
        """Start exactly one worker for a player's queue."""

        async with player.worker_lock:
            self._start_worker_locked(player, self._run_player)

    def _check_voice_channel(self, ctx: Context, player: VoicePlayer) -> None:
        """Validate the caller's voice session for a voice command.

        The command may be sent from any text channel in the guild, but the
        caller must be connected to voice.  Once Fishie is connected, controls
        are restricted to that same voice channel so a moderator in a separate
        voice session cannot alter playback remotely.
        """

        self._check_voice_channel_for_member(ctx.author, player)

    def _check_voice_channel_for_member(
        self,
        member: discord.Member | discord.User,
        player: VoicePlayer,
    ) -> None:
        """Require a member to be in the active voice session."""

        author_channel = self._require_voice_member(member)
        if player.voice is None or not player.voice.is_connected():
            return
        bot_channel = player.voice.channel
        if (
            author_channel is not None
            and bot_channel is not None
            and bot_channel.id != author_channel.id
        ):
            raise commands.BadArgument(
                f"I am already playing in {bot_channel.mention}."
            )

    @staticmethod
    def _message_candidates(message: discord.Message) -> list[str]:
        candidates = [
            str(attachment.url)
            for attachment in getattr(message, "attachments", ())
            if getattr(attachment, "url", None)
        ]
        content = str(getattr(message, "content", "") or "")
        candidates.extend(URL_RE.findall(content))
        return list(dict.fromkeys(_clean_query(value) for value in candidates))

    async def _reply_candidates(self, ctx: Context) -> list[str]:
        reference = getattr(ctx.message, "reference", None)
        if reference is None:
            return []
        resolved = getattr(reference, "resolved", None)
        if isinstance(resolved, discord.Message):
            return self._message_candidates(resolved)
        message_id = getattr(reference, "message_id", None)
        if message_id is None:
            return []
        try:
            message = await ctx.channel.fetch_message(message_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return []
        return self._message_candidates(message)

    async def _resolve_query(self, ctx: Context, query: str) -> str:
        query = _clean_query(query)
        if query:
            return query
        current = self._message_candidates(ctx.message)
        if current:
            return current[0]
        replied = await self._reply_candidates(ctx)
        if replied:
            return replied[0]
        raise commands.BadArgument(
            "Provide a search, URL, or attachment, or reply to a message containing one."
        )

    async def _spotify_lookup(
        self, ctx: Context, query: str
    ) -> tuple[str, str, float, str, str]:
        match = SPOTIFY_TRACK_RE.search(query)
        if match is None:
            raise commands.BadArgument("That is not a Spotify track URL.")
        token = str(getattr(ctx.bot, "spotify_key", "") or "")
        if not token:
            raise commands.BadArgument("Spotify is not connected right now.")
        url = f"https://api.spotify.com/v1/tracks/{match.group('id')}"
        headers = {"Authorization": f"Bearer {token}"}
        async with ctx.session.get(url, headers=headers) as response:
            if response.status == 401:
                raise commands.BadArgument("Spotify authorization has expired.")
            if response.status != 200:
                raise commands.BadArgument("Spotify could not find that track.")
            data = await response.json()
        name = str(data.get("name") or "").strip()
        artists = data.get("artists")
        artist = ""
        if isinstance(artists, list) and artists:
            first_artist = artists[0]
            if isinstance(first_artist, dict):
                artist = str(first_artist.get("name") or "").strip()
        duration = float(data.get("duration_ms") or 0) / 1000
        if not name or not artist:
            raise commands.BadArgument("Spotify did not return usable track details.")
        return (
            f"ytsearch5:{artist} {name} official audio",
            f"{artist} - {name}",
            duration,
            name,
            artist,
        )

    async def _spotify_episode_lookup(
        self, ctx: Context, query: str
    ) -> tuple[str, str, float, str, str]:
        """Resolve a Spotify podcast episode to a searchable YouTube query.

        yt-dlp cannot play a Spotify episode page directly.  Spotify's API
        gives us the episode/show title and duration, which is enough to find
        a matching public upload while retaining the Spotify URL as the
        request/webpage reference.
        """

        match = SPOTIFY_EPISODE_RE.search(query)
        if match is None:
            raise commands.BadArgument("That is not a Spotify episode URL.")
        token = str(getattr(ctx.bot, "spotify_key", "") or "")
        if not token:
            raise commands.BadArgument("Spotify is not connected right now.")
        url = f"https://api.spotify.com/v1/episodes/{match.group('id')}"
        headers = {"Authorization": f"Bearer {token}"}
        async with ctx.session.get(url, headers=headers) as response:
            if response.status == 401:
                raise commands.BadArgument("Spotify authorization has expired.")
            if response.status != 200:
                raise commands.BadArgument("Spotify could not find that episode.")
            data = await response.json()
        name = str(data.get("name") or "").strip()
        show = data.get("show")
        show_name = (
            str(show.get("name") or "").strip() if isinstance(show, dict) else ""
        )
        duration = float(data.get("duration_ms") or 0) / 1000
        if not name:
            raise commands.BadArgument("Spotify did not return usable episode details.")
        lookup = (
            f"ytsearch5:{show_name} {name} podcast"
            if show_name
            else f"ytsearch5:{name} podcast"
        )
        return (
            lookup,
            f"{show_name} - {name}" if show_name else name,
            duration,
            name,
            show_name,
        )

    async def _spotify_album_tracks(
        self,
        ctx: Context,
        query: str,
    ) -> tuple[str, list[VoiceTrack], int]:
        """Build lazy YouTube lookups from up to 50 Spotify album tracks."""

        match = SPOTIFY_ALBUM_RE.search(query)
        if match is None:
            raise commands.BadArgument("That is not a Spotify album URL.")
        token = str(getattr(ctx.bot, "spotify_key", "") or "")
        if not token:
            raise commands.BadArgument("Spotify is not connected right now.")
        album_id = match.group("id")
        headers = {"Authorization": f"Bearer {token}"}
        album_url = f"https://api.spotify.com/v1/albums/{album_id}"
        tracks_url = f"{album_url}/tracks?limit={MAX_QUEUE_SIZE}"

        async with ctx.session.get(album_url, headers=headers) as response:
            if response.status == 401:
                raise commands.BadArgument("Spotify authorization has expired.")
            if response.status != 200:
                raise commands.BadArgument("Spotify could not find that album.")
            album_data = await response.json()
        async with ctx.session.get(tracks_url, headers=headers) as response:
            if response.status == 401:
                raise commands.BadArgument("Spotify authorization has expired.")
            if response.status != 200:
                raise commands.BadArgument(
                    "Spotify could not load that album's tracks."
                )
            tracks_data = await response.json()

        album_name = str(album_data.get("name") or "Spotify album").strip()
        raw_items = tracks_data.get("items")
        if not isinstance(raw_items, list):
            raise commands.BadArgument("Spotify returned no tracks for that album.")
        total = int(tracks_data.get("total") or len(raw_items))
        tracks: list[VoiceTrack] = []
        for item in raw_items[:MAX_QUEUE_SIZE]:
            if not isinstance(item, dict) or item.get("is_local"):
                continue
            name = str(item.get("name") or "").strip()
            artists = item.get("artists")
            artist_names = (
                [
                    str(artist.get("name") or "").strip()
                    for artist in artists
                    if isinstance(artist, dict) and artist.get("name")
                ]
                if isinstance(artists, list)
                else []
            )
            if not name or not artist_names:
                continue
            artist_text = ", ".join(artist_names)
            duration = float(item.get("duration_ms") or 0) / 1000
            if duration > MAX_TRACK_SECONDS:
                continue
            external_urls = item.get("external_urls")
            webpage_url = (
                str(external_urls.get("spotify"))
                if isinstance(external_urls, dict) and external_urls.get("spotify")
                else query
            )
            tracks.append(
                VoiceTrack(
                    request=query,
                    lookup=(
                        f"ytsearch5:{artist_text} {name} {album_name} official audio"
                    ),
                    title=f"{artist_text} - {name}"[:200],
                    requester_id=ctx.author.id,
                    duration=duration or None,
                    webpage_url=webpage_url,
                    search_title=name,
                    search_artists=tuple(artist_names),
                    search_album=album_name,
                )
            )
        if not tracks:
            raise commands.BadArgument("Spotify returned no playable album tracks.")
        return album_name[:200], tracks, total

    async def _extract_info(
        self,
        query: str,
        *,
        spotify_track: VoiceTrack | None = None,
    ) -> dict[str, Any]:
        base_args = [
            sys.executable,
            "-m",
            "utils.ytdlp_safe",
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            "--skip-download",
            "-f",
            "bestaudio/best",
            "--js-runtimes",
            "deno",
            "--socket-timeout",
            "15",
        ]
        use_cookies = (
            _is_youtube_query(query)
            and (FILES_ROOT / "cookies" / "youtube-cookies.txt").is_file()
        )
        cookie_path = _copy_youtube_cookies() if use_cookies else None
        last_stderr = ""
        try:
            for attempt in range(2 if use_cookies else 1):
                args = [*base_args]
                if attempt == 0 and cookie_path is not None:
                    args.extend(["--cookies", str(cookie_path)])
                    args.extend(
                        ["--extractor-args", "youtube:player_client=web_embedded"]
                    )
                args.append(query)
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=EXTRACT_TIMEOUT
                    )
                except asyncio.CancelledError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                    await process.communicate()
                    raise
                except TimeoutError as error:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                    await process.communicate()
                    raise commands.BadArgument(
                        "The media search took too long."
                    ) from error

                last_stderr = stderr.decode(errors="replace")
                output = stdout.decode(errors="replace").strip()
                for line in reversed(output.splitlines()):
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    entries = payload.get("entries")
                    if isinstance(entries, list):
                        candidates = [
                            item for item in entries if isinstance(item, dict)
                        ]
                        if spotify_track is not None:
                            candidates = _rank_spotify_results(
                                candidates, spotify_track
                            )
                            index = spotify_track.search_candidate_index
                        else:
                            index = 0
                        if candidates:
                            return candidates[min(index, len(candidates) - 1)]
                    elif payload.get("url"):
                        return payload
        finally:
            if cookie_path is not None:
                cookie_path.unlink(missing_ok=True)
        detail = _public_error(last_stderr)
        if detail:
            raise commands.BadArgument(f"Could not find playable media: {detail}")
        raise commands.BadArgument("The media search returned no playable result.")

    @staticmethod
    def _audio_details(info: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
        def headers_for(value: object) -> dict[str, str]:
            if not isinstance(value, dict):
                return {}
            return {
                str(name): str(header_value)
                for name, header_value in value.items()
                if isinstance(name, str) and isinstance(header_value, str)
            }

        base_headers = headers_for(info.get("http_headers"))
        value = info.get("url")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            if str(info.get("acodec") or "").casefold() != "none" and not _is_image_url(
                value
            ):
                return value, base_headers
        formats = info.get("formats")
        if not isinstance(formats, list):
            return None, base_headers
        candidates = [
            item
            for item in formats
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and str(item.get("url")).startswith(("http://", "https://"))
            and str(item.get("acodec") or "none").casefold() != "none"
        ]
        candidates.sort(key=lambda item: float(item.get("abr") or 0), reverse=True)
        if not candidates:
            return None, base_headers
        selected = candidates[0]
        return (
            str(selected["url"]),
            headers_for(selected.get("http_headers")) or base_headers,
        )

    @staticmethod
    def _audio_url(info: dict[str, Any]) -> str | None:
        return VoiceMaster._audio_details(info)[0]

    async def _track_from_query(self, ctx: Context, query: str) -> VoiceTrack:
        query = await self._resolve_query(ctx, query)
        sfx_match = SFX_QUERY_RE.fullmatch(query)
        if sfx_match is not None:
            selector = sfx_match.group("selector").strip()
            if selector.casefold() in {"r", "rand", "random"}:
                selector = "random"
            try:
                effect = find_audio_effect(selector)
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            return self._sound_effect_track(ctx, query, effect)

        spotify_title = ""
        spotify_duration = 0.0
        spotify_track_title = ""
        spotify_artist = ""
        lookup = query
        if SPOTIFY_EPISODE_RE.search(query):
            (
                lookup,
                spotify_title,
                spotify_duration,
                spotify_track_title,
                spotify_artist,
            ) = await self._spotify_episode_lookup(ctx, query)
        elif SPOTIFY_TRACK_RE.search(query):
            (
                lookup,
                spotify_title,
                spotify_duration,
                spotify_track_title,
                spotify_artist,
            ) = await self._spotify_lookup(ctx, query)

        if _is_direct_media_url(query):
            if _is_image_url(query):
                raise commands.BadArgument(
                    "Images and GIFs do not contain playable audio."
                )
            direct_url = await refresh_discord_attachment_url(self.bot, query)
            await validate_public_url(direct_url)
            return VoiceTrack(
                request=query,
                lookup=query,
                title=_direct_title(query),
                requester_id=ctx.author.id,
                webpage_url=query,
                stream_url=direct_url,
                resolved_at=time.monotonic(),
            )

        if not lookup.startswith(("http://", "https://")) and not re.match(
            r"ytsearch\d*:", lookup, re.IGNORECASE
        ):
            lookup = f"ytsearch1:{lookup}"
        if lookup.startswith(("http://", "https://")):
            await validate_public_url(lookup)
        spotify_probe = (
            VoiceTrack(
                request=query,
                lookup=lookup,
                title=spotify_title,
                requester_id=ctx.author.id,
                duration=spotify_duration or None,
                search_title=spotify_track_title,
                search_artists=(spotify_artist,) if spotify_artist else (),
            )
            if spotify_track_title
            else None
        )
        info = await self._extract_info(lookup, spotify_track=spotify_probe)
        stream_url, http_headers = self._audio_details(info)
        if stream_url is None:
            raise commands.BadArgument("That source does not contain an audio stream.")
        await validate_public_url(stream_url)
        title = spotify_title or str(info.get("title") or "Audio stream").strip()
        duration = spotify_duration or float(info.get("duration") or 0)
        if duration > MAX_TRACK_SECONDS:
            raise commands.BadArgument("Each track must be one hour or shorter.")
        return VoiceTrack(
            request=query,
            lookup=lookup,
            title=title[:200],
            requester_id=ctx.author.id,
            duration=duration or None,
            webpage_url=str(info.get("webpage_url") or query),
            stream_url=stream_url,
            resolved_at=time.monotonic(),
            http_headers=http_headers,
            search_title=spotify_track_title,
            search_artists=(spotify_artist,) if spotify_artist else (),
        )

    @staticmethod
    def _sound_effect_track(
        ctx: Context,
        request: str,
        effect: AudioEffect,
    ) -> VoiceTrack:
        if effect.duration > MAX_TRACK_SECONDS:
            raise commands.BadArgument("Each track must be one hour or shorter.")
        return VoiceTrack(
            request=request,
            lookup=str(effect.path),
            title=effect.display_name,
            requester_id=ctx.author.id,
            duration=effect.duration,
            stream_url=None,
            resolved_at=time.monotonic(),
            local_path=effect.path,
        )

    async def _stream_url(self, track: VoiceTrack) -> str:
        if track.local_path is not None:
            if not track.local_path.is_file():
                raise commands.BadArgument("That bundled sound effect is unavailable.")
            return str(track.local_path)
        if (
            track.stream_url
            and time.monotonic() - track.resolved_at < STREAM_REFRESH_SECONDS
        ):
            stream_url = track.stream_url
        elif _is_direct_media_url(track.lookup):
            stream_url = await refresh_discord_attachment_url(self.bot, track.lookup)
        else:
            info = await self._extract_info(
                track.lookup,
                spotify_track=track if track.search_title else None,
            )
            stream_url, http_headers = self._audio_details(info)
            if stream_url is None:
                raise commands.BadArgument("The source no longer has an audio stream.")
            if not track.search_title:
                track.title = str(info.get("title") or track.title)[:200]
            duration = float(info.get("duration") or 0)
            if duration > MAX_TRACK_SECONDS:
                raise commands.BadArgument("Each track must be one hour or shorter.")
            track.duration = track.duration or duration or None
            track.stream_url = stream_url
            track.resolved_at = time.monotonic()
            track.http_headers = http_headers
        await validate_public_url(stream_url)
        return stream_url

    async def _send_player_message(self, player: VoicePlayer, content: str) -> None:
        if player.text_channel is None:
            return
        try:
            await player.text_channel.send(
                content, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            self.bot.logger.debug("Could not send voice player status", exc_info=True)

    @asynccontextmanager
    async def _player_typing(self, player: VoicePlayer) -> AsyncIterator[None]:
        """Show typing while a voice source is being resolved and started."""
        channel = player.text_channel
        typing_factory = getattr(channel, "typing", None)
        if not callable(typing_factory):
            yield
            return

        typing: Any = typing_factory()
        entered = False
        try:
            await typing.__aenter__()
            entered = True
        except discord.HTTPException:
            self.bot.logger.debug("Could not start voice player typing", exc_info=True)

        try:
            yield
        finally:
            if entered:
                try:
                    await typing.__aexit__(None, None, None)
                except discord.HTTPException:
                    self.bot.logger.debug(
                        "Could not stop voice player typing", exc_info=True
                    )

    async def _run_player(self, player: VoicePlayer) -> None:
        try:
            while True:
                # Do not consume a queued track while the voice client is
                # reconnecting or has just been disconnected.  The previous
                # implementation popped it first and then dropped it when
                # this check failed, which left the queue apparently empty
                # and made subsequent skip requests report no active track.
                voice = player.voice
                if voice is None or not voice.is_connected():
                    break
                if player.repeat_track is None and not player.queue:
                    # Do not retire the worker between tracks.  A short-lived
                    # worker made enqueue race its final ``while`` check after
                    # skips and natural track endings, occasionally leaving a
                    # new item at position one with nobody consuming it.
                    self._schedule_idle_disconnect(player)
                    player.queue_event.clear()
                    if player.repeat_track is not None or player.queue:
                        continue
                    await player.queue_event.wait()
                    continue
                self._cancel_idle_disconnect(player)
                replaying = player.repeat_track is not None
                if replaying:
                    track = player.repeat_track
                    player.repeat_track = None
                else:
                    track = player.queue.popleft()
                if track is None:
                    continue
                player.current = track
                player.skip_current = False
                player.skip_notice = False
                player.skip_votes.clear()
                completed = False
                last_error: Exception | None = None
                attempt_limit = (
                    SPOTIFY_PLAYBACK_RETRY_ATTEMPTS
                    if track.search_title
                    else PLAYBACK_RETRY_ATTEMPTS
                )
                try:
                    for attempt in range(attempt_limit):
                        source: discord.PCMVolumeTransformer | None = None
                        pcm_source: discord.FFmpegPCMAudio | None = None
                        started_at = time.monotonic()
                        try:
                            voice = player.voice
                            if voice is None or not voice.is_connected():
                                if replaying:
                                    player.repeat_track = track
                                else:
                                    player.queue.appendleft(track)
                                break
                            playback_confirmed = False
                            async with self._player_typing(player):
                                use_ytdlp_transport = (
                                    track.local_path is None
                                    and _is_youtube_query(track.lookup)
                                    and bool(track.webpage_url)
                                )
                                if use_ytdlp_transport:
                                    pcm_source = _YtDlpPipeAudio(
                                        cast(str, track.webpage_url)
                                    )
                                else:
                                    stream_url = await self._stream_url(track)
                                    pcm_source = discord.FFmpegPCMAudio(
                                        stream_url,
                                        before_options=_ffmpeg_before_options(track),
                                        options=(
                                            "-vn -sn -dn -t 3600 " "-loglevel warning"
                                        ),
                                    )
                                source = discord.PCMVolumeTransformer(
                                    pcm_source,
                                    volume=_volume_gain(player.volume),
                                )
                                player.source = source
                                finished = asyncio.Event()
                                errors: list[Exception] = []

                                def after(error: Exception | None) -> None:
                                    if error is not None:
                                        errors.append(error)
                                    self.bot.loop.call_soon_threadsafe(finished.set)

                                voice.play(source, after=after)
                                playback_started = getattr(
                                    pcm_source, "playback_started", None
                                )
                                if isinstance(playback_started, asyncio.Event):
                                    started_waiter = asyncio.create_task(
                                        playback_started.wait()
                                    )
                                    finished_waiter = asyncio.create_task(
                                        finished.wait()
                                    )
                                    done, pending = await asyncio.wait(
                                        {started_waiter, finished_waiter},
                                        timeout=EXTRACT_TIMEOUT,
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )
                                    for waiter in pending:
                                        waiter.cancel()
                                    if pending:
                                        await asyncio.gather(
                                            *pending, return_exceptions=True
                                        )
                                    if started_waiter in done:
                                        playback_confirmed = True
                                    elif not done:
                                        raise RuntimeError(
                                            "Playback did not produce audio in time."
                                        )
                                else:
                                    try:
                                        await asyncio.wait_for(
                                            finished.wait(),
                                            timeout=PREMATURE_PLAYBACK_SECONDS,
                                        )
                                    except TimeoutError:
                                        playback_confirmed = True

                            if playback_confirmed:
                                # Only announce after the source has survived
                                # the premature-failure window. The typing
                                # indicator has ended before this is sent.
                                if player.pending_notice is not None:
                                    safe_title = discord.utils.escape_markdown(
                                        discord.utils.escape_mentions(track.title)
                                    )
                                    await self._send_player_message(
                                        player,
                                        f"{player.pending_notice}Now playing "
                                        f"**{safe_title}**\n"
                                        f"-# \\- *Queued by <@{track.requester_id}>*"
                                        f" | {_format_track_duration(track)}",
                                    )
                                    player.pending_notice = None
                                await finished.wait()
                            elapsed = time.monotonic() - started_at
                            if errors:
                                raise commands.BadArgument(
                                    _public_error(str(errors[0]))
                                )
                            expected_longer = (
                                track.duration is None
                                or track.duration > PREMATURE_PLAYBACK_SECONDS * 2
                            )
                            if (
                                not player.skip_current
                                and expected_longer
                                and elapsed < PREMATURE_PLAYBACK_SECONDS
                            ):
                                process = getattr(pcm_source, "_process", None)
                                return_code = getattr(process, "returncode", None)
                                raise RuntimeError(
                                    "FFmpeg ended before playback started "
                                    f"({elapsed:.2f}s, exit code {return_code!r})"
                                )
                            completed = True
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            last_error = error
                            self.bot.logger.warning(
                                "Voice playback attempt %s/%s failed for guild %s "
                                "and track %r: %s",
                                attempt + 1,
                                attempt_limit,
                                player.guild_id,
                                track.title,
                                error,
                                exc_info=True,
                            )
                            if attempt + 1 < attempt_limit:
                                # Extracted media URLs are short-lived and can
                                # fail without discord.py surfacing an error.
                                # Force a fresh extraction before one bounded
                                # retry instead of silently emptying the queue.
                                if track.local_path is None:
                                    track.stream_url = None
                                    track.resolved_at = 0.0
                                    track.http_headers.clear()
                                    if track.search_title:
                                        track.search_candidate_index += 1
                                await asyncio.sleep(0.25)
                        finally:
                            if source is not None:
                                source.cleanup()
                            if player.source is source:
                                player.source = None
                except asyncio.CancelledError:
                    raise
                finally:
                    if player.current is track:
                        player.current = None

                if not completed and last_error is not None:
                    await self._send_player_message(
                        player,
                        f"Could not play **{discord.utils.escape_markdown(track.title)}**: "
                        f"{_public_error(last_error)}",
                    )

                skipped = player.skip_notice
                if completed and not player.skip_current:
                    if player.repeat_mode == "one":
                        if player.repeat_remaining is None:
                            player.repeat_track = track
                        elif player.repeat_remaining > 0:
                            player.repeat_remaining -= 1
                            player.repeat_track = track
                        else:
                            player.repeat_mode = "off"
                            player.repeat_remaining = None
                    elif player.repeat_mode == "queue":
                        player.queue.append(track)

                next_track = player.repeat_track or (
                    player.queue[0] if player.queue else None
                )
                if next_track is not None and (completed or skipped or last_error):
                    player.pending_notice = (
                        "Skipped the current track. " if skipped else ""
                    )
                elif skipped:
                    await self._send_player_message(
                        player,
                        "Skipped the current track. Nothing else is queued.",
                    )
        finally:
            current_task = asyncio.current_task()
            # A request can append a track while this worker is finishing its
            # last iteration.  Serialize the hand-off with enqueue so the
            # queue is either adopted by this worker or by one replacement,
            # never left idle and never consumed by two workers.
            async with player.worker_lock:
                if player.worker is current_task:
                    player.worker = None
                if (
                    (player.queue or player.repeat_track is not None)
                    and player.voice is not None
                    and player.voice.is_connected()
                    and current_task is not None
                    and not current_task.cancelling()
                ):
                    self._start_worker_locked(player, self._run_player)
                elif (
                    not player.queue
                    and player.repeat_track is None
                    and player.voice is not None
                ):
                    # Keep an otherwise empty voice connection around briefly
                    # so a follow-up request can reuse it.
                    self._schedule_idle_disconnect(player)

    async def _enqueue_many(
        self,
        ctx: Context,
        tracks: list[VoiceTrack],
        *,
        announce_first: bool = False,
    ) -> tuple[VoicePlayer, list[VoiceTrack]]:
        guild = ctx.guild
        if guild is None:
            raise commands.NoPrivateMessage(
                "Voice commands can only be used in servers."
            )
        if not tracks:
            raise commands.BadArgument("There are no tracks to queue.")
        player = self._player(guild.id)
        # Keep connecting, cancelling the idle disconnect, appending, and
        # starting the worker in one hand-off.  Previously the queue append
        # happened outside ``worker_lock``; if the previous worker was in its
        # final cleanup at the same time, it could observe an empty queue,
        # schedule the idle disconnect, and leave this new track stranded with
        # no worker (the next ``skip`` then correctly saw no active track).
        worker_to_wait: asyncio.Task[None] | None = None
        async with player.worker_lock:
            self._cancel_idle_disconnect(player)
            await self._ensure_voice(ctx, player)
            available = MAX_QUEUE_SIZE - len(player.queue)
            if available <= 0:
                raise commands.BadArgument("The voice queue is full.")
            accepted = tracks[:available]
            self._claim_dj(ctx, player)
            player.text_channel = ctx.channel
            starts_immediately = False
            if announce_first and player.voice is not None:
                starts_immediately = (
                    player.current is None
                    and player.repeat_track is None
                    and not player.queue
                    and not player.voice.is_playing()
                    and not player.voice.is_paused()
                )
            if announce_first and starts_immediately and player.pending_notice is None:
                player.pending_notice = ""
            player.queue.extend(accepted)
            player.queue_event.set()
            worker = player.worker
            if (
                worker is not None
                and not worker.done()
                and self._needs_skip_worker_recovery(player)
            ):
                # A stop callback can race with a new request.  If the old
                # worker is still waiting for that callback while the voice
                # client is already idle, cancel it and wait for its cleanup
                # before starting the queued track.
                worker.cancel()
                worker_to_wait = worker
            else:
                self._start_worker_locked(player, self._run_player)

        if worker_to_wait is not None:
            try:
                await worker_to_wait
            except asyncio.CancelledError:
                pass
            async with player.worker_lock:
                if player.worker is worker_to_wait:
                    player.worker = None
                if (
                    player.queue
                    and player.voice is not None
                    and player.voice.is_connected()
                ):
                    self._start_worker_locked(player, self._run_player)
        return player, accepted

    async def _enqueue(
        self,
        ctx: Context,
        track: VoiceTrack,
        *,
        announce_first: bool = False,
    ) -> VoicePlayer:
        player, _accepted = await self._enqueue_many(
            ctx, [track], announce_first=announce_first
        )
        return player

    async def _play_impl(
        self, ctx: Context, query: str = "", *, require_control: bool = False
    ) -> None:
        guild = ctx.guild
        if guild is None:
            raise commands.NoPrivateMessage(
                "Voice commands can only be used in servers."
            )
        # Check the caller's channel before resolving a potentially expensive
        # YouTube/Spotify request.  This gives a clear cross-channel response
        # and avoids doing work that cannot be queued.
        player = self._player(guild.id)
        self._check_voice_channel(ctx, player)
        album_name: str | None = None
        album_total = 0
        async with ctx.typing():
            resolved_query = await self._resolve_query(ctx, query)
            if SPOTIFY_ALBUM_RE.search(resolved_query):
                album_name, tracks, album_total = await self._spotify_album_tracks(
                    ctx, resolved_query
                )
            else:
                tracks = [await self._track_from_query(ctx, resolved_query)]

        if album_name is not None:
            _player, accepted = await self._enqueue_many(
                ctx,
                tracks,
                announce_first=True,
            )
            safe_album = discord.utils.escape_markdown(
                discord.utils.escape_mentions(album_name)
            )
            omitted = max(0, len(tracks) - len(accepted))
            capped = max(0, album_total - len(tracks))
            details = ""
            if omitted:
                details = f" The queue limit left out **{omitted}** track(s)."
            elif capped:
                details = (
                    f" Spotify lists **{album_total}** tracks, so only the first "
                    f"**{MAX_QUEUE_SIZE}** were queued."
                )
            await ctx.send(
                f"Queued **{len(accepted)}** tracks from **{safe_album}**.{details}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        track = tracks[0]

        # A DJ can choose whether a new request should interrupt the active
        # track or wait its turn.  Other listeners do not get this prompt:
        # their play requests are simply appended to the queue.
        voice = player.voice
        active = (
            player.current is not None
            and voice is not None
            and voice.is_connected()
            and (voice.is_playing() or voice.is_paused())
        )
        if require_control and active and self._can_control(ctx, player):
            view = PlayChoiceView(ctx, self, player, track)
            view.message = await ctx.send(
                f"The DJ is already playing a track. What should happen with "
                f"**{view.safe_title}**?",
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        player = await self._enqueue(ctx, track, announce_first=True)
        position = len(player.queue) + (
            1 if player.current is not None or player.repeat_track is not None else 0
        )
        await ctx.send(
            f"Queued **{discord.utils.escape_markdown(track.title)}**"
            f" at position **{position}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _pause_impl(self, ctx: Context) -> None:
        guild = ctx.guild
        self._require_voice_caller(ctx)
        voice = guild.voice_client if guild is not None else None
        if not isinstance(voice, discord.VoiceClient) or not voice.is_connected():
            raise commands.BadArgument("I am not playing anything in this server.")
        player = self.players.get(guild.id) if guild is not None else None
        if player is None:
            raise commands.BadArgument(
                "Only the current DJ can pause or resume playback."
            )
        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument(
                "Only the current DJ can pause or resume playback."
            )
        if voice.is_paused():
            voice.resume()
            await ctx.send("Resumed the current track.")
        elif voice.is_playing():
            voice.pause()
            await ctx.send("Paused the current track.")
        else:
            raise commands.BadArgument("I am not playing anything in this server.")

    async def _restart_impl(self, ctx: Context) -> None:
        """Restart the active track without changing the pending queue."""

        guild = ctx.guild
        self._require_voice_caller(ctx)
        player = self.players.get(guild.id) if guild is not None else None
        if (
            player is None
            or player.voice is None
            or not player.voice.is_connected()
            or player.current is None
            or not (player.voice.is_playing() or player.voice.is_paused())
        ):
            raise commands.BadArgument("There is no active track to restart.")

        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument(
                "Only the current DJ can restart the active track."
            )

        track = player.current
        player.repeat_track = track
        player.pending_notice = None
        player.skip_votes.clear()
        player.skip_current = True
        player.skip_notice = False
        player.voice.stop()
        await ctx.send(
            f"Restarting **{discord.utils.escape_markdown(track.title)}** "
            "from the beginning.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _stop_impl(self, ctx: Context) -> None:
        """Stop playback and clear queued tracks for the current DJ."""

        guild = ctx.guild
        self._require_voice_caller(ctx)
        voice = guild.voice_client if guild is not None else None
        player = self.players.get(guild.id) if guild is not None else None
        if (
            player is None
            or not isinstance(voice, discord.VoiceClient)
            or not voice.is_connected()
        ):
            raise commands.BadArgument("I am not playing anything in this server.")
        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument("Only the current DJ can stop playback.")
        player.queue.clear()
        player.repeat_track = None
        player.pending_notice = None
        player.skip_votes.clear()
        player.skip_current = True
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        elif player.current is None:
            self._schedule_idle_disconnect(player)
        await ctx.send(
            "Stopped playback and cleared the queue.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _disconnect_impl(self, ctx: Context) -> None:
        """Clear the session and disconnect Fishie from the voice channel."""

        guild = ctx.guild
        self._require_voice_caller(ctx)
        player = self.players.get(guild.id) if guild is not None else None
        if player is None or player.voice is None or not player.voice.is_connected():
            raise commands.BadArgument("I am not connected to voice in this server.")
        voice = player.voice
        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument("Only the current DJ can disconnect the bot.")

        worker: asyncio.Task[None] | None = None
        async with player.worker_lock:
            self._cancel_idle_disconnect(player)
            player.queue.clear()
            player.repeat_track = None
            player.pending_notice = None
            player.queue_event.set()
            player.skip_votes.clear()
            player.dj_id = None
            player.skip_current = True
            player.skip_notice = False
            worker = player.worker
            if worker is not None and not worker.done():
                worker.cancel()
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            try:
                await voice.disconnect(force=True)
            except discord.DiscordException as error:
                raise commands.BadArgument(
                    "I could not disconnect from that voice channel."
                ) from error
            finally:
                player.current = None
                player.source = None
                player.voice = None
                if guild is not None and self.players.get(guild.id) is player:
                    self.players.pop(guild.id, None)

        if worker is not None and worker is not asyncio.current_task():
            try:
                await worker
            except asyncio.CancelledError:
                pass
        await ctx.send(
            "Disconnected from the voice channel and cleared the queue.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _volume_impl(self, ctx: Context, volume: float) -> None:
        """Set the shared volume for the active and queued voice tracks."""

        if not 0 <= volume <= MAX_VOLUME:
            raise commands.BadArgument("Volume must be between 0 and 150.")
        guild = ctx.guild
        self._require_voice_caller(ctx)
        player = self.players.get(guild.id) if guild is not None else None
        if (
            player is None
            or player.voice is None
            or not player.voice.is_connected()
            or (
                player.current is None
                and player.repeat_track is None
                and not player.queue
            )
        ):
            raise commands.BadArgument("There are no active or queued tracks.")
        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument("Only the current DJ can change the volume.")
        player.volume = volume
        if player.source is not None:
            player.source.volume = _volume_gain(volume)
        await ctx.send(
            f"Voice volume set to **{volume:g}%**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _repeat_impl(self, ctx: Context, mode: str = "") -> None:
        guild = ctx.guild
        if guild is None:
            raise commands.NoPrivateMessage(
                "Voice commands can only be used in servers."
            )
        player = self._player(guild.id)
        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument("Only the current DJ can change repeat mode.")
        normalized = mode.casefold().strip()
        repeat_count: int | None = None
        if normalized.isdigit():
            repeat_count = int(normalized)
            if not 0 <= repeat_count <= 1000:
                raise commands.BadArgument(
                    "The repeat count must be between 0 and 1,000."
                )
            normalized = "off" if repeat_count == 0 else "one"
        elif normalized in {"", "one", "track", "forever", "infinite"}:
            normalized = "one"
        elif normalized == "toggle":
            normalized = "off" if player.repeat_mode == "one" else "one"
        elif normalized not in {"off", "queue"}:
            raise commands.BadArgument(
                "Choose `off`, `one`, `queue`, or a repeat count from 1 to 1,000."
            )
        player.repeat_mode = cast(Literal["off", "one", "queue"], normalized)
        player.repeat_remaining = repeat_count if normalized == "one" else None
        if normalized != "one":
            player.repeat_track = None
        if normalized == "one" and repeat_count is not None:
            label = f"the current track for **{repeat_count}** additional play(s)"
        else:
            label = {
                "off": "disabled",
                "one": "the current track forever",
                "queue": "the queue",
            }[normalized]
        await ctx.send(f"Repeat is now set to {label}.")

    async def _skip_impl(self, ctx: Context) -> None:
        guild = ctx.guild
        self._require_voice_caller(ctx)
        player = self.players.get(guild.id) if guild is not None else None
        if (
            player is None
            or player.voice is None
            or not (player.voice.is_playing() or player.voice.is_paused())
        ):
            raise commands.BadArgument("There is no active track to skip.")
        self._check_voice_channel(ctx, player)
        self._claim_dj(ctx, player)
        if self._is_privileged_dj(ctx) or player.dj_id == ctx.author.id:
            player.skip_votes.clear()
            player.skip_current = True
            player.skip_notice = True
            player.voice.stop()
            return

        member_ids = self._human_voice_member_ids(player.voice)
        if ctx.author.id not in member_ids:
            raise commands.BadArgument(
                "You must be in the voice channel to vote to skip."
            )
        player.skip_votes.intersection_update(member_ids)
        player.skip_votes.add(ctx.author.id)
        votes, required = self._skip_vote_requirement(player)
        if votes < required:
            await ctx.send(f"Skip vote: **{votes}/{required}** voice members voted.")
            return
        player.skip_votes.clear()
        player.skip_current = True
        player.skip_notice = True
        player.voice.stop()

    async def _shuffle_impl(self, ctx: Context) -> None:
        """Shuffle pending tracks for the current voice session.

        Queue management is available from any text channel in the guild while
        the caller is in the active voice channel and follows the normal
        DJ/privileged-member checks.  The active track is never moved; only
        tracks waiting in the queue change order.
        """

        guild = ctx.guild
        self._require_voice_caller(ctx)
        player = self.players.get(guild.id) if guild is not None else None
        if player is None or player.voice is None or not player.voice.is_connected():
            raise commands.BadArgument("I am not connected to voice in this server.")

        self._check_voice_channel(ctx, player)
        if not self._can_control(ctx, player):
            raise commands.BadArgument("Only the current DJ can shuffle the queue.")

        async with player.worker_lock:
            if not player.queue:
                raise commands.BadArgument("There are no queued tracks to shuffle.")
            tracks = list(player.queue)
            random.shuffle(tracks)
            player.queue = deque(tracks)

        await ctx.send(
            f"Shuffled **{len(tracks)}** queued track{'s' if len(tracks) != 1 else ''}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _queue_impl(self, ctx: Context, query: str = "") -> None:
        # ``queue`` accepts the same media sources as ``play``.  In
        # particular, a user can attach a video (whose audio should be queued)
        # or reply to a message containing one without supplying a text query.
        # Previously only a non-empty query delegated to ``_play_impl``, so an
        # attachment/reply silently opened the queue view instead.
        resolved_query = _clean_query(query)
        if not resolved_query:
            candidates = self._message_candidates(ctx.message)
            if not candidates:
                candidates = await self._reply_candidates(ctx)
            if candidates:
                resolved_query = candidates[0]
        if resolved_query:
            await self._play_impl(ctx, resolved_query, require_control=False)
            return
        self._require_voice_caller(ctx)
        guild = ctx.guild
        player = self.players.get(guild.id) if guild is not None else None
        if player is not None:
            self._check_voice_channel(ctx, player)
        if player is None or (
            player.current is None and player.repeat_track is None and not player.queue
        ):
            await ctx.send("The voice queue is empty.")
            return
        await VoiceQueueView(ctx, self, player).start()

    @commands.command(
        name="play",
        extras={
            "usage": "[search, Spotify track/album, URL, attachment, or sfx:<id|name|random>]"
        },
    )
    @commands.guild_only()
    async def play(self, ctx: Context, *, query: str = "") -> None:
        """Play a song, video soundtrack, audio URL, or attached file in voice."""

        await self._play_impl(ctx, query, require_control=True)

    @commands.command(name="pause")
    @commands.guild_only()
    async def pause(self, ctx: Context) -> None:
        """Pause the current voice track, or resume it when already paused."""

        await self._pause_impl(ctx)

    @commands.command(name="restart", aliases=("rewind", "replay"))
    @commands.guild_only()
    async def restart(self, ctx: Context) -> None:
        """Restart the current voice track from the beginning."""

        await self._restart_impl(ctx)

    @commands.command(
        name="repeat",
        aliases=("loop",),
        extras={"usage": "[off|one|queue|1-1000]"},
    )
    @commands.guild_only()
    async def repeat(self, ctx: Context, mode: str = "") -> None:
        """Loop the current track forever or use a finite repeat count."""

        await self._repeat_impl(ctx, mode)

    @commands.command(name="skip")
    @commands.guild_only()
    async def skip(self, ctx: Context) -> None:
        """Skip the current voice track."""

        await self._skip_impl(ctx)

    @commands.command(name="stop")
    @commands.guild_only()
    async def stop(self, ctx: Context) -> None:
        """Stop playback and clear the queue."""

        await self._stop_impl(ctx)

    @commands.command(name="disconnect")
    @commands.guild_only()
    async def disconnect(self, ctx: Context) -> None:
        """Disconnect from voice and clear the current playback session."""

        await self._disconnect_impl(ctx)

    @commands.command(name="volume", extras={"usage": "<0-150>"})
    @commands.guild_only()
    async def volume(self, ctx: Context, volume: float) -> None:
        """Set voice volume from 0 to 150, with 100 as the normal level."""

        await self._volume_impl(ctx, volume)

    @commands.command(
        name="queue",
        extras={
            "usage": "[search, Spotify track/album, URL, attachment, or sfx:<id|name|random>]"
        },
    )
    @commands.guild_only()
    async def queue(self, ctx: Context, *, query: str = "") -> None:
        """Show the voice queue or add another track to it."""

        await self._queue_impl(ctx, query)

    @commands.hybrid_group(
        name="voicemaster",
        invoke_without_command=True,  # pyright: ignore[reportCallIssue]
        extras={
            "usage": "<play|pause|restart|repeat|skip|stop|disconnect|volume|queue|shuffle>"
        },
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster(self, ctx: Context) -> None:
        """Control voice playback in a server voice channel."""

        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Use `voicemaster play`, `voicemaster pause`, `voicemaster repeat`, "
                "`voicemaster restart`, `voicemaster skip`, `voicemaster stop`, "
                "`voicemaster disconnect`, `voicemaster volume`, or "
                "`voicemaster queue`, or `voicemaster shuffle`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @voicemaster.command(name="play")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        query="A search, Spotify track/album, URL, attachment, or sfx:<id|name|random>"
    )
    async def voicemaster_play(self, ctx: Context, *, query: str = "") -> None:
        """Play a song, video soundtrack, audio URL, or attached file."""

        await self._play_impl(ctx, query, require_control=True)

    @voicemaster.command(name="pause")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster_pause(self, ctx: Context) -> None:
        """Pause or resume the current voice track."""

        await self._pause_impl(ctx)

    @voicemaster.command(name="restart")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster_restart(self, ctx: Context) -> None:
        """Restart the current voice track from the beginning."""

        await self._restart_impl(ctx)

    @voicemaster.command(name="repeat")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(mode="Blank loops forever; or use off, queue, or 1-1000")
    async def voicemaster_repeat(self, ctx: Context, mode: str = "") -> None:
        """Repeat the current track or queue."""

        await self._repeat_impl(ctx, mode)

    @voicemaster.command(name="loop")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(mode="Blank loops forever; or use off, queue, or 1-1000")
    async def voicemaster_loop(self, ctx: Context, mode: str = "") -> None:
        """Alias for repeat."""

        await self._repeat_impl(ctx, mode)

    @voicemaster.command(name="skip")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster_skip(self, ctx: Context) -> None:
        """Skip the current voice track."""

        await self._skip_impl(ctx)

    @voicemaster.command(name="stop")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster_stop(self, ctx: Context) -> None:
        """Stop playback and clear the queue."""

        await self._stop_impl(ctx)

    @voicemaster.command(name="disconnect")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster_disconnect(self, ctx: Context) -> None:
        """Disconnect from voice and clear the playback session."""

        await self._disconnect_impl(ctx)

    @voicemaster.command(name="volume")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(volume="Playback volume from 0 to 150; 100 is normal")
    async def voicemaster_volume(self, ctx: Context, volume: float) -> None:
        """Set voice volume from 0 to 150, with 100 as the normal level."""

        await self._volume_impl(ctx, volume)

    @voicemaster.command(name="queue")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        query="A search, Spotify track/album, URL, attachment, or sfx:<id|name|random>"
    )
    async def voicemaster_queue(self, ctx: Context, *, query: str = "") -> None:
        """Show the voice queue or add another track."""

        await self._queue_impl(ctx, query)

    @voicemaster.command(name="shuffle")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def voicemaster_shuffle(self, ctx: Context) -> None:
        """Shuffle the pending voice queue."""

        await self._shuffle_impl(ctx)

    async def cog_unload(self) -> None:
        for player in self.players.values():
            if player.worker is not None:
                player.worker.cancel()
            self._cancel_idle_disconnect(player)
            if player.voice is not None and player.voice.is_connected():
                try:
                    await player.voice.disconnect(force=True)
                except discord.DiscordException:
                    self.bot.logger.debug(
                        "Could not disconnect voice client during unload",
                        exc_info=True,
                    )
        self.players.clear()


class PlayChoiceView(discord.ui.View):
    """Let the current DJ decide whether a request jumps the queue."""

    def __init__(
        self,
        ctx: Context,
        cog: VoiceMaster,
        player: VoicePlayer,
        track: VoiceTrack,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.cog = cog
        self.player = player
        self.track = track
        self.message: discord.Message | None = None

    @property
    def safe_title(self) -> str:
        return discord.utils.escape_markdown(
            discord.utils.escape_mentions(self.track.title or "Untitled")
        )

    def _disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _edit(self, interaction: discord.Interaction, content: str) -> None:
        self._disable()
        try:
            if self.message is not None:
                await self.message.edit(
                    content=content,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.edit_original_response(
                    content=content,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            self.ctx.bot.logger.debug(
                "Could not update voice play choice", exc_info=True
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            try:
                self.cog._check_voice_channel_for_member(
                    interaction.user,
                    self.player,
                )
            except commands.BadArgument as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return False
            return True
        await interaction.response.send_message(
            "Only the DJ who started this request can choose how to play it.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Play request expired without a choice.",
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                self.ctx.bot.logger.debug(
                    "Could not disable expired voice play choice", exc_info=True
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[Any] | None = None,
    ) -> None:
        try:
            await self.ctx.bot.log_error(
                error, context=self.ctx, interaction=interaction
            )
        except Exception:
            self.ctx.bot.logger.exception(
                "Could not send voice play choice error report"
            )
        message = "The play request could not be updated."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            self.ctx.bot.logger.debug(
                "Could not acknowledge voice play choice error", exc_info=True
            )

    @discord.ui.button(label="Play now", style=discord.ButtonStyle.primary)
    async def play_now(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not self.cog._can_control(self.ctx, self.player):
            await interaction.response.send_message(
                "Only the current DJ can choose how to play this request.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        async with self.player.worker_lock:
            if len(self.player.queue) >= MAX_QUEUE_SIZE:
                await interaction.followup.send(
                    "The voice queue is full.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self.cog._cancel_idle_disconnect(self.player)
            self.player.text_channel = self.ctx.channel
            self.player.queue.appendleft(self.track)
            self.player.queue_event.set()
            # Stopping the active source lets the worker transition
            # immediately. Do not mark this as a user skip: the prompt already
            # tells the DJ what happened, and the skip announcement is
            # reserved for ``skip``.
            self.player.skip_current = True
            self.player.skip_notice = False
            self.cog._start_worker_locked(self.player, self.cog._run_player)
        voice = self.player.voice
        if voice is not None and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        await self._edit(interaction, f"Playing **{self.safe_title}** now.")
        self.stop()

    @discord.ui.button(label="Add to queue", style=discord.ButtonStyle.secondary)
    async def add_to_queue(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not self.cog._can_control(self.ctx, self.player):
            await interaction.response.send_message(
                "Only the current DJ can choose how to play this request.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        await self.cog._enqueue(self.ctx, self.track)
        await self._edit(interaction, f"Queued **{self.safe_title}**.")
        self.stop()


class VoiceQueuePageModal(discord.ui.Modal, title="Go to queue page"):
    """Jump to a queue page without restricting navigation to the DJ."""

    def __init__(self, view: "VoiceQueueView") -> None:
        super().__init__()
        self.view = view
        self.page = discord.ui.TextInput(
            label=f"Page number (1/{view.page_count})",
            min_length=1,
            max_length=8,
            required=True,
            style=discord.TextStyle.short,
        )
        self.add_item(self.page)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.page.value).strip()
        if not value.isdigit():
            await interaction.response.send_message(
                "Please enter a valid page number.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        page = int(value)
        if page < 1 or page > self.view.page_count:
            await interaction.response.send_message(
                f"Page **{page}** does not exist.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        await self.view._set_page(interaction, page - 1, acknowledged=True)


class VoiceQueueView(discord.ui.LayoutView):
    """Components V2 queue browser with DJ-only queue management controls."""

    def __init__(
        self,
        ctx: Context,
        cog: VoiceMaster,
        player: VoicePlayer,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.cog = cog
        self.player = player
        self.page = 0
        self.selected_index: int | None = None
        self.message: discord.Message | None = None
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.player.queue) / QUEUE_PAGE_SIZE))

    def _entries(self) -> list[VoiceTrack]:
        return list(self.player.queue)

    @staticmethod
    def _safe_title(track: VoiceTrack) -> str:
        return discord.utils.escape_markdown(
            discord.utils.escape_mentions(track.title or "Untitled")
        )

    def _render(self) -> None:
        self.clear_items()
        page_count = self.page_count
        self.page %= page_count
        entries = self._entries()
        start = self.page * QUEUE_PAGE_SIZE
        page_entries = entries[start : start + QUEUE_PAGE_SIZE]
        lines = ["## Voice queue"]
        if self.player.current is not None:
            lines.append(
                f"Now playing: **{self._safe_title(self.player.current)}**"
                f" | {_format_track_duration(self.player.current)}"
            )
        else:
            lines.append("Now playing: **Nothing**")
        if self.player.dj_id is not None:
            dj = (
                self.ctx.guild.get_member(self.player.dj_id) if self.ctx.guild else None
            )
            dj_name = getattr(dj, "display_name", None) or getattr(dj, "name", None)
            lines.append(
                f"DJ: **{discord.utils.escape_markdown(dj_name or str(self.player.dj_id))}**"
            )
        else:
            lines.append("DJ: **Unassigned**")
        if page_entries:
            lines.append("")
            lines.extend(
                f"**{start + index + 1}.** {self._safe_title(track)}"
                f" | {_format_track_duration(track)}"
                for index, track in enumerate(page_entries)
            )
        else:
            lines.append("\nNo tracks are waiting.")
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

        can_manage = self._can_manage(self.ctx.author)
        if can_manage and page_entries:
            options = [
                discord.SelectOption(
                    label=(
                        f"{start + index + 1}. "
                        f"{' '.join(track.title.split())[:90] or 'Untitled'}"
                    ),
                    value=str(start + index),
                    default=self.selected_index == start + index,
                )
                for index, track in enumerate(page_entries)
            ]
            selector = discord.ui.Select(
                placeholder="Select a queued track",
                min_values=1,
                max_values=1,
                options=options,
            )
            selector.callback = cast(Any, self._select_track)
            self.add_item(discord.ui.ActionRow(selector))

            remove = discord.ui.Button(
                label="Remove selected",
                style=discord.ButtonStyle.secondary,
                disabled=self.selected_index is None,
            )
            skip_to = discord.ui.Button(
                label="Skip to selected",
                style=discord.ButtonStyle.secondary,
                disabled=self.selected_index is None,
            )
            remove.callback = cast(Any, self._remove_selected)
            skip_to.callback = cast(Any, self._skip_to_selected)
            self.add_item(discord.ui.ActionRow(remove, skip_to))

        if page_count > 1:
            row, _ = build_layout_pagination_row(
                page=self.page,
                page_count=page_count,
                previous=self._previous_page,
                next_page=self._next_page,
                shuffle=self._shuffle_page,
                go_to_page=self._open_page_modal,
                trash=self._delete,
            )
            self.add_item(row)

    def _can_manage(self, user: discord.User | discord.Member) -> bool:
        guild = self.ctx.guild
        if guild is None:
            return False
        self.cog._refresh_dj(self.ctx, self.player)
        return self.cog._can_control_user(guild, user, self.player)

    async def start(self) -> None:
        self._render()
        self.message = await self.ctx.send(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._render()
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_page(
        self,
        interaction: discord.Interaction,
        page: int,
        *,
        acknowledged: bool = False,
    ) -> None:
        if not acknowledged and not interaction.response.is_done():
            await interaction.response.defer()
        self.page = page % self.page_count
        self.selected_index = None
        await self._edit(interaction)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        await self._set_page(interaction, self.page - 1)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        await self._set_page(interaction, self.page + 1)

    async def _shuffle_page(self, interaction: discord.Interaction) -> None:
        if self.page_count <= 1:
            await interaction.response.send_message(
                "There are no other queue pages.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        choices = [index for index in range(self.page_count) if index != self.page]
        await self._set_page(interaction, random.choice(choices))

    async def _open_page_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VoiceQueuePageModal(self))

    async def _delete(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.ctx.author.id and not self._can_manage(
            interaction.user
        ):
            await interaction.response.send_message(
                "Only the person who opened this queue can close it.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            self.cog._check_voice_channel_for_member(
                interaction.user,
                self.player,
            )
        except commands.BadArgument as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        try:
            if self.message is not None:
                await self.message.delete()
            else:
                await interaction.delete_original_response()
        finally:
            self.stop()

    async def _require_manage(self, interaction: discord.Interaction) -> bool:
        try:
            self.cog._check_voice_channel_for_member(
                interaction.user,
                self.player,
            )
        except commands.BadArgument as error:
            message = str(error)
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return False
        if self._can_manage(interaction.user):
            return True
        message = "Only the current DJ or a server moderator can manage the queue."
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return False

    async def _select_track(self, interaction: discord.Interaction) -> None:
        if not await self._require_manage(interaction):
            return
        data = interaction.data
        # Component payloads are mappings in discord.py.  Do not use
        # ``getattr(data, "values")`` for mappings because that returns the
        # dictionary method rather than the selected option list.
        values = data.get("values") if isinstance(data, dict) else None
        if values is None:
            values = getattr(data, "values", None)
        if not values:
            await interaction.response.send_message(
                "Select a queued track first.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            selected = int(values[0])
        except (TypeError, ValueError):
            await interaction.response.send_message(
                "That queue item is no longer available.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if selected < 0 or selected >= len(self.player.queue):
            await interaction.response.send_message(
                "That queue item is no longer available.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        self.selected_index = selected
        await self._edit(interaction)

    async def _remove_selected(self, interaction: discord.Interaction) -> None:
        if not await self._require_manage(interaction):
            return
        if self.selected_index is None:
            await interaction.response.send_message(
                "Select a queued track first.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        items = self._entries()
        if self.selected_index >= len(items):
            self.selected_index = None
            await interaction.response.send_message(
                "That queue item is no longer available.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        removed = items.pop(self.selected_index)
        self.player.queue = deque(items)
        self.selected_index = None
        if not self.player.queue and self.player.current is None:
            self.cog._schedule_idle_disconnect(self.player)
        await self._edit(interaction)
        await self.cog._send_player_message(
            self.player,
            f"Removed **{discord.utils.escape_markdown(removed.title)}** from the queue.",
        )

    async def _skip_to_selected(self, interaction: discord.Interaction) -> None:
        if not await self._require_manage(interaction):
            return
        if self.selected_index is None:
            await interaction.response.send_message(
                "Select a queued track first.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        items = self._entries()
        if self.selected_index >= len(items):
            self.selected_index = None
            await interaction.response.send_message(
                "That queue item is no longer available.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        self.player.queue = deque(items[self.selected_index :])
        self.player.skip_votes.clear()
        self.player.skip_current = True
        if self.player.voice is not None and (
            self.player.voice.is_playing() or self.player.voice.is_paused()
        ):
            self.player.voice.stop()
        self.selected_index = None
        await self._edit(interaction)

    async def interaction_check(self, _interaction: discord.Interaction) -> bool:
        # Queue browsing is public; only the management callbacks are DJ-gated.
        return True

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                self.ctx.bot.logger.debug(
                    "Could not disable timed-out voice queue view", exc_info=True
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[Any] | None = None,
    ) -> None:
        try:
            await self.ctx.bot.log_error(
                error, context=self.ctx, interaction=interaction
            )
        except Exception:
            self.ctx.bot.logger.exception("Could not send voice queue error report")
        message = "The queue could not be updated."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            self.ctx.bot.logger.debug(
                "Could not acknowledge voice queue error", exc_info=True
            )


async def setup(bot: Fishie) -> None:
    await bot.add_cog(VoiceMaster(bot))
