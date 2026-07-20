import socket

import pytest
from discord.ext import commands

from utils.network import validate_connected_peer, validate_public_url


@pytest.mark.asyncio
async def test_rejects_loopback_url() -> None:
    with pytest.raises(commands.BadArgument, match="Private"):
        await validate_public_url("http://127.0.0.1/admin")


@pytest.mark.asyncio
async def test_rejects_credentials_in_url() -> None:
    with pytest.raises(commands.BadArgument, match="public HTTP"):
        await validate_public_url("https://user:password@example.com/")


@pytest.mark.asyncio
async def test_host_allowlist_is_exact() -> None:
    with pytest.raises(commands.BadArgument, match="not supported"):
        await validate_public_url(
            "https://youtube.com.attacker.example/video", allowed_hosts={"youtube.com"}
        )


@pytest.mark.asyncio
async def test_accepts_only_public_dns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def public_result(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", public_result)
    assert await validate_public_url("https://example.com/path") == (
        "https://example.com/path"
    )


@pytest.mark.asyncio
async def test_rejects_mixed_public_and_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mixed_result(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_result)
    with pytest.raises(commands.BadArgument, match="Private"):
        await validate_public_url("https://example.com/path")


def test_rejects_rebound_private_connection() -> None:
    class Transport:
        def get_extra_info(self, name: str):
            return ("169.254.169.254", 80)

    class Response:
        connection = type("Connection", (), {"transport": Transport()})()

        def close(self) -> None:
            pass

    with pytest.raises(commands.BadArgument, match="Private"):
        validate_connected_peer(Response())  # type: ignore[arg-type]
