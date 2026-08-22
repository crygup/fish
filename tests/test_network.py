import asyncio
import socket

import pytest
from discord.ext import commands

from utils.network import (
    canonical_media_url,
    is_public_address,
    refresh_discord_attachment_url,
    validate_connected_peer,
    validate_public_url,
    validate_public_url_sync,
)


@pytest.mark.asyncio
async def test_rejects_loopback_url() -> None:
    with pytest.raises(commands.BadArgument, match="Private"):
        await validate_public_url("http://127.0.0.1/admin")


@pytest.mark.parametrize(
    "address",
    ("100.64.0.1", "192.0.0.1", "198.18.0.1", "fc00::1", "2001:db8::1"),
)
def test_rejects_non_global_special_ranges(address: str) -> None:
    assert not is_public_address(address)


def test_sync_url_validation_rejects_private_literal() -> None:
    with pytest.raises(ValueError, match="Private"):
        validate_public_url_sync("https://100.64.0.1/media.mp4")


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
    async def public_result(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public_result)
    assert await validate_public_url("https://example.com/path") == (
        "https://example.com/path"
    )


@pytest.mark.asyncio
async def test_rejects_mixed_public_and_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mixed_result(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", mixed_result)
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


def test_uses_protocol_transport_when_connection_is_unavailable() -> None:
    class Transport:
        def get_extra_info(self, name: str):
            return ("93.184.216.34", 443)

    class Protocol:
        transport = Transport()

    class Response:
        connection = None
        _protocol = Protocol()

        def close(self) -> None:
            raise AssertionError("a public protocol peer should be accepted")

    validate_connected_peer(Response())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_refreshes_expired_discord_attachment_url() -> None:
    class HTTP:
        async def request(self, route, **kwargs):
            assert route.path == "/attachments/refresh-urls"
            assert kwargs["json"] == {
                "attachment_urls": [
                    "https://cdn.discordapp.com/attachments/1/2/file.png"
                ]
            }
            return {
                "refreshed_urls": [
                    {
                        "refreshed": "https://cdn.discordapp.com/attachments/1/2/file.png?ex=fresh"
                    }
                ]
            }

    class Bot:
        http = HTTP()

    result = await refresh_discord_attachment_url(
        Bot(),
        "https://cdn.discordapp.com/attachments/1/2/file.png?ex=expired&hm=old",
    )
    assert result.endswith("?ex=fresh")


def test_canonical_media_url_ignores_discord_signature_metadata() -> None:
    signed = (
        "https://cdn.discordapp.com/attachments/1/2/file.png?"
        "ex=expired&is=old&hm=signature"
    )
    assert canonical_media_url(signed) == (
        "https://cdn.discordapp.com/attachments/1/2/file.png"
    )
    assert canonical_media_url(
        "https://example.com/file.png?cache=1"
    ) == "https://example.com/file.png?cache=1"
