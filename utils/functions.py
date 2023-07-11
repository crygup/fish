from __future__ import annotations

import asyncio
from io import BytesIO
import textwrap
from typing import Awaitable, Callable, Sequence, TYPE_CHECKING, Tuple

from aiohttp import ClientResponse
import aiohttp
from discord.ext import commands

from .types import P, T

if TYPE_CHECKING:
    from core import Fishie


def to_thread(func: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


def human_join(seq: Sequence[str], delim=", ", final="or", spaces: bool = True) -> str:
    size = len(seq)
    if size == 0:
        return ""

    if size == 1:
        return seq[0]

    if size == 2:
        return f"{seq[0]} {final} {seq[1]}"

    final = f" {final} " if spaces else final
    return delim.join(seq[:-1]) + f"{final}{seq[-1]}"


def response_checker(response: ClientResponse) -> bool:
    if response.status == 200:
        return True

    bad_response = {
        502: "The server is down or under maintenance, try again later.",
        404: "The requested resource could not be found.",
        400: "The request was invalid.",
        401: "The request requires authentication.",
        403: "The request was forbidden.",
    }
    for br, reason in bad_response.items():
        if br == response:
            raise commands.BadArgument(reason)

    if str(response.status).startswith("5"):
        reason = (
            f"\nReason: {textwrap.shorten(response.reason, 100)}"
            if response.reason
            else ""
        )
        raise commands.BadArgument(
            f"The server returned an error ({response.status}). {reason}"
        )

    raise commands.BadArgument(
        f"Something went wrong, try again later? \nStatus code: `{response.status}`"
    )


async def get_sp_cover(bot: Fishie, query: str) -> Tuple[str, bool]:
    results = bot.cached_covers.get(query)

    if results:
        return results

    if bot.spotify_key is None:
        raise commands.BadArgument(
            "Spotify key is not set yet, maybe spotify cog needs loaded?"
        )

    url = "https://api.spotify.com/v1/search"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bot.spotify_key}",
    }

    data = {"q": query, "type": "album", "limit": "1"}

    async with bot.session.get(url, headers=headers, params=data) as r:
        results = await r.json()

    try:
        cover = results["albums"]["items"][0]["images"][0]["url"]
        nsfw = results["albums"]["items"][0]["id"] in await bot.redis.smembers(
            "nsfw_covers"
        )

        try:
            bot.cached_covers[query] = (cover, nsfw)
        except KeyError:
            pass

        return cover, nsfw
    except (IndexError, KeyError):
        raise commands.BadArgument("No cover found for this album, sorry.")


async def to_image(
    session: aiohttp.ClientSession,
    url: str,
    bytes: bool = False,
    skip_check: bool = False,
) -> BytesIO | bytes:
    async with session.get(url) as resp:
        if not skip_check:
            response_checker(resp)

        data = await resp.read()

        return data if bytes else BytesIO(data)
