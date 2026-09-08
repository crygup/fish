from __future__ import annotations

from extensions.voicemaster import (
    VoiceTrack,
    _ffmpeg_before_options,
    _public_error,
)


def test_extractor_errors_do_not_expose_tracebacks() -> None:
    error = """Traceback (most recent call last):
  File \"ytdlp_safe.py\", line 1, in <module>
    main()
yt_dlp.utils.DownloadError: ERROR: [youtube] abc: Sign in to confirm
"""

    assert _public_error(error) == ("ERROR: [youtube] abc: Sign in to confirm")


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
