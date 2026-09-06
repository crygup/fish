from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

import utils.ytdlp_safe as ytdlp_safe


def test_provider_exception_is_scoped_to_trusted_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "http://provider:4416"
    monkeypatch.setattr(ytdlp_safe, "_POT_PROVIDER", ("http", "provider", 4416))

    def validate(url: str) -> str:
        parsed = urlsplit(url)
        if url.startswith(provider) or parsed.username or parsed.password:
            raise ValueError("private provider")
        return url

    monkeypatch.setattr(ytdlp_safe, "validate_public_url_sync", validate)

    with pytest.raises(URLError):
        ytdlp_safe._guard_url(f"{provider}/get_pot")

    assert ytdlp_safe._guard_url(f"{provider}/get_pot", allow_provider=True) == (
        f"{provider}/get_pot"
    )
    with pytest.raises(URLError):
        ytdlp_safe._guard_url(
            f"http://user@provider:4416/get_pot",
            allow_provider=True,
        )


def test_external_redirect_cannot_enter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "http://provider:4416"
    monkeypatch.setattr(ytdlp_safe, "_POT_PROVIDER", ("http", "provider", 4416))

    def validate(url: str) -> str:
        if url.startswith(provider):
            raise ValueError("private provider")
        return url

    monkeypatch.setattr(ytdlp_safe, "validate_public_url_sync", validate)
    calls: list[str] = []

    def request(_self: Any, _method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(url)
        return SimpleNamespace(
            status_code=302,
            headers={"Location": f"{provider}/get_pot"},
            close=lambda: None,
        )

    guarded = ytdlp_safe._guard_session_request(request)
    with pytest.raises(URLError):
        guarded(object(), "GET", "https://public.example/media")

    assert calls == ["https://public.example/media"]


def test_provider_redirect_can_reach_public_media_but_not_return_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "http://provider:4416"
    public = "https://cdn.example/media"
    monkeypatch.setattr(ytdlp_safe, "_POT_PROVIDER", ("http", "provider", 4416))
    monkeypatch.setattr(
        ytdlp_safe,
        "validate_public_url_sync",
        lambda url: url,
    )
    calls: list[str] = []

    def request(_self: Any, _method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(url)
        if url == provider:
            return SimpleNamespace(
                status_code=302,
                headers={"Location": public},
                close=lambda: None,
            )
        return SimpleNamespace(status_code=200, headers={}, close=lambda: None)

    guarded = ytdlp_safe._guard_session_request(request)
    response = guarded(object(), "GET", provider)

    assert response.status_code == 200
    assert calls == [provider, public]
