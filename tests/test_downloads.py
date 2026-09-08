from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from utils.downloads import (
    DIRECT_MEDIA_HOSTS,
    is_downloadable_media_page,
    record_download,
)


def test_supported_media_pages_use_the_guarded_downloader() -> None:
    assert is_downloadable_media_page("https://www.youtube.com/watch?v=example")
    assert is_downloadable_media_page("https://www.instagram.com/reel/example/")
    assert is_downloadable_media_page("https://x.com/example/status/123")
    assert is_downloadable_media_page(
        "https://www.reddit.com/r/videos/comments/abc123/example/"
    )
    assert is_downloadable_media_page("https://www.threads.net/@example/post/abc123")
    assert is_downloadable_media_page("https://www.facebook.com/reel/123456")
    assert is_downloadable_media_page("https://www.pixiv.net/en/artworks/123456")
    assert is_downloadable_media_page("https://example.tumblr.com/post/123456/example")
    assert is_downloadable_media_page("https://kkinstagram.com/p/DbOiEnrE-bV/")
    assert is_downloadable_media_page(
        "https://kktiktok.com/@example/video/1234567890123456789"
    )
    assert is_downloadable_media_page("https://www.kkinstagram.com/p/example/")
    assert is_downloadable_media_page(
        "https://www.kktiktok.com/@example/video/1234567890123456789"
    )


def test_direct_and_unapproved_urls_do_not_use_the_page_downloader() -> None:
    assert not is_downloadable_media_page("https://static.klipy.com/example/video.mp4")
    assert not is_downloadable_media_page(
        "https://media1.tenor.com/m/example/example.gif"
    )
    assert "media1.tenor.com" in DIRECT_MEDIA_HOSTS
    assert not is_downloadable_media_page("https://example.com/watch/123")
    assert not is_downloadable_media_page("file:///etc/passwd")


def test_all_yt_dlp_invocations_use_the_guarded_entry_point() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src/utils/downloads.py"
    ).read_text()
    assert '"utils.ytdlp_safe"' in source
    assert '"yt_dlp",' not in source


@pytest.mark.asyncio
async def test_record_download_stores_site_only_and_respects_opt_out() -> None:
    calls: list[tuple[object, ...]] = []

    class Cache:
        def __init__(self, opted_out: bool) -> None:
            self.opted_out = opted_out

        def user_tracking_opted_out(self, _user_id: int, item: str) -> bool:
            assert item == "downloads"
            return self.opted_out

    class Pool:
        async def execute(self, _sql: str, *args: object) -> None:
            calls.append(args)

    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=42),
            bot=SimpleNamespace(
                db_cache=Cache(False),
                pool=Pool(),
                logger=SimpleNamespace(exception=lambda *_args: None),
            ),
        ),
    )
    await record_download(ctx, "https://vxtwitter.com/example/status/1")
    assert calls == [(42, "twitter", False)]

    ctx.bot.db_cache = Cache(True)
    await record_download(ctx, "https://x.com/example/status/2")
    assert calls == [(42, "twitter", False)]
