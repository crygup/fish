from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from bot import Bot
else:
    from discord.ext.commands import Bot

from .helpers import response_checker


async def LastfmClient(
    bot: Bot,
    version: str,
    method: str,
    endpoint: str,
    query: str,
    extras: Optional[str] = None,
) -> Dict[Any, Any]:
    url = f"http://ws.audioscrobbler.com/{version}/?method={method}&{endpoint}={query}&api_key={bot.config['keys']['lastfm_key']}&format=json"
    if extras:
        url += f"{extras}"
    async with bot.session.get(url) as response:
        response_checker(response)
        return await response.json()


async def SteamClient(
    bot: Bot,
    endpoint: str,
    version: str,
    account: int,
    ids: bool = False,
) -> Dict:
    url = f'https://api.steampowered.com/{endpoint}/{version}/?key={bot.config["keys"]["steam_key"]}&steamid{"s" if ids else ""}={account}'
    async with bot.session.get(url) as response:
        response_checker(response)
        return await response.json()
