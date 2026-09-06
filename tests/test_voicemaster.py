from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands
from test_support import require_type

import extensions.voicemaster as voicemaster_module

# Test doubles supply only the Discord/service fields exercised by each test.
from extensions.context import Context
from extensions.voicemaster import (
    MAX_QUEUE_SIZE,
    QUEUE_PAGE_SIZE,
    SPOTIFY_ALBUM_RE,
    SPOTIFY_EPISODE_RE,
    VoiceMaster,
    VoicePlayer,
    VoiceQueueView,
    VoiceTrack,
    _clean_query,
    _ffmpeg_before_options,
    _is_direct_media_url,
    _is_image_url,
    _is_youtube_bot_challenge,
    _public_error,
    _rank_spotify_results,
    _volume_gain,
    _youtube_extractor_args,
)


def _voice_queue_view(player: VoicePlayer) -> VoiceQueueView:
    guild = SimpleNamespace(
        get_member=lambda user_id: SimpleNamespace(
            id=user_id, display_name="Queue DJ", name="queue-dj"
        )
    )
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=999),
        guild=guild,
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
    )
    cog = SimpleNamespace(
        _refresh_dj=lambda _ctx, _player: None,
        _can_control_user=lambda _guild, _user, _player: False,
    )
    return VoiceQueueView(cast(Any, ctx), cast(Any, cog), player)


