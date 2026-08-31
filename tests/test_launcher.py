from typing import Any, cast

import pytest

from core import normalize_bot_instance
from launcher import _api_port, _env_bool, _resolve_instance, _token_for_instance
from utils import Config


def test_instance_defaults_preserve_legacy_and_testing_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FISHIE_BOT_INSTANCE", raising=False)

    assert _resolve_instance(False) == "legacy"
    assert _resolve_instance(True) == "testing"
    assert normalize_bot_instance("old") == "legacy"


def test_new_instance_can_be_selected_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FISHIE_BOT_INSTANCE", "new")

    assert _resolve_instance(False) == "new"
    with pytest.raises(ValueError):
        _resolve_instance(True)


def test_instance_token_and_port_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    config: dict[str, Any] = {
        "tokens": {
            "bot": "legacy-token",
            "testing_bot": "testing-token",
            "new_bot": "new-token",
        },
        "databases": {},
    }
    monkeypatch.delenv("DISCORD_NEW_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FISHIE_API_PORT", raising=False)
    monkeypatch.delenv("FISHIE_API_PORT_NEW", raising=False)

    assert _token_for_instance(cast(Config, config), "new") == "new-token"
    assert _api_port("legacy") == 8001
    assert _api_port("new") == 8002

    monkeypatch.setenv("DISCORD_NEW_BOT_TOKEN", "environment-token")
    monkeypatch.setenv("FISHIE_API_PORT_NEW", "8123")
    assert _token_for_instance(cast(Config, config), "new") == "environment-token"
    assert _api_port("new") == 8123


def test_api_enabled_flag_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FISHIE_API_ENABLED", raising=False)
    assert _env_bool("FISHIE_API_ENABLED") is True

    monkeypatch.setenv("FISHIE_API_ENABLED", "off")
    assert _env_bool("FISHIE_API_ENABLED") is False

    monkeypatch.setenv("FISHIE_API_ENABLED", "maybe")
    with pytest.raises(RuntimeError):
        _env_bool("FISHIE_API_ENABLED")
