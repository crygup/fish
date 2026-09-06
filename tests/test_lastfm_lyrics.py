from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from test_support import require_type

# Test doubles supply only the Discord/service fields exercised by each test.
from core import Fishie
from extensions.context import Context
from extensions.lastfm.lyrics import Lyrics, LyricsTrack, lyric_pages
from extensions.lastfm.top import (
    Top,
    TrackListPageSource,
    _sorted_tracks,
)
from extensions.search.spotify import Spotify


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self, **_kwargs: Any) -> Any:
        return self.payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _recent_track() -> dict[str, Any]:
    return {
        "recenttracks": {
            "track": [
                {
                    "name": "Song",
                    "artist": {"#text": "Artist"},
                    "album": {"#text": "Album"},
                }
            ]
        }
    }


def test_lyric_pages_never_include_more_than_two_sections() -> None:
    pages = lyric_pages(
        "[Verse 1]\nFirst\n\n[Chorus]\nSecond\n\n"
        "[Verse 2]\nThird\n\n[Bridge]\nFourth\n\n[Outro]\nFifth"
    )

    assert len(pages) == 3
    assert pages[0].count("\n\n") == 1
    assert pages[1].count("\n\n") == 1
    assert "[Outro]" in pages[2]


@pytest.mark.asyncio
async def test_lrclib_exact_lookup_falls_back_to_search() -> None:
    session = _Session(
        [
            _Response(404, {}),
            _Response(
                200,
                [
                    {
                        "trackName": "Song",
                        "artistName": "Artist",
                        "plainLyrics": "Verse",
                    }
                ],
            ),
        ]
    )
    ctx = cast(Context, SimpleNamespace(session=session))
    lyrics = Lyrics()

    result = await lyrics._lrclib_result(
        ctx,
        LyricsTrack(title="Song", artist="Artist", album="Album"),
        exact=True,
    )

    assert result["plainLyrics"] == "Verse"
    assert session.calls[0][0].endswith("/get")
    assert session.calls[0][1]["params"]["track_name"] == "Song"
    assert session.calls[1][0].endswith("/search")
    assert session.calls[1][1]["params"] == {"q": "Artist Song"}


