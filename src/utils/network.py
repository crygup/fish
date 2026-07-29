from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import aiohttp
from discord.ext import commands

MAX_MEDIA_BYTES = 50 * 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_public_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_http: bool = True,
) -> str:
    """Validate that an HTTP URL resolves only to public addresses.

    This check is repeated for every redirect and browser subrequest by callers.
    Network-level egress filtering should still be used in production as a final
    defense against DNS rebinding and browser implementation bugs.
    """

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise commands.BadArgument("Invalid URL.") from error

    schemes = {"https", "http"} if allow_http else {"https"}
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() not in schemes
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise commands.BadArgument("Only public HTTP(S) URLs are allowed.")

    if allowed_hosts is not None:
        allowed = {item.lower().rstrip(".") for item in allowed_hosts}
        if host not in allowed:
            raise commands.BadArgument("That website is not supported.")

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as error:
            raise commands.BadArgument(
                "The URL hostname could not be resolved."
            ) from error
        resolved = {str(item[4][0]) for item in addresses}
    else:
        resolved = {str(literal_address)}
    if not resolved or any(not _is_public_address(address) for address in resolved):
        raise commands.BadArgument(
            "Private and local network addresses are not allowed."
        )
    return url


@dataclass(slots=True)
class FetchedBytes:
    data: bytes
    url: str
    content_type: str


async def read_bounded_response(
    response: aiohttp.ClientResponse, max_bytes: int
) -> bytes:
    if response.content_length is not None and response.content_length > max_bytes:
        raise commands.BadArgument("The response was too large.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise commands.BadArgument("The response was too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_connected_peer(response: aiohttp.ClientResponse) -> None:
    """Reject a connection if DNS rebinding sent it to a non-public peer."""

    connection = response.connection
    transport = connection.transport if connection is not None else None
    peer = transport.get_extra_info("peername") if transport is not None else None
    if peer and not _is_public_address(str(peer[0])):
        response.close()
        raise commands.BadArgument(
            "Private and local network addresses are not allowed."
        )


async def fetch_public_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int = MAX_MEDIA_BYTES,
    allowed_content_prefixes: tuple[str, ...] = ("image/", "video/"),
    allowed_hosts: Iterable[str] | None = None,
    max_redirects: int = 5,
) -> FetchedBytes:
    """Fetch a bounded public resource while validating every redirect."""

    current = url
    for _ in range(max_redirects + 1):
        await validate_public_url(current, allowed_hosts=allowed_hosts)
        try:
            async with session.get(current, allow_redirects=False) as response:
                validate_connected_peer(response)
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise commands.BadArgument(
                            "The URL returned an invalid redirect."
                        )
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise commands.BadArgument(
                        f"The media server returned HTTP {response.status}."
                    )

                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if allowed_content_prefixes and not content_type.startswith(
                    allowed_content_prefixes
                ):
                    raise commands.BadArgument(
                        "The URL did not return supported media."
                    )

                data = await read_bounded_response(response, max_bytes)
                return FetchedBytes(data, str(response.url), content_type)
        except commands.CommandError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise commands.BadArgument("The media URL could not be fetched.") from error

    raise commands.BadArgument("The media URL redirected too many times.")
