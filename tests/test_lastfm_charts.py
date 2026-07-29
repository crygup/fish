from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from extensions.context import Context
from extensions.lastfm.charts import (
    SPOTIFY_COVER_CACHE,
    _lastfm_artist_name,
    search_spotify,
)


class _Response:
    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self.payload


def _context(payload: dict[str, Any]) -> Context:
    session = SimpleNamespace(get=lambda *_args, **_kwargs: _Response(payload))
    return cast(
        Context,
        SimpleNamespace(
            session=session,
            bot=SimpleNamespace(spotify_key="token"),
        ),
    )


def test_spotify_track_cover_requires_exact_track_and_artist_match() -> None:
    SPOTIFY_COVER_CACHE.clear()
    payload = {
        "tracks": {
            "items": [
                {
                    "name": "Same Track",
                    "artists": [{"name": "Wrong Artist"}],
                    "album": {"images": [{"url": "https://wrong"}]},
                },
                {
                    "name": "Same Track",
                    "artists": [{"name": "Right Artist"}],
                    "album": {"images": [{"url": "https://right"}]},
                },
            ]
        }
    }

    result = asyncio.run(
        search_spotify(
            _context(payload),
            "track",
            "Same Track",
            "Right Artist",
        )
    )

    assert result == "https://right"


def test_spotify_track_cover_skips_results_without_exact_match() -> None:
    SPOTIFY_COVER_CACHE.clear()
    payload = {
        "tracks": {
            "items": [
                {
                    "name": "Different Track",
                    "artists": [{"name": "Right Artist"}],
                    "album": {"images": [{"url": "https://wrong"}]},
                }
            ]
        }
    }

    result = asyncio.run(
        search_spotify(
            _context(payload),
            "track",
            "Expected Track",
            "Right Artist",
        )
    )

    assert result is None


def test_lastfm_artist_name_handles_track_artist_objects() -> None:
    assert _lastfm_artist_name({"artist": {"name": "Artist"}}) == "Artist"
    assert _lastfm_artist_name({"artist": {"#text": "Artist"}}) == "Artist"
