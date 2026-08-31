from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

import extensions.moderation.logger as logger_module
from extensions.moderation.logger import Logger, _ChannelPositionChange


def _guild(*, view_audit_log: bool = True) -> Any:
    return SimpleNamespace(
        id=939497177821110272,
        me=SimpleNamespace(
            guild_permissions=SimpleNamespace(view_audit_log=view_audit_log)
        ),
    )


def _change(guild: Any, channel_id: int, before: int, after: int) -> Any:
    return _ChannelPositionChange(
        guild=guild,
        channel_id=channel_id,
        channel_label=f"<#{channel_id}>",
        before_position=before,
        after_position=after,
    )


@pytest.mark.asyncio
async def test_channel_position_batch_is_suppressed_without_audit_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = _guild()
    logger = Logger()
    logger.bot = cast(
        Any,
        SimpleNamespace(
            logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        ),
    )
    logger._channel_move_batches = {
        guild.id: [_change(guild, 10, 5, 2), _change(guild, 11, 6, 3)]
    }
    logger._channel_move_tasks = {}
    emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def emit(*args: Any, **kwargs: Any) -> None:
        emitted.append((args, kwargs))

    async def no_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    logger._emit_logger = emit  # type: ignore[method-assign]
    logger._recent_audit_entry = no_audit  # type: ignore[method-assign]
    monkeypatch.setattr(logger_module, "CHANNEL_MOVE_BATCH_DELAY", 0)

    await logger._flush_channel_position_updates(guild.id)

    assert emitted == []


@pytest.mark.asyncio
async def test_channel_position_batch_emits_when_audit_entry_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = _guild()
    logger = Logger()
    logger.bot = cast(
        Any,
        SimpleNamespace(
            logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        ),
    )
    logger._channel_move_batches = {
        guild.id: [_change(guild, 10, 5, 2), _change(guild, 11, 6, 3)]
    }
    logger._channel_move_tasks = {}
    emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def emit(*args: Any, **kwargs: Any) -> None:
        emitted.append((args, kwargs))

    async def matching_audit(*_args: Any, **_kwargs: Any) -> object:
        return object()

    logger._emit_logger = emit  # type: ignore[method-assign]
    logger._recent_audit_entry = matching_audit  # type: ignore[method-assign]
    monkeypatch.setattr(logger_module, "CHANNEL_MOVE_BATCH_DELAY", 0)

    await logger._flush_channel_position_updates(guild.id)

    assert len(emitted) == 1
    assert emitted[0][0][1] == "channel"
    assert emitted[0][1]["audit_target_ids"] == {10, 11}
