from __future__ import annotations

from utils.downloads import download_format_selector, is_downloadable_media_page


def test_supported_media_pages_use_the_guarded_downloader() -> None:
    assert is_downloadable_media_page("https://www.youtube.com/watch?v=example")
    assert is_downloadable_media_page("https://www.instagram.com/reel/example/")
    assert is_downloadable_media_page("https://x.com/example/status/123")


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


def test_regular_video_selector_keeps_the_resolution_limit() -> None:
    selector = download_format_selector(
        "https://www.tiktok.com/@example/video/1234567890123456789",
        "mp4",
        res_target=720,
    )

    assert selector == "bestvideo[height<=720]+bestaudio/best[height<=720]"


def test_audio_selector_is_unchanged() -> None:
    assert (
        download_format_selector(
            "https://soundcloud.com/example/track",
            "mp4",
            res_target=1080,
        )
        == "bestaudio/best"
    )
