from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import bs4
import discord
from bs4 import BeautifulSoup
from dateutil import parser
from discord.ext import commands

from utils import (
    REPLIES,
    REPLY,
    FieldPageSource,
    Pager,
    RobloxAccountConverter,
    RobloxAssetConverter,
    SimplePages,
    human_join,
    template,
    to_bytesio,
    to_thread,
)
from utils.helpers.roblox import *  # smd

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Roblox(bot))


class Roblox(commands.Cog, name="roblox"):
    """Roblox related commands"""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="roblox", id=1006847143400706078)

    @commands.group(name="roblox", aliases=("rbx",), invoke_without_command=True)
    async def roblox(
        self,
        ctx: Context,
        *,
        account: int = commands.parameter(
            converter=RobloxAccountConverter,
            default=None,
            displayed_default="[roblox account]",
        ),
    ):
        """Get information about a roblox account"""

        async with ctx.typing():
            user = account or await RobloxAccountConverter().convert(
                ctx, str(ctx.author)
            )

            info = await fetch_info(ctx.bot.session, user)

            embed = discord.Embed(timestamp=parser.parse(info["created"]))
            embed.description = textwrap.shorten(info["description"], 100)

            name = (
                info["name"]
                if info["name"] == info["displayName"]
                else f'{info["name"]}  •  {info["displayName"]}'
            )
            embed.set_author(name=name, icon_url=f"attachment://{user}_headshot.png")

            followers = await fetch_followers_count(ctx.bot.session, user)
            friends = await fetch_friend_count(ctx.bot.session, user)
            embed.add_field(
                name="Followers",
                value=f"Followers: {followers:,}\nFriends: {friends:,}",
            )

            rblx_info = await fetch_rblx_trade_user_info(ctx.bot.session, user)
            rap = f'{rblx_info["accountRAP"]:,}'
            value = f'{rblx_info["accountValue"]:,}'

            embed.add_field(name="Value", value=f"Value: {value}\nRAP: {rap}")
            embed.add_field(
                name="Place vists", value=f'{rblx_info["placeVisitCount"]:,}'
            )

            onlinestatus = await fetch_onlinestatus(ctx.bot.session, user)
            onlinestatus_str = ["Offline", "Online"][onlinestatus["IsOnline"]]
            if not onlinestatus["IsOnline"]:
                onlinestatus_str += f'\n{REPLY}Last seen {discord.utils.format_dt(parser.parse(onlinestatus["LastOnline"]), "R")}'

            embed.add_field(name="Status", value=onlinestatus_str)

            usernames = await fetch_usernames(ctx.bot.session, user)
            if usernames:
                first_5 = usernames[:5]
                humaned_joined = human_join([f"`{u}`" for u in first_5], final="and")
                remaining = usernames[5:]
                text = f"{humaned_joined}"

                if remaining:
                    text += f"\n*({len(remaining)} remaining)*"

                embed.add_field(name="Usernames", value=text, inline=False)

            _badges = await fetch_badges(ctx.bot.session, user)
            badges = ", ".join([data["name"] for data in _badges])

            embed.set_footer(
                text=f"ID: {user}\nBadges: {badges if badges else 'No badges'}\nCreated"
            )

            headshot = discord.File(
                await to_bytesio(
                    ctx.session, await fetch_headshot(ctx.bot.session, user)
                ),
                f"{user}_headshot.png",
            )

        await ctx.send(embed=embed, files=[headshot])

    @roblox.command(name="usernames", aliases=("names",))
    async def roblox_usernames(
        self,
        ctx: Context,
        *,
        account: int = commands.parameter(
            converter=RobloxAccountConverter,
            default=None,
            displayed_default="[roblox account]",
        ),
    ):
        """Get all usernames for a roblox account"""

        async with ctx.typing():
            user = account or await RobloxAccountConverter().convert(
                ctx, str(ctx.author)
            )
            info = await fetch_info(ctx.bot.session, user)

            usernames = await fetch_usernames(ctx.bot.session, user)
            if not usernames:
                return await ctx.send("No usernames found")

        pages = SimplePages(entries=usernames, per_page=15, ctx=ctx)
        pages.embed.title = f"Usernames for {info['name']}"
        pages.embed.color = self.bot.embedcolor
        await pages.start(ctx)

    @roblox.command(name="set")
    async def roblox_set(self, ctx: Context, username: str):
        """Alias for set roblox command"""

        command = self.bot.get_command("set roblox")

        if command is None:
            return

        await command(ctx, username=username)

    @commands.command(name="friends")
    async def friends(
        self,
        ctx: Context,
        *,
        account: int = commands.parameter(
            converter=RobloxAccountConverter,
            default=None,
            displayed_default="[roblox account]",
        ),
    ):
        """Get the friends of a roblox account"""

        user = account or await RobloxAccountConverter().convert(ctx, str(ctx.author))
        name = (await fetch_info(ctx.bot.session, user))["name"]
        friends = await fetch_friends(ctx.bot.session, user)

        if not friends["data"]:
            raise TypeError(f"{name} has no friends.")

        entries = [
            (
                data["name"]
                if data["name"] == data["displayName"]
                else f'{data["name"]}  •  {data["displayName"]}',
                f'ID: `{data["id"]}`',
            )
            for data in friends["data"]
        ]
        # \nCreated: {discord.utils.format_dt(parser.parse(data["created"]), "D")} # api gives inacurate date, saving for later if ever fixed

        p = FieldPageSource(entries, per_page=4)
        p.embed.title = f"{name}'s friends"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

    @to_thread
    def scrape_info(self, text: str, embed: discord.Embed) -> discord.Embed:
        scraper = BeautifulSoup(text, "html.parser")
        description = scraper.find(id="item-details-description")
        price = scraper.find(class_="text-robux-lg wait-for-i18n-format-render")
        clothing_type = scraper.find(
            id="type-content", class_="font-body text wait-for-i18n-format-render"
        ).text  # type: ignore
        title_div = scraper.find(class_="border-bottom item-name-container")
        title: str = title_div.find("h1").text  # type: ignore
        price = f"{price.text} robux" if price else "Not for sale"
        creator: bs4.Tag = scraper.find(class_="text-name")  # type: ignore

        embed.add_field(name="Price", value=price, inline=True)
        embed.title = title

        embed.add_field(name="Type", value=clothing_type, inline=True)

        embed.add_field(
            name="Creator", value=f'[{creator.text}]({creator["href"]})', inline=True
        )

        if description:
            embed.add_field(name="Description", value=description.text, inline=False)

        return embed

    @commands.command(name="asset", aliases=("template",))
    async def asset(
        self,
        ctx: Context,
        assetID: int = commands.parameter(
            converter=RobloxAssetConverter, displayed_default="[roblox asset url/id]"
        ),
    ):
        """Gets the template from a given asset url or id"""
        asset = await template(ctx.session, assetID)
        embed = discord.Embed()
        embed.set_image(url=f"attachment://asset.png")
        file = discord.File(fp=asset, filename=f"asset.png")
        url = f'https://www.roblox.com/catalog/{assetID}/"'
        embed.url = url

        async with self.bot.session.get(url) as r:
            text = await r.text()
            embed = await self.scrape_info(text, embed)

        await ctx.send(embed=embed, file=file, reference=ctx.message)