def _voice_queue_text(view: VoiceQueueView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def test_voice_commands_are_text_only_and_grouped_for_app_commands() -> None:
    assert isinstance(VoiceMaster.play, commands.Command)
    assert isinstance(VoiceMaster.pause, commands.Command)
    assert isinstance(VoiceMaster.restart, commands.Command)
    assert VoiceMaster.restart.aliases == ("rewind", "replay")
    assert isinstance(VoiceMaster.repeat, commands.Command)
    assert VoiceMaster.repeat.aliases == ("loop",)
    group = require_type(VoiceMaster.voicemaster, commands.HybridGroup)
    assert {command.name for command in group.commands} == {
        "play",
        "pause",
        "restart",
        "repeat",
        "loop",
        "skip",
        "stop",
        "disconnect",
        "volume",
        "queue",
        "shuffle",
    }
    app_command = VoiceMaster.voicemaster.app_command
    assert app_command.allowed_installs.guild is True
    assert app_command.allowed_installs.user is False
    assert app_command.allowed_contexts.guild is True
    assert app_command.allowed_contexts.dm_channel is False
    assert app_command.allowed_contexts.private_channel is False


def test_voice_input_helpers_distinguish_direct_audio_from_images() -> None:
    audio = "https://cdn.example.test/audio.mp3?token=abc"
    video = "https://cdn.example.test/video.mp4"
    image = "https://cdn.example.test/image.png"

    assert _is_direct_media_url(audio)
    assert _is_direct_media_url(video)
    assert not _is_direct_media_url(image)
    assert not _is_image_url(audio)
    assert _is_image_url(image)
    assert _clean_query("<https://example.test/a.mp3>!") == (
        "https://example.test/a.mp3"
    )
    assert SPOTIFY_ALBUM_RE.search(
        "https://open.spotify.com/album/1234567890123456789012"
    )
    assert SPOTIFY_EPISODE_RE.search(
        "https://open.spotify.com/episode/1234567890123456789012"
    )
    assert MAX_QUEUE_SIZE == 50


def test_audio_url_prefers_selected_audio_and_falls_back_to_formats() -> None:
    assert (
        VoiceMaster._audio_url(
            {"url": "https://cdn.example.test/audio", "acodec": "opus"}
        )
        == "https://cdn.example.test/audio"
    )
    assert (
        VoiceMaster._audio_url(
            {
                "url": "https://cdn.example.test/video",
                "acodec": "none",
                "formats": [
                    {"url": "https://cdn.example.test/low", "acodec": "aac", "abr": 64},
                    {
                        "url": "https://cdn.example.test/high",
                        "acodec": "opus",
                        "abr": 128,
                    },
                ],
            }
        )
        == "https://cdn.example.test/high"
    )


def test_youtube_extractor_uses_supported_clients_and_bot_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FISHIE_YOUTUBE_POT_PROVIDER_URL", raising=False)
    assert _youtube_extractor_args() == [
        "--extractor-args",
        "youtube:player_client=web_embedded,web_safari,mweb",
    ]
    assert _is_youtube_bot_challenge(
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot"
    )
    assert not _is_youtube_bot_challenge("ERROR: [youtube] abc: Video unavailable")

    monkeypatch.setenv("FISHIE_YOUTUBE_POT_PROVIDER_URL", "http://provider:4416/")
    assert _youtube_extractor_args()[-1] == (
        "youtubepot-bgutilhttp:base_url=http://provider:4416"
    )


def test_extractor_errors_do_not_expose_tracebacks() -> None:
    error = """Traceback (most recent call last):
  File \"ytdlp_safe.py\", line 1, in <module>
    main()
yt_dlp.utils.DownloadError: ERROR: [youtube] abc: Sign in to confirm
"""

    assert _public_error(error) == ("ERROR: [youtube] abc: Sign in to confirm")


def test_voice_player_queue_is_fifo_and_repeat_mode_is_per_guild() -> None:
    first = VoiceTrack("one", "one", "One", 1)
    second = VoiceTrack("two", "two", "Two", 1)
    player = VoicePlayer(guild_id=123, queue=deque((first, second)))

    assert player.queue.popleft() is first
    assert player.queue.popleft() is second
    assert player.repeat_mode == "off"
    player.repeat_mode = "queue"
    assert player.repeat_mode == "queue"


def test_voice_volume_uses_quieter_perceptual_curve_and_safe_boost() -> None:
    assert _volume_gain(0) == 0
    assert _volume_gain(5) == pytest.approx(0.002125)
    assert _volume_gain(100) == pytest.approx(0.85)
    assert _volume_gain(150) == pytest.approx(1.275)


@pytest.mark.asyncio
async def test_voice_restart_replays_current_track_without_changing_queue() -> None:
    class Voice:
        channel = SimpleNamespace(id=20)

        def __init__(self) -> None:
            self.stopped = False

        def is_connected(self) -> bool:
            return True

        def is_playing(self) -> bool:
            return True

        def is_paused(self) -> bool:
            return False

        def stop(self) -> None:
            self.stopped = True

    current = VoiceTrack("current", "current", "Current Song", 1, duration=185)
    queued = VoiceTrack("queued", "queued", "Queued Song", 2, duration=120)
    voice = Voice()
    player = VoicePlayer(
        guild_id=123,
        current=current,
        queue=deque((queued,)),
        voice=cast(Any, voice),
        dj_id=9,
    )
    cog = object.__new__(VoiceMaster)
    cast(Any, cog).players = {123: player}
    cast(Any, cog)._can_control = lambda _ctx, _player: True
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(
            id=9, voice=SimpleNamespace(channel=SimpleNamespace(id=20))
        ),
        send=AsyncMock(),
    )

    await cog._restart_impl(cast(Any, ctx))

    assert voice.stopped is True
    assert player.repeat_track is current
    assert list(player.queue) == [queued]
    assert player.skip_current is True
    assert player.skip_notice is False
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.args == (
        "Restarting **Current Song** from the beginning.",
    )
    allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False


def test_voice_queue_layout_shows_durations_and_hides_single_page_controls() -> None:
    player = VoicePlayer(
        guild_id=123,
        current=VoiceTrack("current", "current", "Current Song", 1, duration=185),
        queue=deque(
            (
                VoiceTrack("one", "one", "First Song", 1, duration=62),
                VoiceTrack("two", "two", "Unknown Length", 1),
            )
        ),
        dj_id=42,
    )

    view = _voice_queue_view(player)
    text = _voice_queue_text(view)
    buttons = [
        item for item in view.walk_children() if isinstance(item, discord.ui.Button)
    ]

    assert text == (
        "## Voice queue\n"
        "Now playing: **Current Song** | 3:05\n"
        "DJ: **Queue DJ**\n\n"
        "**1.** First Song | 1:02\n"
        "**2.** Unknown Length | Unknown"
    )
    assert buttons == []


