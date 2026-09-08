from __future__ import annotations

import asyncio
import random
import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

import aiohttp
from cachetools import TTLCache
from discord.ext import commands

from .functions import response_checker

_KEY_FAILURE_STATUSES = frozenset({400, 401, 403, 429})
_KEY_COOLDOWNS = TTLCache[tuple[str, str], bool](maxsize=256, ttl=60)


def _google_keys(values: str | Iterable[object]) -> list[str]:
    if isinstance(values, str):
        values = (values,)
    keys = list(dict.fromkeys(key for value in values if (key := str(value).strip())))
    random.shuffle(keys)
    return keys


async def _google_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    keys: str | Iterable[object],
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a Google API JSON response, trying each key once on key errors."""
    usable_keys = _google_keys(keys)
    if not usable_keys:
        raise commands.BadArgument("Google API key is not configured.")

    request_params = dict(params or {})
    last_key_error: Exception | None = None
    for index, key in enumerate(usable_keys):
        cooldown_key = (url, hashlib.sha256(key.encode()).hexdigest())
        if cooldown_key in _KEY_COOLDOWNS:
            continue
        request_params["key"] = key
        try:
            async with session.get(url, params=request_params) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if isinstance(data, dict):
                        return data
                    raise commands.BadArgument("Google returned an invalid response.")
                if response.status == 400:
                    payload = await response.json(content_type=None)
                    detail = (
                        payload.get("error", {}) if isinstance(payload, dict) else {}
                    )
                    detail = detail if isinstance(detail, dict) else {}
                    # A malformed search does not improve with another key.
                    if (
                        not any(
                            item.get("reason") in {"keyInvalid", "API_KEY_INVALID"}
                            for item in (
                                *detail.get("errors", []),
                                *detail.get("details", []),
                            )
                            if isinstance(item, dict)
                        )
                        and "api key not valid"
                        not in str(detail.get("message", "")).casefold()
                    ):
                        response_checker(response)
                if response.status in _KEY_FAILURE_STATUSES:
                    _KEY_COOLDOWNS[cooldown_key] = True
                if (
                    response.status not in _KEY_FAILURE_STATUSES
                    or index == len(usable_keys) - 1
                ):
                    response_checker(response)
                last_key_error = commands.BadArgument(
                    f"Google key failed with status {response.status}"
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise commands.BadArgument(
                "Google could not be reached. Please try again later."
            ) from error

    raise commands.BadArgument("Google API keys are unavailable.") from last_key_error


async def google_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    keys: str | Iterable[object],
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(12):
            return await _google_json(session, url, keys=keys, params=params)
    except TimeoutError as error:
        raise commands.BadArgument(
            "Google is taking too long to respond. Please try again later."
        ) from error
