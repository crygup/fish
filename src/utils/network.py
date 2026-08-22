from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
import discord
from discord.ext import commands
from discord.http import Route

MAX_MEDIA_BYTES = 50 * 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DISCORD_ATTACHMENT_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }
)


def canonical_media_url(url: str) -> str:
    """Return a stable URL for comparing media without signed metadata.

    Discord attachment URLs carry expiring ``ex``, ``is`` and ``hm`` query
    parameters.  Those values change when an attachment is refreshed, so they
    must not make the same uploaded media look like a new source.  Non-Discord
    URLs are left intact apart from surrounding angle brackets and whitespace.
    """

    value = str(url or "").strip().strip("<>")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if hostname not in DISCORD_ATTACHMENT_HOSTS:
        return value
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            "",
            "",
        )
    )


def is_public_address(address: str) -> bool:
    """Return whether *address* is a routable public IP address.

    Browser integrations cannot use :func:`validate_public_url` alone because
    the browser performs its own DNS lookup.  Keeping this check public lets
    those integrations validate the address reported by the actual network
    connection as a second line of defence.
    """

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # ``is_private`` does not cover every non-routable allocation.  In
    # particular, carrier-grade NAT (100.64.0.0/10), benchmarking ranges,
    # documentation ranges, and IPv6 unique-local addresses must not be
    # reachable by a URL fetcher either.  ``is_global`` is the standard
    # library's complete routability check for those special ranges.
    return ip.is_global


# Keep the private name available for callers that imported it while this
# helper was internal.
_is_public_address = is_public_address


def validate_public_url_sync(url: str) -> str:
    """Synchronously validate a network URL before a third-party fetch.

    The normal bot HTTP paths use :func:`validate_public_url`, but yt-dlp runs
    in a subprocess and performs its own redirects.  The small synchronous
    equivalent is used by the guarded yt-dlp entry point for every request.
    Non-HTTP URLs are returned unchanged so yt-dlp can apply its own handling
    for data URLs and reject file URLs as usual.
    """

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid URL.") from error

    if parsed.scheme.lower() not in {"http", "https"}:
        return url
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("Only public HTTP(S) URLs are allowed.")

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = socket.getaddrinfo(
                host,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as error:
            raise ValueError("The URL hostname could not be resolved.") from error
        resolved = {str(item[4][0]) for item in addresses}
    else:
        resolved = {str(literal_address)}

    if not resolved or any(not is_public_address(address) for address in resolved):
        raise ValueError("Private and local network addresses are not allowed.")
    return url


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
    if not resolved or any(not is_public_address(address) for address in resolved):
        raise commands.BadArgument(
            "Private and local network addresses are not allowed."
        )
    return url


async def refresh_discord_attachment_url(bot: Any, url: str) -> str:
    """Refresh a signed Discord CDN URL when its query has expired."""

    try:
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return url
    if hostname not in DISCORD_ATTACHMENT_HOSTS:
        return url
    original = url.split("?", 1)[0]
    try:
        response = await bot.http.request(
            Route("POST", "/attachments/refresh-urls"),
            json={"attachment_urls": [original]},
        )
        refreshed_urls = response.get("refreshed_urls", [])
        if isinstance(refreshed_urls, list) and refreshed_urls:
            refreshed = refreshed_urls[0]
            if isinstance(refreshed, dict) and refreshed.get("refreshed"):
                return str(refreshed["refreshed"])
    except (AttributeError, discord.HTTPException, KeyError, TypeError):
        # Still-valid URLs and URLs Discord does not recognize can continue
        # through the caller's normal bounded fetch path.
        pass
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


def validate_connected_peer(
    response: aiohttp.ClientResponse, *, allow_missing_peer: bool = False
) -> None:
    """Reject a connection if DNS rebinding sent it to a non-public peer.

    A few CDN responses do not expose a transport peer through aiohttp after
    headers are received. Callers that already performed :func:`validate_public_url`
    and are talking to a known public CDN may opt into accepting that missing
    metadata. A reported private peer is never accepted.
    """

    connection = response.connection
    transport = connection.transport if connection is not None else None
    # aiohttp can clear ``response.connection`` while the response is still
    # inside its context manager.  The protocol retains the same transport,
    # so use it as a compatibility fallback instead of treating a legitimate
    # response (notably Discord CDN avatar URLs) as an unavailable peer.
    if transport is None:
        protocol = getattr(response, "_protocol", None)
        transport = getattr(protocol, "transport", None)
    peer = transport.get_extra_info("peername") if transport is not None else None
    address = peer[0] if isinstance(peer, tuple) and peer else None
    if not address:
        if allow_missing_peer:
            return
        response.close()
        raise commands.BadArgument(
            "The remote server did not provide a public network connection."
        )
    if not is_public_address(str(address)):
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