def test_voice_queue_adds_navigation_only_for_multiple_pages() -> None:
    player = VoicePlayer(
        guild_id=123,
        queue=deque(
            VoiceTrack(str(index), str(index), f"Song {index}", 1, duration=60)
            for index in range(QUEUE_PAGE_SIZE + 1)
        ),
    )

    view = _voice_queue_view(player)
    labels = {
        item.label
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button) and item.label is not None
    }

    assert {"<", ">", "#"} <= labels


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_title"),
    [
        ("sfx:62", "Nothing Bad Ever Happens"),
        ("sfx: Nothing Bad Ever Happens", "Nothing Bad Ever Happens"),
    ],
)
async def test_voice_sfx_queries_resolve_bundled_audio(
    query: str, expected_title: str
) -> None:
    cog = object.__new__(VoiceMaster)
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1),
        message=SimpleNamespace(attachments=[], content=""),
    )

    track = await cog._track_from_query(ctx, query)  # type: ignore[arg-type]

    assert track.title == expected_title
    assert track.local_path is not None
    assert track.local_path.is_file()
    assert await cog._stream_url(track) == str(track.local_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ("sfx: random", "sfx:r"))
async def test_voice_sfx_random_aliases_resolve_bundled_audio(query: str) -> None:
    cog = object.__new__(VoiceMaster)
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1),
        message=SimpleNamespace(attachments=[], content=""),
    )

    track = await cog._track_from_query(ctx, query)  # type: ignore[arg-type]

    assert track.local_path is not None
    assert track.local_path.is_file()


@pytest.mark.asyncio
async def test_spotify_album_builds_ordered_lazy_track_lookups() -> None:
    class Response:
        status = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        async def __aenter__(self) -> "Response":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            pass

        async def json(self) -> dict[str, Any]:
            return self.payload

    class Session:
        def get(self, url: str, **_kwargs: Any) -> Response:
            if url.endswith("/tracks?limit=50"):
                return Response(
                    {
                        "total": 2,
                        "items": [
                            {
                                "name": "First",
                                "artists": [{"name": "Artist"}],
                                "duration_ms": 180_000,
                                "external_urls": {
                                    "spotify": "https://open.spotify.com/track/first"
                                },
                                "is_local": False,
                            },
                            {
                                "name": "Second",
                                "artists": [{"name": "Artist"}],
                                "duration_ms": 200_000,
                                "external_urls": {
                                    "spotify": "https://open.spotify.com/track/second"
                                },
                                "is_local": False,
                            },
                        ],
                    }
                )
            return Response({"name": "Album"})

    cog = object.__new__(VoiceMaster)
    ctx = SimpleNamespace(
        bot=SimpleNamespace(spotify_key="token"),
        session=Session(),
        author=SimpleNamespace(id=1),
    )
    name, tracks, total = await cog._spotify_album_tracks(  # type: ignore[arg-type]
        cast("Context", ctx),
        "https://open.spotify.com/album/1234567890123456789012",
    )

    assert name == "Album"
    assert total == 2
    assert [track.title for track in tracks] == ["Artist - First", "Artist - Second"]
    assert tracks[0].lookup == "ytsearch5:Artist First Album official audio"
    assert tracks[0].stream_url is None


