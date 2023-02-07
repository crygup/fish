from __future__ import annotations

import datetime
import difflib
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import discord
from discord.ext import commands

from utils import (
    BlankException,
    SteamConverter,
    Unauthorized,
    human_join,
    response_checker,
    status_state,
    to_bytesio,
    get_steam_data,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Steam(CogBase):
    @commands.group(name="steam", invoke_without_command=True)
    async def steam(
        self,
        ctx: Context,
        account: Optional[Union[discord.User, str]] = commands.Author,
    ):
        if account is None:
            raise commands.UserNotFound("User not found")

        user_id = await SteamConverter().convert(ctx, account)

        await ctx.typing()

        info: Dict = await get_steam_data(ctx.bot, "ISteamUser/GetPlayerSummaries", "v0002", user_id, ids=True)  # type: ignore
        games: Dict = await get_steam_data(ctx.bot, "IPlayerService/GetOwnedGames", "v0001", user_id)  # type: ignore

        try:
            friends = await get_steam_data(ctx.bot, "ISteamUser/GetFriendList", "v0001", user_id)  # type: ignore
            friends = len(friends["friendslist"]["friends"])
        except Unauthorized:
            friends = 0

        info = info["response"]["players"][0]
        avatar = await to_bytesio(ctx.session, info["avatarfull"])
        avatar_file = discord.File(avatar, filename="avatar.png")

        name = (
            f'{info["personaname"]}  •  {info["realname"]}'
            if info.get("realname")
            else info["personaname"]
        )

        embed = discord.Embed(
            timestamp=datetime.datetime.utcfromtimestamp(info["timecreated"])
            if info.get("timecreated")
            else None,
        )

        embed.add_field(name="Status", value=status_state[info["personastate"]])
        embed.add_field(
            name="Last Logoff",
            value=f'<t:{info["lastlogoff"]}:R>'
            if info.get("lastlogoff")
            else "Unknown",
        )
        embed.add_field(name="Friends", value=f"{friends:,}")
        embed.add_field(
            name="Games",
            value=f'{int(games["response"]["game_count"]):,}'
            if "game_count" in games["response"]
            else "0",
        )
        embed.set_thumbnail(url="attachment://avatar.png")
        embed.set_author(name=name, icon_url=info["avatarfull"], url=info["profileurl"])

        footer_text = (
            f"ID: {user_id} \nCreated at"
            if info.get("timecreated")
            else f"ID: {user_id}"
        )
        embed.set_footer(text=footer_text)

        await ctx.send(embed=embed, check_ref=True, files=[avatar_file])

    @steam.command(name="id")
    async def steam_id(self, ctx: Context, account: SteamConverter = commands.Author):
        user_id = (
            await SteamConverter().convert(ctx, str(account.id))
            if isinstance(account, discord.Member)
            else account
        )
        info: Dict = await get_steam_data(ctx.bot, "ISteamUser/GetPlayerSummaries", "v0002", user_id, ids=True)  # type: ignore
        info = info["response"]["players"][0]
        await ctx.send(f'{info["personaname"]}\'s 64bit steam ID is: `{user_id}`')

    @steam.group(name="search", invoke_without_command=True)
    async def steam_search(self, ctx: Context):
        await ctx.send("wip", delete_after=5)

    @steam_search.command(name="game")
    async def steam_search_game(self, ctx: Context):
        await ctx.send("wip", delete_after=5)

    @steam.command(name="game")
    async def steam_game(
        self,
        ctx: Context,
        app_id: Optional[int] = None,
        *,
        app_name: Optional[str] = None,
    ):
        if app_id:
            app_id = app_id

        elif app_name:
            message = await ctx.send(
                "Searching for game from name, this might take a moment."
            )

            async with ctx.typing():
                app_id = await self.get_app_id_from_name(ctx, app_name)
            await ctx.delete(message)

        else:
            raise BlankException("Provide either an app id or title of a game.")

        url = f"https://store.steampowered.com/api/appdetails"

        params: Dict[str, Any] = {
            "key": self.bot.config["keys"]["steam-key"],
            "appids": app_id,
        }

        async with ctx.session.get(url, params=params) as response:
            response_checker(response)
            json = await response.json()
            if not json[str(app_id)]["success"]:
                raise BlankException("Unknown app id.")

            data: Dict[Any, Any] = json[str(app_id)]["data"]

        if data["type"] != "game":
            raise BlankException(
                "App id provided is not a game, please only provide game app ids."
            )

        embed = discord.Embed(
            color=self.bot.embedcolor,
            description=data.get("short_description"),
        )
        embed.add_field(
            name="Categories",
            value=human_join(
                [item["description"] for item in data["categories"]], final="and"
            ),
            inline=False,
        )
        embed.add_field(
            name="Developers",
            value=human_join(data["developers"], final="and"),
            inline=False,
        )
        embed.add_field(
            name="Publishers",
            value=human_join(data["publishers"], final="and"),
            inline=True,
        )
        footer_text = f"App ID: {data['steam_appid']}"

        embed.set_author(
            name=data["name"],
            url=f"https://store.steampowered.com/app/{data['steam_appid']}",
        )

        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed)
