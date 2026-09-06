from __future__ import annotations

from types import SimpleNamespace

# Test doubles supply only the Discord/service fields exercised by each test.
from typing import cast

import discord

from extensions.events.auto_upload import (
    AutoUpload,
    _is_media_candidate_url,
    _media_type,
)


def test_auto_upload_media_types() -> None:
    assert _media_type(filename="clip.gif") == "gifs"
    assert _media_type(filename="clip.mp4") == "videos"
    assert _media_type(filename="clip.png") == "images"
    assert _media_type(content_type="video/mp4") == "videos"
    assert _media_type(content_type="image/gif") == "gifs"


def test_auto_upload_accepts_direct_media_and_supported_pages() -> None:
    assert _is_media_candidate_url("https://cdn.example.test/file.gif")
    assert _is_media_candidate_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert not _is_media_candidate_url("https://example.test/article")


def test_auto_upload_collects_attachments_and_content_without_duplicates() -> None:
    attachment = SimpleNamespace(
        id=123,
        url="https://cdn.example.test/photo.png?token=one",
        filename="photo.png",
        content_type="image/png",
    )
    message = SimpleNamespace(
        attachments=[attachment],
        embeds=[],
        stickers=[],
        components=[],
        message_snapshots=[],
        content="https://cdn.example.test/photo.png?token=two",
    )

    entries = AutoUpload._collect(cast("discord.Message", message))
    assert len(entries) == 1
    assert entries[0][0] == "attachment"


def test_auto_upload_prefers_a_link_over_its_discord_preview() -> None:
    message = SimpleNamespace(
        attachments=[],
        stickers=[],
        components=[],
        message_snapshots=[],
        content="https://www.youtube.com/watch?v=abcdefghijk",
        embeds=[
            SimpleNamespace(
                image=SimpleNamespace(
                    url="https://cdn.example.test/preview.mp4?token=one"
                ),
                thumbnail=None,
            )
        ],
    )

    entries = AutoUpload._collect(cast("discord.Message", message))
    assert entries == [("url", "https://www.youtube.com/watch?v=abcdefghijk")]


def test_auto_upload_rolling_counts_prune_old_entries() -> None:
    event = object.__new__(AutoUpload)
    assert event._record_attempts((1, 2), 10, 100.0) == (10, 10)
    assert event._record_attempts((1, 2), 5, 120.0) == (15, 15)
    assert event._record_attempts((1, 2), 1, 500.0) == (1, 1)