@pytest.mark.asyncio
async def test_spotify_episode_resolves_to_a_lazy_youtube_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200

        async def __aenter__(self) -> "Response":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            pass

        async def json(self) -> dict[str, Any]:
            return {
                "name": "Episode title",
                "duration_ms": 123_000,
                "show": {"name": "Example Podcast"},
            }

    class Session:
        def get(self, _url: str, **_kwargs: Any) -> Response:
            return Response()

    cog = object.__new__(VoiceMaster)

    async def allow_public_url(_url: str) -> None:
        return None

    monkeypatch.setattr(voicemaster_module, "validate_public_url", allow_public_url)
    ctx = SimpleNamespace(
        bot=SimpleNamespace(spotify_key="token"),
        session=Session(),
        author=SimpleNamespace(id=1),
        message=SimpleNamespace(attachments=[], content=""),
    )
    cog._extract_info = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "url": "https://cdn.example.test/episode.opus",
            "acodec": "opus",
            "duration": 123,
        }
    )
    track = await cog._track_from_query(  # type: ignore[arg-type]
        cast("Context", ctx),
        "https://open.spotify.com/episode/1234567890123456789012",
    )

    assert track.title == "Example Podcast - Episode title"
    assert track.lookup == "ytsearch5:Example Podcast Episode title podcast"
    assert track.duration == 123
    assert track.search_title == "Episode title"
    assert track.search_artists == ("Example Podcast",)


def test_spotify_results_are_ranked_by_title_artist_album_and_duration() -> None:
    track = VoiceTrack(
        "album",
        "search",
        "PinkPantheress - Illegal",
        1,
        duration=150,
        search_title="Illegal",
        search_artists=("PinkPantheress",),
        search_album="Fancy That",
    )
    entries = [
        {
            "title": "PinkPantheress - Girl Like Me (Official Audio)",
            "channel": "PinkPantheress",
            "duration": 142,
        },
        {
            "title": "PinkPantheress - Fancy That (FULL ALBUM)",
            "channel": "PinkPantheress",
            "duration": 1223,
        },
        {
            "title": "PinkPantheress - Illegal (Official Audio)",
            "channel": "PinkPantheress",
            "duration": 150,
        },
    ]

    ranked = _rank_spotify_results(entries, track)

    assert ranked[0]["title"] == "PinkPantheress - Illegal (Official Audio)"


def test_ffmpeg_receives_safe_ytdlp_request_headers() -> None:
    track = VoiceTrack(
        "one",
        "one",
        "One",
        1,
        http_headers={
            "User-Agent": "Browser UA",
            "Accept-Language": "en-US",
            "Cookie": "must-not-be-forwarded",
        },
    )

    options = _ffmpeg_before_options(track)

    assert options is not None
    assert "-user_agent 'Browser UA'" in options
    assert "Accept-Language: en-US" in options
    assert "must-not-be-forwarded" not in options


@pytest.mark.asyncio
async def test_repeat_defaults_to_forever_and_accepts_finite_counts() -> None:
    cog = object.__new__(VoiceMaster)
    player = VoicePlayer(guild_id=123)
    cog.players = {123: player}
    cast(Any, cog)._can_control = lambda _ctx, _player: True
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(
            id=9, voice=SimpleNamespace(channel=SimpleNamespace(id=20))
        ),
        send=AsyncMock(),
    )

    await cog._repeat_impl(ctx, "")  # type: ignore[arg-type]
    assert player.repeat_mode == "one"
    assert player.repeat_remaining is None

    await cog._repeat_impl(ctx, "3")  # type: ignore[arg-type]
    assert player.repeat_mode == "one"
    assert player.repeat_remaining == 3

    with pytest.raises(commands.BadArgument, match="1,000"):
        await cog._repeat_impl(ctx, "1001")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_batch_enqueue_never_exceeds_fifty_pending_tracks() -> None:
    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def is_connected(self) -> bool:
            return True

    async def idle_worker(_player: VoicePlayer) -> None:
        await asyncio.Event().wait()

    existing = deque(
        VoiceTrack(str(index), str(index), str(index), 1)
        for index in range(MAX_QUEUE_SIZE - 1)
    )
    player = VoicePlayer(
        guild_id=123,
        voice=Voice(),  # type: ignore[arg-type]
        queue=existing,
    )
    cog = object.__new__(VoiceMaster)
    cog.players = {123: player}
    cast(Any, cog)._ensure_voice = AsyncMock(return_value=player.voice)
    cast(Any, cog)._claim_dj = lambda _ctx, _player: None
    cast(Any, cog)._run_player = idle_worker
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=1),
        channel=SimpleNamespace(),
    )
    incoming = [
        VoiceTrack("a", "a", "A", 1),
        VoiceTrack("b", "b", "B", 1),
        VoiceTrack("c", "c", "C", 1),
    ]

    _player, accepted = await cog._enqueue_many(ctx, incoming)  # type: ignore[arg-type]

    assert accepted == incoming[:1]
    assert len(player.queue) == MAX_QUEUE_SIZE
    assert player.worker is not None
    player.worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await player.worker


