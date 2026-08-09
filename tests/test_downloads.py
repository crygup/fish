from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from utils.downloads import (
    _extract_html_media_urls,
    _extract_threads_json_media_urls,
    download_format_selector,
    is_downloadable_media_page,
    normalize_download_site,
    record_download,
)
from utils.regexes import VIDEOS_RE


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


def test_direct_and_unapproved_urls_do_not_use_the_page_downloader() -> None:
    assert not is_downloadable_media_page("https://static.klipy.com/example/video.mp4")
    assert not is_downloadable_media_page("https://example.com/watch/123")
    assert not is_downloadable_media_page("file:///etc/passwd")


def test_twitter_selector_falls_back_when_gif_dimensions_are_unknown() -> None:
    selector = download_format_selector(
        "https://x.com/sh1n__0/status/2081845129573474683/",
        "mp4",
        res_target=1080,
    )

    assert selector == ("bestvideo[height<=1080]+bestaudio/" "best[height<=1080]/best")


def test_tiktok_selector_falls_back_when_dimensions_are_unavailable() -> None:
    selector = download_format_selector(
        "https://www.tiktok.com/@example/video/1234567890123456789",
        "mp4",
        res_target=720,
    )

    assert selector == "bestvideo[height<=720]+bestaudio/best[height<=720]/best"


def test_auto_download_keeps_the_tiktok_video_id() -> None:
    url = "https://www.tiktok.com/@example/video/1234567890123456789"
    match = VIDEOS_RE.search(url)

    assert match is not None
    assert match.group(0) == url


@pytest.mark.parametrize(
    "url",
    (
        "https://www.reddit.com/r/videos/comments/abc123/example/",
        "https://redd.it/abc123",
        "https://www.threads.net/@example/post/abc123",
        "https://www.threads.com/share/_u7pEM-wd/",
        "https://www.facebook.com/reel/123456",
        "https://www.pixiv.net/en/artworks/123456",
        "https://example.tumblr.com/post/123456/example",
    ),
)
def test_auto_download_recognizes_new_sites(url: str) -> None:
    match = VIDEOS_RE.search(url)
    assert match is not None
    assert match.group(0) == url


def test_audio_selector_is_unchanged() -> None:
    assert (
        download_format_selector(
            "https://soundcloud.com/example/track",
            "mp4",
            res_target=1080,
        )
        == "bestaudio/best"
    )


def test_download_sites_group_mirror_domains() -> None:
    assert normalize_download_site("https://x.com/user/status/1") == "twitter"
    assert normalize_download_site("https://vxtwitter.com/user/status/1") == "twitter"
    assert normalize_download_site("https://fxtwitter.com/user/status/1") == "twitter"
    assert normalize_download_site("https://www.youtube.com/watch?v=1") == "youtube"
    assert normalize_download_site("https://static.klipy.com/file.mp4") == "klipy"
    assert (
        normalize_download_site("https://old.reddit.com/r/pics/comments/abc123")
        == "reddit"
    )
    assert (
        normalize_download_site("https://www.threads.com/@example/post/abc123")
        == "threads"
    )
    assert normalize_download_site("https://m.facebook.com/reel/123456") == "facebook"
    assert (
        normalize_download_site("https://www.pixiv.net/en/artworks/123456") == "pixiv"
    )
    assert normalize_download_site("https://artist.tumblr.com/post/123456") == "tumblr"
    assert normalize_download_site("https://example.com/file.mp4") is None


def test_html_media_extraction_prefers_video_media() -> None:
    html = """
    <meta property="og:image" content="https://cdn.example/image.jpg">
    <meta property="og:video" content="https://cdn.example/video.mp4">
    <video><source src="https://cdn.example/video-2.mp4"></video>
    """
    assert _extract_html_media_urls(html, "https://threads.net/@user/post/1") == [
        "https://cdn.example/video-2.mp4",
        "https://cdn.example/video.mp4",
    ]


def test_threads_json_media_extraction_keeps_each_carousel_attachment() -> None:
    html = """
    <script type="application/json">
    {"data": {"media": {"carousel_media": [
      {"video_versions": [{"url": "https:\\/\\/scontent.example.cdninstagram.com\\/one.mp4"}]},
      {"image_versions2": {"candidates": [{"url": "https:\\/\\/scontent.example.cdninstagram.com\\/two.gif"}]}}
    ]}}}
    </script>
    """
    assert _extract_threads_json_media_urls(
        html, "https://www.threads.com/@example/post/1"
    ) == [
        "https://scontent.example.cdninstagram.com/one.mp4",
        "https://scontent.example.cdninstagram.com/two.gif",
    ]


def test_html_media_extraction_ignores_escaped_loading_assets() -> None:
    html = """
    <meta property="og:image" content="https://cdn.example/post.jpg">
    <script>
      {"loading":"https:\\/\\/static.cdninstagram.com\\/spinner.gif"}
    </script>
    """
    assert _extract_html_media_urls(
        html, "https://www.threads.com/@example/post/1"
    ) == ["https://cdn.example/post.jpg"]


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


@pytest.mark.asyncio
async def test_record_download_marks_auto_downloads() -> None:
    calls: list[tuple[object, ...]] = []

    class Pool:
        async def execute(self, _sql: str, *args: object) -> None:
            calls.append(args)

    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=99),
            channel=SimpleNamespace(id=123),
            bot=SimpleNamespace(
                db_cache=SimpleNamespace(
                    user_tracking_opted_out=lambda *_args: False,
                ),
                pool=Pool(),
                logger=SimpleNamespace(exception=lambda *_args: None),
            ),
        ),
    )
    await record_download(
        ctx, "https://www.instagram.com/reel/example/", auto_download=True
    )
    assert calls == [
        (42, "instagram", True),
        (42, 99, 123, "instagram", True),
    ]
