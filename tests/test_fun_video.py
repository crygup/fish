from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import extensions.fun.video as video_module
from extensions.fun.video import VideoCommands


def _video_cog(pool: object) -> VideoCommands:
    cog = VideoCommands()
    cog.bot = cast(
        Any,
        SimpleNamespace(
            pool=pool,
            embedcolor=0x123456,
            config={"ids": {"owner_id": "1"}},
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        ),
    )
    return cog


def test_video_upload_rejects_gif_media() -> None:
    assert not VideoCommands._is_video(  # type: ignore[arg-type]
        SimpleNamespace(filename="animation.gif", content_type="video/mp4")
    )
    assert not VideoCommands._is_video(  # type: ignore[arg-type]
        SimpleNamespace(filename="animation.mp4", content_type="image/gif")
    )
    assert not VideoCommands._is_video_response("video/mp4", "animation.gif")
    assert VideoCommands._is_gif_data(b"GIF89a\x01\x00")


@pytest.mark.asyncio
async def test_current_video_url_refreshes_discord_attachment() -> None:
    pool = SimpleNamespace(execute=AsyncMock())
    cog = _video_cog(pool)
    message = SimpleNamespace(
        attachments=[SimpleNamespace(url="https://cdn.discordapp.com/fresh.mp4")]
    )
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    cog._video_review_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    result = await cog._current_video_url(
        upload_id=42,
        source_url="https://cdn.discordapp.com/expired.mp4",
        review_message_id=99,
    )

    assert result == "https://cdn.discordapp.com/fresh.mp4"
    pool.execute.assert_awaited_once_with(
        "UPDATE video_uploads SET source_url = $2 WHERE id = $1",
        42,
        "https://cdn.discordapp.com/fresh.mp4",
    )


@pytest.mark.asyncio
async def test_random_video_uses_count_and_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pool:
        async def fetchval(self, query: str) -> int:
            assert "COUNT(*)" in query
            return 3

        async def fetchrow(self, query: str, offset: int) -> dict[str, object]:
            assert "ORDER BY random()" not in query
            assert "OFFSET $1" in query
            assert offset == 1
            return {
                "id": 8,
                "source_url": "https://cdn.discordapp.com/video.mp4",
                "filename": "video.mp4",
                "review_message_id": 88,
            }

    cog = _video_cog(Pool())
    cog._current_video_url = AsyncMock(  # type: ignore[method-assign]
        return_value="https://cdn.discordapp.com/fresh.mp4"
    )
    monkeypatch.setattr(video_module.secrets, "randbelow", lambda _count: 1)
    ctx = SimpleNamespace(send=AsyncMock())

    await cog._send_video(cast(Any, ctx))

    cog._current_video_url.assert_awaited_once_with(
        upload_id=8,
        source_url="https://cdn.discordapp.com/video.mp4",
        review_message_id=88,
    )
    ctx.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_all_keeps_rows_whose_messages_cannot_be_removed() -> None:
    class Pool:
        def __init__(self) -> None:
            self.execute = AsyncMock(return_value="DELETE 1")

        async def fetch(self, _query: str, *_args: object) -> list[dict[str, int]]:
            return [
                {"id": 1, "library_id": 1, "review_message_id": 10},
                {"id": 2, "library_id": 2, "review_message_id": 20},
            ]

    pool = Pool()
    cog = _video_cog(pool)
    cog._require_library_manager = AsyncMock()  # type: ignore[method-assign]
    cog._delete_review_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=(True, False)
    )
    ctx = SimpleNamespace(
        send=AsyncMock(),
        prompt=AsyncMock(return_value=object()),
        author=SimpleNamespace(id=1),
    )

    await VideoCommands.video_delete.callback(cog, cast(Any, ctx), identifier="all")

    assert pool.execute.await_count == 2
    tombstone_call, delete_call = pool.execute.await_args_list
    assert tombstone_call.args[-1] == [1]
    assert delete_call.args[-1] == [1]
    ctx.send.assert_awaited_once()
    response = ctx.send.await_args.args[0]
    assert "Deleted 1 approved video" in response
    assert "Kept 1 video" in response


@pytest.mark.asyncio
async def test_delete_one_removes_review_message_before_database_row() -> None:
    events: list[str] = []

    class Pool:
        async def fetchrow(self, _query: str, upload_id: int) -> dict[str, int]:
            assert upload_id == 7
            return {
                "id": 7,
                "library_id": 7,
                "uploader_id": 22,
                "review_message_id": 70,
            }

        async def execute(self, _query: str, upload_id: int) -> str:
            assert upload_id == 7
            events.append("database")
            return "DELETE 1"

    async def delete_message(message_id: int | None) -> bool:
        assert message_id == 70
        events.append("message")
        return True

    cog = _video_cog(Pool())
    cog._require_library_manager = AsyncMock()  # type: ignore[method-assign]
    cog._delete_review_message = delete_message  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        send=AsyncMock(),
        prompt=AsyncMock(return_value=object()),
        author=SimpleNamespace(id=1),
    )

    await VideoCommands.video_delete.callback(cog, cast(Any, ctx), identifier="7")

    assert events == ["message", "database", "database"]
    assert ctx.send.await_args.args[0] == "Deleted that approved video."


@pytest.mark.asyncio
async def test_delete_one_rejects_video_owned_by_another_user() -> None:
    class Pool:
        async def fetchrow(self, _query: str, upload_id: int) -> dict[str, int]:
            assert upload_id == 7
            return {
                "id": 7,
                "library_id": 7,
                "uploader_id": 22,
                "review_message_id": 70,
            }

    cog = _video_cog(Pool())
    cog._delete_review_message = AsyncMock()  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        send=AsyncMock(), prompt=AsyncMock(), author=SimpleNamespace(id=33)
    )

    await VideoCommands.video_delete.callback(cog, cast(Any, ctx), identifier="7")

    cog._delete_review_message.assert_not_awaited()
    ctx.prompt.assert_not_awaited()
    assert (
        ctx.send.await_args.args[0] == "You can only delete videos that you uploaded."
    )