@pytest.mark.asyncio
async def test_spotify_blank_and_mentioned_users_use_lastfm_track() -> None:
    calls: list[dict[str, Any]] = []

    async def lfm_get(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return _recent_track()

    bot = SimpleNamespace(
        db_cache=SimpleNamespace(
            lastfm={111111111111111111: "self", 222222222222222222: "other"}
        ),
        lfm_get=lfm_get,
    )
    ctx = cast(
        Context,
        SimpleNamespace(author=SimpleNamespace(id=111111111111111111)),
    )
    spotify = Spotify(cast("Fishie", bot))

    own = await spotify._spotify_query(ctx, "track", None)
    other = await spotify._spotify_query(ctx, "album", "<@222222222222222222>")

    assert own == 'track:"Song" artist:"Artist"'
    assert other == 'album:"Album" artist:"Artist"'
    assert [call["user"] for call in calls] == ["self", "other"]


@pytest.mark.asyncio
async def test_lastfm_track_lists_use_user_playcounts_and_paginate() -> None:
    calls: list[dict[str, Any]] = []
    playcounts = {"First": "2", "Second": "9"}

    async def lfm_get(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return {
            "track": {
                "name": params["track"],
                "url": f"https://last.fm/{params['track']}",
                "userplaycount": playcounts[params["track"]],
            }
        }

    top = Top()
    top.bot = cast("Fishie", SimpleNamespace(lfm_get=lfm_get))
    tracks = await top._user_track_playcounts(
        [{"name": "First"}, {"name": "Second"}],
        "Artist",
        "listener",
    )
    ordered = _sorted_tracks(tracks)
    source = TrackListPageSource(
        heading="Tracks by Artist",
        subtitle="Sorted by your playcount",
        tracks=ordered + [{"name": f"Track {index}"} for index in range(9)],
    )

    assert [track["name"] for track in ordered] == ["Second", "First"]
    assert all(call["username"] == "listener" for call in calls)
    assert source.get_max_pages() == 2
    text = "\n".join(
        str(require_type(item, discord.ui.TextDisplay).content)
        for item in source.format_page(0)
        if hasattr(item, "content")
    )
    assert "Second" in text


def test_track_list_compact_positive_counts():
    tracks = _sorted_tracks(
        [
            {"name": "None", "playcount": 0},
            {"name": "Once", "playcount": 1},
            {"name": "Twice", "playcount": 2},
        ]
    )
    source = TrackListPageSource(
        heading="listener's top tracks for Artist", subtitle="", tracks=tracks
    )
    text = "\n".join(
        str(require_type(item, discord.ui.TextDisplay).content)
        for item in source.format_page(0)
        if hasattr(item, "content")
    )
    assert "1. **Twice** - 2 plays\n2. **Once** - 1 play" in text
    assert "2 total tracks" in text
    assert "Last.fm" not in text
    assert "None" not in text


async def test_track_range_counts_scrobbles_and_filters_album():
    calls = []

    async def lfm_get(params):
        calls.append(params)
        return {
            "recenttracks": {
                "@attr": {"totalPages": "1"},
                "track": [
                    {
                        "name": "Song",
                        "artist": {"#text": "Artist"},
                        "album": {"#text": "Album"},
                        "date": {"uts": "1"},
                    },
                    {
                        "name": "Song",
                        "artist": {"#text": "artist"},
                        "album": {"#text": "album"},
                        "date": {"uts": "2"},
                    },
                    {
                        "name": "Live",
                        "artist": {"#text": "Artist"},
                        "album": {"#text": "Album"},
                    },
                    {
                        "name": "Other",
                        "artist": {"#text": "Artist"},
                        "album": {"#text": "Other"},
                        "date": {"uts": "3"},
                    },
                ],
            }
        }

    top = Top()
    top.bot = cast("Fishie", SimpleNamespace(lfm_get=lfm_get))
    tracks = await top._range_tracks(
        "listener", "Artist", "2026-08-01 to 2026-08-31", "Album"
    )
    assert tracks == [{"name": "Song", "playcount": 2}]
    assert calls[0]["to"] - calls[0]["from"] == 31 * 86400 - 1


@pytest.mark.parametrize(
    "query",
    [
        "weekly Artist <@123456789012345678>",
        "<@123456789012345678> Artist weekly",
        "Artist weekly <@123456789012345678>",
    ],
)
def test_dynamic_track_arguments(query):
    assert Top()._track_arguments(query, "overall", 1) == (
        "Artist",
        "7day",
        123456789012345678,
    )


@pytest.mark.parametrize(
    "period,expected",
    [
        ("alltime", "overall"),
        ("hourly", "hourly"),
        ("daily", "daily"),
        ("monthly", "1month"),
        ("biyearly", "24month"),
    ],
)
def test_track_timeframes(period, expected):
    assert Top()._track_arguments(period, "overall", 1) == ("", expected, 1)


async def test_artist_top100_stops_and_caches():
    from extensions.lastfm.top import ARTIST_TRACK_CACHE

    ARTIST_TRACK_CACHE.clear()
    calls = []

    async def lfm_get(params):
        calls.append(params)
        return {
            "toptracks": {
                "@attr": {"totalPages": "26"},
                "track": [
                    {
                        "name": f"Track {i}",
                        "artist": {"name": "Test Artist"},
                        "playcount": 200 - i,
                    }
                    for i in range(100)
                ],
            }
        }

    top = Top()
    top.bot = cast("Fishie", SimpleNamespace(lfm_get=lfm_get))
    first = await top._artist_user_tracks("test-listener", "Test Artist")
    second = await top._artist_user_tracks("test-listener", "Test Artist")
    assert len(first) == 100
    assert first == second
    assert len(calls) == 1
    ARTIST_TRACK_CACHE.clear()
