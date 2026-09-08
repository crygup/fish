from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Mapping
from typing import Any

import aiohttp
from discord.ext import commands

from .functions import response_checker

_KEY_FAILURE_STATUSES = frozenset({400, 401, 403, 429})


def _google_keys(values: str | Iterable[object]) -> list[str]:
    if isinstance(values, str):
        values = (values,)
    keys = list(dict.fromkeys(key for value in values if (key := str(value).strip())))
    random.shuffle(keys)
    return keys


async def google_json(
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
        request_params["key"] = key
        try:
            async with session.get(url, params=request_params) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if isinstance(data, dict):
                        return data
                    raise commands.BadArgument("Google returned an invalid response.")
                if (
                    response.status not in _KEY_FAILURE_STATUSES
                    or index == len(usable_keys) - 1
                ):
                    response_checker(response)
                last_key_error = commands.BadArgument(
                    f"Google key failed with status {response.status}"
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            last_key_error = error
            if index == len(usable_keys) - 1:
                raise commands.BadArgument(
                    "Google could not be reached. Please try again later."
                ) from error

    raise commands.BadArgument("Google API keys are unavailable.") from last_key_error