@pytest.mark.asyncio
async def test_idle_album_enqueue_arms_first_track_announcement() -> None:
    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def is_connected(self) -> bool:
            return True

        def is_playing(self) -> bool:
            return False

        def is_paused(self) -> bool:
            return False

    async def idle_worker(_player: VoicePlayer) -> None:
        await asyncio.Event().wait()

    player = VoicePlayer(guild_id=123, voice=Voice())  # type: ignore[arg-type]
    cog = object.__new__(VoiceMaster)
    cog.players = {123: player}
    cast(Any, cog)._ensure_voice = AsyncMock(return_value=player.voice)
    cast(Any, cog)._claim_dj = lambda _ctx, _player: None
    cast(Any, cog)._run_player = idle_worker
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=1),
        channel=SimpleNamespace(),
    )

    _player, accepted = await cog._enqueue_many(  # type: ignore[arg-type]
        cast("Context", ctx),
        [VoiceTrack("one", "one", "One", 1)],
        announce_first=True,
    )

    assert accepted
    assert player.pending_notice == ""
    assert player.worker is not None
    player.worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await player.worker


@pytest.mark.asyncio
async def test_empty_voice_channel_disconnects_and_clears_session() -> None:
    class Voice:
        channel = SimpleNamespace(members=[SimpleNamespace(bot=True)])

        def is_connected(self) -> bool:
            return True

        def is_playing(self) -> bool:
            return False

        def is_paused(self) -> bool:
            return False

        async def disconnect(self, *, force: bool = False) -> None:
            self.disconnected = force

    voice = Voice()
    player = VoicePlayer(
        guild_id=123,
        voice=voice,  # type: ignore[arg-type]
        queue=deque((VoiceTrack("one", "one", "One", 1),)),
    )
    cog = object.__new__(VoiceMaster)
    cog.players = {123: player}
    cast(Any, cog).bot = SimpleNamespace(
        logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    )

    await cog._disconnect_if_empty(123)

    assert voice.disconnected is True
    assert player.queue == deque()
    assert player.voice is None
    assert 123 not in cog.players


@pytest.mark.asyncio
async def test_worker_preserves_tracks_when_voice_is_temporarily_disconnected() -> None:
    class Voice:
        channel = SimpleNamespace(members=[])

        def is_connected(self) -> bool:
            return False

    track = VoiceTrack("one", "one", "One", 1)
    player = VoicePlayer(
        guild_id=123, voice=cast("discord.VoiceClient", Voice()), queue=deque((track,))
    )
    cog = object.__new__(VoiceMaster)

    await cog._run_player(player)

    assert list(player.queue) == [track]
    assert player.current is None


@pytest.mark.asyncio
async def test_enqueue_recovers_a_worker_stuck_after_skip() -> None:
    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def is_connected(self) -> bool:
            return True

        def is_playing(self) -> bool:
            return False

        def is_paused(self) -> bool:
            return False

    async def stuck_worker() -> None:
        await asyncio.Event().wait()

    started = asyncio.Event()

    async def replacement_worker(_player: VoicePlayer) -> None:
        started.set()

    voice = Voice()
    old_track = VoiceTrack("old", "old", "Old", 1)
    player = VoicePlayer(
        guild_id=123,
        voice=voice,  # type: ignore[arg-type]
        current=old_track,
        skip_current=True,
    )
    old_worker = asyncio.create_task(stuck_worker())
    player.worker = old_worker
    cog = object.__new__(VoiceMaster)
    cog.players = {123: player}
    cast(Any, cog)._ensure_voice = AsyncMock(return_value=voice)
    cast(Any, cog)._claim_dj = lambda _ctx, _player: None
    cast(Any, cog)._run_player = replacement_worker
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=1),
        channel=SimpleNamespace(),
    )

    await cog._enqueue(ctx, VoiceTrack("new", "new", "New", 1))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert old_worker.cancelled()
    assert started.is_set()


