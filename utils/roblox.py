from typing import Dict, List

from aiohttp import ClientSession

from .helpers import response_checker


async def fetch_badges(session: ClientSession, account_id: int) -> Dict:
    """Get a list of badges for a user"""

    async with session.get(
        f"https://accountinformation.roblox.com/v1/users/{account_id}/roblox-badges"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_onlinestatus(session: ClientSession, account_id: int) -> Dict:
    """Gets the online status of a user"""

    async with session.get(
        f"https://api.roblox.com/users/{account_id}/onlinestatus"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_headshot(
    session: ClientSession,
    account_id: int,
    width: int = 420,
    height: int = 420,
    format: str = "png",
) -> str:
    """
    Returns the link to a headshot image of a user

    width, height, and format can be customised
    """
    url = f"https://www.roblox.com/headshot-thumbnail/image?userId={account_id}&width={width}&height={height}&format={format}"

    async with session.get(url) as resp:
        response_checker(resp)
        return str(resp.url)


async def fetch_outfit_image(
    session: ClientSession,
    account_id: int,
    width: int = 420,
    height: int = 420,
    format: str = "png",
) -> str:
    """
    Returns the link to a outfit image of a user

    width, height, and format can be customised
    """
    url = f"https://www.roblox.com/outfit-thumbnail/image?userOutfitId={account_id}&width={width}&height={height}&format={format}"

    async with session.get(url) as resp:
        response_checker(resp)
        return str(resp.url)


async def fetch_asset_thumbnail(
    session: ClientSession,
    asset_id: int,
    width: int = 420,
    height: int = 420,
    format: str = "png",
) -> str:
    """
    Returns the link to an image of an asset

    width, height, and format can be customised
    """
    url = f"https://www.roblox.com/asset-thumbnail/image?assetId={asset_id}&width={width}&height={height}&format={format}"

    async with session.get(url) as resp:
        response_checker(resp)
        return str(resp.url)


async def fetch_user_id_by_name(session: ClientSession, name: str) -> int:
    """
    Returns the user id of a user by their name
    """
    async with session.get(
        f"https://api.roblox.com/users/get-by-username?username={name}"
    ) as resp:
        response_checker(resp)
        return (await resp.json())["Id"]


async def fetch_outfits(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the outfits of a user
    """
    async with session.get(
        f"https://avatar.roblox.com/v1/users/{account_id}/outfits"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_avatar(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the avatar of a user
    """
    async with session.get(
        f"https://avatar.roblox.com/v1/users/{account_id}/avatar"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_primary_group(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the primary group of a user
    """
    async with session.get(
        f"https://groups.roblox.com/v1/users/{account_id}/groups/primary/role"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_rblx_trade_user_info(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the rblx trade user info of a user
    """
    async with session.get(
        f"https://rblx.trade/api/v2/users/{account_id}/info"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_info(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the info of a user
    """
    async with session.get(f"https://users.roblox.com/v1/users/{account_id}") as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_friends(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the friend count of a user
    """
    async with session.get(
        f"https://friends.roblox.com/v1/users/{account_id}/friends"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_followers(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the followers of a user
    """
    async with session.get(
        f"https://friends.roblox.com/v1/users/{account_id}/followers"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_friend_count(session: ClientSession, account_id: int) -> int:
    """
    Returns the friend count of a user
    """
    async with session.get(
        f"https://friends.roblox.com/v1/users/{account_id}/friends/count"
    ) as resp:
        response_checker(resp)
        return (await resp.json())["count"]


async def fetch_followers_count(session: ClientSession, account_id: int) -> int:
    """
    Returns the followers of a user
    """
    async with session.get(
        f"https://friends.roblox.com/v1/users/{account_id}/followers/count"
    ) as resp:
        response_checker(resp)
        return (await resp.json())["count"]


async def fetch_groups(session: ClientSession, group_ids: List[int]) -> Dict:
    """
    Multi-get groups information by Ids
    """
    groups = ",".join([str(grou_id) for grou_id in group_ids])

    async with session.get(
        f"https://groups.roblox.com/v2/groups?groupIds={groups}"
    ) as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_group(session: ClientSession, group_id: int) -> Dict:
    """
    Multi-get groups information by Ids
    """
    async with session.get(f"https://groups.roblox.com/v1/groups/{group_id}") as resp:
        response_checker(resp)
        return await resp.json()


async def fetch_user_groups(session: ClientSession, account_id: int) -> Dict:
    """
    Returns the groups of a user
    """
    async with session.get(
        f"https://groups.roblox.com/v2/users/{account_id}/groups/roles"
    ) as resp:
        response_checker(resp)
        return await resp.json()
