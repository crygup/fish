from __future__ import annotations

from typing import Any, cast

import pytest

from utils.google import google_json


class _Response:
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self.status = status
        self.reason = ""
        self._data = data

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> dict[str, Any]:
        return self._data


class _Session:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def get(self, _url: str, *, params: dict[str, Any]) -> _Response:
        key = str(params["key"])
        self.keys.append(key)
        return _Response(
            429 if key == "exhausted" else 200,
            {"items": ["ok"]} if key == "working" else {},
        )


@pytest.mark.asyncio
async def test_google_json_tries_another_key_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("utils.google.random.shuffle", lambda values: None)
    session = _Session()

    result = await google_json(
        cast(Any, session),
        "https://www.googleapis.com/test",
        keys=["exhausted", "working"],
    )

    assert result == {"items": ["ok"]}
    assert session.keys == ["exhausted", "working"]