@pytest.mark.asyncio
async def test_idle_worker_wakes_for_a_track_queued_after_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty queue must not retire the session worker between songs."""

    class FakePcm:
        _process = SimpleNamespace(returncode=0)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    class FakeTransformer:
        def __init__(self, source: FakePcm, *, volume: float) -> None:
            self.original = source
            self.volume = volume

        def cleanup(self) -> None:
            pass

    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def __init__(self) -> None:
            self.connected = True
            self.plays = 0

        def is_connected(self) -> bool:
            return self.connected

        def play(self, _source: Any, *, after: Any) -> None:
            self.plays += 1
            asyncio.get_running_loop().call_soon(after, None)

    monkeypatch.setattr(voicemaster_module.discord, "FFmpegPCMAudio", FakePcm)
    monkeypatch.setattr(
        voicemaster_module.discord, "PCMVolumeTransformer", FakeTransformer
    )
    voice = Voice()
    first = VoiceTrack("one", "one", "One", 1, duration=0.1)
    second = VoiceTrack("two", "two", "Two", 1, duration=0.1)
    player = VoicePlayer(
        guild_id=123,
        voice=voice,  # type: ignore[arg-type]
        queue=deque((first,)),
    )
    cog = object.__new__(VoiceMaster)
    cast(Any, cog).bot = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        logger=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            debug=lambda *_args, **_kwargs: None,
        ),
    )
    cast(Any, cog)._stream_url = AsyncMock(return_value="https://example.test/audio")
    task = asyncio.create_task(cog._run_player(player))
    player.worker = task

    async def wait_for_plays(expected: int) -> None:
        while voice.plays < expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_plays(1), timeout=1)
    await asyncio.sleep(0)
    assert not task.done()

    player.queue.append(second)
    player.queue_event.set()
    await asyncio.wait_for(wait_for_plays(2), timeout=1)

    voice.connected = False
    player.queue_event.set()
    await asyncio.wait_for(task, timeout=1)
    cog._cancel_idle_disconnect(player)
    assert voice.plays == 2


@pytest.mark.asyncio
async def test_premature_ffmpeg_exit_refreshes_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePcm:
        _process = SimpleNamespace(returncode=1)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    class FakeTransformer:
        def __init__(self, source: FakePcm, *, volume: float) -> None:
            self.original = source
            self.volume = volume

        def cleanup(self) -> None:
            pass

    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def __init__(self) -> None:
            self.connected = True
            self.plays = 0

        def is_connected(self) -> bool:
            return self.connected

        def play(self, _source: Any, *, after: Any) -> None:
            self.plays += 1
            asyncio.get_running_loop().call_soon(after, None)

    class Channel:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, content: str, **_kwargs: Any) -> None:
            self.messages.append(content)

    monkeypatch.setattr(voicemaster_module.discord, "FFmpegPCMAudio", FakePcm)
    monkeypatch.setattr(
        voicemaster_module.discord, "PCMVolumeTransformer", FakeTransformer
    )
    voice = Voice()
    channel = Channel()
    track = VoiceTrack("one", "one", "One", 1, duration=180)
    player = VoicePlayer(
        guild_id=123,
        voice=voice,  # type: ignore[arg-type]
        queue=deque((track,)),
        text_channel=channel,  # type: ignore[arg-type]
    )
    cog = object.__new__(VoiceMaster)
    cast(Any, cog).bot = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        logger=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            debug=lambda *_args, **_kwargs: None,
        ),
    )
    cast(Any, cog)._stream_url = AsyncMock(
        side_effect=("https://example.test/first", "https://example.test/refreshed")
    )
    task = asyncio.create_task(cog._run_player(player))
    player.worker = task

    async def wait_for_failure() -> None:
        while not channel.messages:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_failure(), timeout=2)
    assert voice.plays == voicemaster_module.PLAYBACK_RETRY_ATTEMPTS
    assert "ended before playback started" in channel.messages[0]
    assert track.stream_url is None

    voice.connected = False
    player.queue_event.set()
    await asyncio.wait_for(task, timeout=1)
    cog._cancel_idle_disconnect(player)


@pytest.mark.asyncio
async def test_youtube_uses_ytdlp_transport_instead_of_direct_cdn_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []

    class FakeDirectPcm:
        _process = SimpleNamespace(returncode=1)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            created.append("direct")

    class FakeYtDlpPcm:
        _process = SimpleNamespace(returncode=None)

        def __init__(self, source: str) -> None:
            assert source == "https://www.youtube.com/watch?v=example"
            self.playback_started = asyncio.Event()
            created.append("yt-dlp")

    class FakeTransformer:
        def __init__(self, source: Any, *, volume: float) -> None:
            self.original = source
            self.volume = volume

        def cleanup(self) -> None:
            pass

    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def __init__(self) -> None:
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

        def play(self, source: FakeTransformer, *, after: Any) -> None:
            if isinstance(source.original, FakeDirectPcm):
                asyncio.get_running_loop().call_soon(after, None)
                return

            asyncio.get_running_loop().call_soon(source.original.playback_started.set)

            def finish() -> None:
                self.connected = False
                after(None)

            asyncio.get_running_loop().call_later(0.03, finish)

    monkeypatch.setattr(voicemaster_module, "PREMATURE_PLAYBACK_SECONDS", 0.01)
    monkeypatch.setattr(voicemaster_module.discord, "FFmpegPCMAudio", FakeDirectPcm)
    monkeypatch.setattr(voicemaster_module, "_YtDlpPipeAudio", FakeYtDlpPcm)
    monkeypatch.setattr(
        voicemaster_module.discord, "PCMVolumeTransformer", FakeTransformer
    )
    track = VoiceTrack(
        "song",
        "ytsearch1:song",
        "Song",
        1,
        duration=180,
        webpage_url="https://www.youtube.com/watch?v=example",
    )
    player = VoicePlayer(
        guild_id=123,
        voice=Voice(),  # type: ignore[arg-type]
        queue=deque((track,)),
    )
    cog = object.__new__(VoiceMaster)
    cast(Any, cog).bot = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        logger=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            debug=lambda *_args, **_kwargs: None,
        ),
    )
    cast(Any, cog)._stream_url = AsyncMock(
        return_value="https://googlevideo.example/direct"
    )

    task = asyncio.create_task(cog._run_player(player))
    player.worker = task
    await asyncio.wait_for(task, timeout=1)

    assert created == ["yt-dlp"]
    assert cast(Any, cog)._stream_url.await_count == 0


@pytest.mark.asyncio
async def test_now_playing_is_sent_after_typing_and_playback_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakePcm:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    class FakeTransformer:
        def __init__(self, source: FakePcm, *, volume: float) -> None:
            self.original = source
            self.volume = volume

        def cleanup(self) -> None:
            pass

    class Typing:
        async def __aenter__(self) -> None:
            events.append("typing-enter")

        async def __aexit__(self, *_args: Any) -> None:
            events.append("typing-exit")

    class Channel:
        def typing(self) -> Typing:
            return Typing()

        async def send(self, content: str, **_kwargs: Any) -> None:
            events.append(f"send:{content}")

    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def __init__(self) -> None:
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

        def play(self, _source: Any, *, after: Any) -> None:
            def finish() -> None:
                self.connected = False
                after(None)

            asyncio.get_running_loop().call_later(0.03, finish)

    monkeypatch.setattr(voicemaster_module, "PREMATURE_PLAYBACK_SECONDS", 0.01)
    monkeypatch.setattr(voicemaster_module.discord, "FFmpegPCMAudio", FakePcm)
    monkeypatch.setattr(
        voicemaster_module.discord, "PCMVolumeTransformer", FakeTransformer
    )
    voice = Voice()
    player = VoicePlayer(
        guild_id=123,
        voice=voice,  # type: ignore[arg-type]
        queue=deque((VoiceTrack("one", "one", "One", 1, duration=180),)),
        pending_notice="",
        text_channel=Channel(),  # type: ignore[arg-type]
    )
    cog = object.__new__(VoiceMaster)
    cast(Any, cog).bot = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        logger=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            debug=lambda *_args, **_kwargs: None,
        ),
    )
    cast(Any, cog)._stream_url = AsyncMock(return_value="https://example.test/audio")

    task = asyncio.create_task(cog._run_player(player))
    player.worker = task
    await asyncio.wait_for(task, timeout=1)

    assert events == [
        "typing-enter",
        "typing-exit",
        "send:Now playing **One**\n-# \\- *Queued by <@1>* | 3:00",
    ]


def test_non_dj_skip_requires_more_than_seventy_percent_of_humans() -> None:
    class Voice:
        channel = SimpleNamespace(
            members=[
                SimpleNamespace(id=1, bot=False),
                SimpleNamespace(id=2, bot=False),
                SimpleNamespace(id=3, bot=False),
                SimpleNamespace(id=4, bot=False),
                SimpleNamespace(id=99, bot=True),
            ]
        )

    player = VoicePlayer(guild_id=123, voice=Voice())  # type: ignore[arg-type]
    player.skip_votes.update({1, 2})
    cog = object.__new__(VoiceMaster)

    assert cog._skip_vote_requirement(player) == (2, 3)
    player.skip_votes.add(3)
    assert cog._skip_vote_requirement(player) == (3, 3)


def test_owner_and_voice_moderation_permissions_are_privileged_djs() -> None:
    cog = object.__new__(VoiceMaster)
    guild = SimpleNamespace(owner_id=10)
    owner_ctx = SimpleNamespace(
        guild=guild,
        author=SimpleNamespace(id=10, guild_permissions=SimpleNamespace()),
    )
    assert cog._is_privileged_dj(cast("Context", owner_ctx))

    moderator_ctx = SimpleNamespace(
        guild=SimpleNamespace(owner_id=11),
        author=SimpleNamespace(
            id=12,
            guild_permissions=SimpleNamespace(
                move_members=True,
                manage_guild=False,
                ban_members=False,
                kick_members=False,
            ),
        ),
    )
    assert cog._is_privileged_dj(cast("Context", moderator_ctx))


@pytest.mark.asyncio
async def test_privileged_skip_can_run_from_any_text_channel() -> None:
    class Voice:
        channel = SimpleNamespace(id=20, members=[])

        def is_connected(self) -> bool:
            return True

        def is_playing(self) -> bool:
            return True

        def is_paused(self) -> bool:
            return False

        def stop(self) -> None:
            self.stopped = True

    player = VoicePlayer(guild_id=123, voice=Voice())  # type: ignore[arg-type]
    cog = object.__new__(VoiceMaster)
    cog.players = {123: player}
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123, owner_id=999),
        author=SimpleNamespace(
            id=12,
            voice=SimpleNamespace(channel=SimpleNamespace(id=20)),
            guild_permissions=SimpleNamespace(manage_guild=True),
        ),
    )

    voice = require_type(player.voice, Voice)
    voice.stopped = False
    await cog._skip_impl(ctx)  # type: ignore[arg-type]
    assert voice.stopped is True


@pytest.mark.asyncio
async def test_queue_attachment_is_added_instead_of_showing_the_queue() -> None:
    """Queue should treat an attached video as a playable audio source."""

    cog = object.__new__(VoiceMaster)
    cog._play_impl = AsyncMock()  # type: ignore[method-assign]
    attachment_url = "https://cdn.example.test/video.mp4"
    ctx = SimpleNamespace(
        message=SimpleNamespace(
            attachments=[SimpleNamespace(url=attachment_url)],
            content="fish queue",
        )
    )

    await cog._queue_impl(cast("Context", ctx), "")

    cog._play_impl.assert_awaited_once_with(ctx, attachment_url, require_control=False)
