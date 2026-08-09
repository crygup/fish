"""Upload oversized Discord results to Fishie's short-lived media host."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlsplit

import aiohttp

if TYPE_CHECKING:
    from core.bot import Fishie

TEMP_MEDIA_MAX_BYTES = 500 * 1024 * 1024
# The normal temporary host limit remains 500 MiB.  The bot owner can opt into
# a bounded larger upload from the download command after the command-level
# owner check has passed.  Keeping a hard ceiling still protects the host from
# accidentally accepting an unbounded request.
TEMP_MEDIA_OWNER_MAX_BYTES = 2 * 1024 * 1024 * 1024
TEMP_MEDIA_TTL = 30 * 60
TEMP_MEDIA_TIMEOUT = 10 * 60
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

__all__ = [
    "TEMP_MEDIA_MAX_BYTES",
    "TEMP_MEDIA_OWNER_MAX_BYTES",
    "TEMP_MEDIA_TTL",
    "TEMP_MEDIA_TIMEOUT",
    "TemporaryMediaError",
    "upload_temporary_media",
]


class TemporaryMediaError(RuntimeError):
    """The temporary media service could not accept an upload."""


def _config_value(bot: Fishie, key: str) -> str:
    environment_name = {
        "url": "FISHIE_TEMP_MEDIA_URL",
        "api_key": "FISHIE_MEDIA_API_KEY",
        "owner_api_key": "FISHIE_MEDIA_OWNER_API_KEY",
    }[key]
    value = os.getenv(environment_name, "").strip()
    if value:
        return value
    config = getattr(bot, "config", {})
    keys = config.get("keys", {}) if isinstance(config, dict) else {}
    if key == "api_key":
        return str(keys.get("media_api", "")).strip()
    if key == "owner_api_key":
        return str(keys.get("media_api_owner", "")).strip()
    return "https://api.crygup.com/media"


def _upload_urls(public_url: str, *, owner_override: bool) -> tuple[str, ...]:
    """Return upload endpoints, preferring the local origin for owner uploads."""
    if not owner_override:
        return (public_url,)

    internal_url = os.getenv(
        # The /media/ prefix belongs to the public Nginx route.  The media
        # API listens directly on port 8003 and exposes /uploads and /files
        # at its root, so using /media here would request /media/uploads and
        # never reach the upload handler.
        "FISHIE_TEMP_MEDIA_INTERNAL_URL",
        "http://127.0.0.1:8003",
    ).strip()
    if not internal_url or internal_url.rstrip("/") == public_url.rstrip("/"):
        return (public_url,)
    return (internal_url.rstrip("/"), public_url)


def _safe_filename(filename: str) -> str:
    value = Path(str(filename or "media")).name
    value = _SAFE_FILENAME_RE.sub("_", value).strip("._")[:120]
    return value or "media"


async def _file_chunks(reader: BinaryIO) -> AsyncIterator[bytes]:
    await asyncio.to_thread(reader.seek, 0)
    while True:
        chunk = await asyncio.to_thread(reader.read, 1024 * 1024)
        if not chunk:
            return
        yield chunk


async def _path_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as reader:
        async for chunk in _file_chunks(reader):
            yield chunk


def _size_of_reader(reader: BinaryIO) -> int:
    try:
        return int(os.fstat(reader.fileno()).st_size)
    except (AttributeError, OSError, ValueError):
        current = reader.tell()
        reader.seek(0, os.SEEK_END)
        size = reader.tell()
        reader.seek(current)
        return int(size)


def _validate_response_url(base_url: str, value: object) -> str:
    if not isinstance(value, str):
        raise TemporaryMediaError("The temporary media service returned no URL.")
    parsed_base = urlsplit(base_url)
    parsed_value = urlsplit(value)
    if (
        parsed_value.scheme != parsed_base.scheme
        or parsed_value.netloc != parsed_base.netloc
        or not parsed_value.path.startswith(parsed_base.path.rstrip("/") + "/files/")
        or parsed_value.query
        or parsed_value.fragment
    ):
        raise TemporaryMediaError(
            "The temporary media service returned an invalid URL."
        )
    return value


async def upload_temporary_media(
    bot: Fishie,
    data: bytes | Path | BinaryIO,
    filename: str,
    *,
    content_type: str | None = None,
    ignore_size_limit: bool = False,
) -> str:
    """Upload media and return a public URL valid for thirty minutes.

    ``data`` can be bytes, a path, or an open binary file. File inputs stream
    in 1 MiB chunks so a large upload does not require another full buffer.
    ``ignore_size_limit`` is reserved for the owner-only download override.
    """

    public_url = _config_value(bot, "url").rstrip("/")
    api_key = _config_value(bot, "owner_api_key" if ignore_size_limit else "api_key")
    if not api_key:
        if ignore_size_limit:
            raise TemporaryMediaError(
                "Owner temporary media hosting is not configured."
            )
        raise TemporaryMediaError("Temporary media hosting is not configured.")
    if not public_url:
        raise TemporaryMediaError("Temporary media hosting has no URL configured.")

    safe_name = _safe_filename(filename)
    if content_type is None:
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

    payload_kind: str
    if isinstance(data, bytes):
        size = len(data)
        payload_kind = "bytes"
    elif isinstance(data, Path):
        try:
            size = data.stat().st_size
        except OSError as error:
            raise TemporaryMediaError(
                "The temporary media file is unavailable."
            ) from error
        payload_kind = "path"
    else:
        size = _size_of_reader(data)
        payload_kind = "reader"

    if size <= 0:
        raise TemporaryMediaError("The temporary media file is empty.")
    max_bytes = (
        TEMP_MEDIA_OWNER_MAX_BYTES if ignore_size_limit else TEMP_MEDIA_MAX_BYTES
    )
    if size > max_bytes:
        limit = max_bytes // (1024 * 1024)
        raise TemporaryMediaError(f"Media larger than {limit:g} MB cannot be hosted.")

    headers = {
        "X-API-Key": api_key,
        "X-Filename": safe_name,
        "Content-Type": content_type,
        "Content-Length": str(size),
    }
    if ignore_size_limit:
        headers["X-Ignore-Size-Limit"] = "1"
    timeout = aiohttp.ClientTimeout(total=TEMP_MEDIA_TIMEOUT)
    last_error: TemporaryMediaError | None = None
    upload_urls = _upload_urls(public_url, owner_override=ignore_size_limit)
    for upload_url in upload_urls:
        if payload_kind == "bytes":
            payload: bytes | AsyncIterator[bytes] = data  # type: ignore[assignment]
        elif payload_kind == "path":
            payload = _path_chunks(data)  # type: ignore[arg-type]
        else:
            payload = _file_chunks(data)  # type: ignore[arg-type]
        try:
            async with bot.session.post(
                f"{upload_url}/uploads",
                data=payload,
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status != 201:
                    detail = (await response.text())[:300]
                    raise TemporaryMediaError(
                        f"Temporary media hosting returned HTTP {response.status}: {detail}"
                    )
                try:
                    result = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as error:
                    raise TemporaryMediaError(
                        "Temporary media hosting returned an invalid response."
                    ) from error
            if not isinstance(result, dict):
                raise TemporaryMediaError(
                    "Temporary media hosting returned invalid data."
                )
            # The origin may be used for upload, but its response must always
            # contain the public URL that Discord can access.
            return _validate_response_url(public_url, result.get("url"))
        except TemporaryMediaError as error:
            last_error = error
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            last_error = TemporaryMediaError("Temporary media hosting is unavailable.")
            if upload_url != upload_urls[-1]:
                continue
            raise last_error from error

    if last_error is not None:
        raise last_error
    raise TemporaryMediaError("Temporary media hosting is unavailable.")
