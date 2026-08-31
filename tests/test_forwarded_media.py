from types import SimpleNamespace

import pytest

from extensions.fun.post import PostCommands
from extensions.fun.video import VideoCommands
from utils.converters import MediaConverter


def _attachment(url: str, filename: str, content_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=hash(url),
        url=url,
        filename=filename,
        content_type=content_type,
    )


def test_media_converter_reads_forwarded_snapshot_attachments() -> None:
    url = "https://cdn.discordapp.com/forwarded/photo.png"
    message = SimpleNamespace(
        attachments=[],
        embeds=[],
        components=[],
        stickers=[],
        message_snapshots=[
            SimpleNamespace(
                attachments=[_attachment(url, "photo.png", "image/png")],
                embeds=[],
                components=[],
                stickers=[],
            )
        ],
    )

    assert MediaConverter._message_media_url(message) == url
    assert getattr(MediaConverter._message_attachments(message)[0], "url") == url


def test_media_converter_reads_raw_forwarded_snapshot_payload() -> None:
    url = "https://cdn.discordapp.com/forwarded/clip.mp4"
    message = {
        "attachments": [],
        "message_snapshots": [
            {
                "message": {
                    "attachments": [
                        {
                            "id": "123",
                            "filename": "clip.mp4",
                            "content_type": "video/mp4",
                            "url": url,
                        }
                    ]
                }
            }
        ],
    }

    assert MediaConverter._message_media_url(message) == url
    assert (
        MediaConverter._attachment_url(
            message["message_snapshots"][0]["message"]["attachments"][0]
        )
        == url
    )


@pytest.mark.asyncio
async def test_reply_video_sources_include_forwarded_video_attachments() -> None:
    url = "https://cdn.discordapp.com/forwarded/clip.mp4"
    attachment = _attachment(url, "clip.mp4", "video/mp4")
    replied = SimpleNamespace(
        attachments=[],
        message_snapshots=[SimpleNamespace(attachments=[attachment])],
    )

    attachments, media_url = await VideoCommands()._reply_video_sources(
        SimpleNamespace(), replied  # type: ignore[arg-type]
    )

    assert attachments == [attachment]
    assert media_url is None


@pytest.mark.asyncio
async def test_reply_post_sources_include_all_forwarded_attachments() -> None:
    first = _attachment(
        "https://cdn.discordapp.com/forwarded/one.png", "one.png", "image/png"
    )
    second = _attachment(
        "https://cdn.discordapp.com/forwarded/two.gif", "two.gif", "image/gif"
    )
    replied = SimpleNamespace(
        attachments=[],
        message_snapshots=[SimpleNamespace(attachments=[first, second])],
    )

    attachments, media_urls = await PostCommands()._reply_post_sources(
        SimpleNamespace(), replied  # type: ignore[arg-type]
    )

    assert attachments == [first, second]
    assert media_urls == []
