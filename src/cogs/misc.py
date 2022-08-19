import datetime
import difflib
import re
from typing import Dict, List, Optional, Union

import discord
import psutil
from bot import Bot, Context
from discord.ext import commands
from utils import (
    FrontHelpPageSource,
    GuildContext,
    Pager,
    SteamClient,
    SteamConverter,
    Unauthorized,
    human_join,
    human_timedelta,
)

from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Miscellaneous(bot))


status_state = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to trade",
    6: "Looking to play",
}


class Miscellaneous(commands.Cog, name="miscellaneous"):
    """Miscellaneous commands."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.aliases = ["misc"]
        self.process = psutil.Process()

        perms = discord.Permissions.none()
        perms.read_messages = True
        perms.send_messages = True
        perms.read_message_history = True
        perms.embed_links = True
        perms.attach_files = True
        perms.add_reactions = True
        perms.manage_messages = True
        perms.external_emojis = True
        perms.external_stickers = True
        perms.manage_guild = True
        perms.manage_channels = True
        perms.manage_roles = True
        perms.kick_members = True
        perms.ban_members = True
        perms.manage_nicknames = True
        perms.manage_emojis_and_stickers = True

        self.invite_url = discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot",))  # type: ignore

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f4a0")

    @commands.command(name="invite", aliases=("join",))
    async def invite(self, ctx: commands.Context):
        """Sends an invite link to the bot"""

        await ctx.send(
            self.invite_url,
        )

    @commands.command(name="about")
    async def about(self, ctx: Context):
        """Tells you information about the bot itself."""

        if ctx.bot.user is None:
            return

        sql = """SELECT * FROM command_logs"""
        results = await self.bot.pool.fetch(sql)

        total = len(results)
        start = discord.utils.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        total_today = len(
            [result for result in results if result["created_at"] >= start]
        )
        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
        cr = await self.bot.getch_user(766953372309127168)

        e = discord.Embed(
            description="cool discord bot",
            timestamp=ctx.bot.user.created_at,
            color=ctx.bot.embedcolor,
        )

        e.set_footer(text="Created at")
        e.set_author(name=f"{cr}", icon_url=cr.display_avatar.url)

        e.add_field(
            name="Commands ran", value=f"{total:,} total\n{total_today:,} today"
        )
        e.add_field(
            name="Process", value=f"{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU"
        )
        e.add_field(name="Invite", value=f"[Click here]({self.invite_url})")
        e.add_field(
            name="Guilds",
            value=f"{len(ctx.bot.guilds):,}",
        )

        e.add_field(name="Users", value=f"{len(ctx.bot.users):,}")
        e.add_field(
            name="Uptime",
            value=human_timedelta(
                ctx.bot.uptime, accuracy=None, brief=True, suffix=False
            ),
        )

        await ctx.send(embed=e)

    @commands.command(name="hello", hidden=True)
    async def hello(self, ctx: Context):
        """Displays my hello message"""
        msg = "Hello! I'm a robot! cr#0333 made me."

        if ctx.bot.testing:
            msg += "\nThis is the testing version of the bot."

        await ctx.send(msg)

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

        info: Dict = await SteamClient(ctx.bot, "ISteamUser/GetPlayerSummaries", "v0002", user_id, ids=True)  # type: ignore
        games: Dict = await SteamClient(ctx.bot, "IPlayerService/GetOwnedGames", "v0001", user_id)  # type: ignore

        try:
            friends = await SteamClient(ctx.bot, "ISteamUser/GetFriendList", "v0001", user_id)  # type: ignore
            friends = len(friends["friendslist"]["friends"])
        except Unauthorized:
            friends = 0

        info = info["response"]["players"][0]
        avatar = await ctx.to_image(info["avatarfull"])
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
        info: Dict = await SteamClient(ctx.bot, "ISteamUser/GetPlayerSummaries", "v0002", user_id, ids=True)  # type: ignore
        info = info["response"]["players"][0]
        await ctx.send(f'{info["personaname"]}\'s 64bit steam ID is: `{user_id}`')

    @commands.command(name="character")
    async def character(self, ctx: Context, *, character: str):
        """Gets the information about a character."""

        pattern = re.compile(r'"(?P<name>[a-zA-Z-]{1,})"')
        async with self.bot.session.get("https://api.genshin.dev/characters") as r:
            results = await r.text()

        characters = pattern.findall(results)

        if not character.lower() in [c for c in characters]:

            message = "Character not found.\n\n"
            maybe = difflib.get_close_matches(character.lower(), characters)
            if maybe:
                message += f"Did you mean `{human_join(maybe)}`?"

            await ctx.send(message)
            return

        async with self.bot.session.get(
            f"https://api.genshin.dev/characters/{character}"
        ) as r:
            results = await r.json()

        embed = discord.Embed(
            color=ctx.bot.embedcolor, description=results["description"]
        )
        icon_fp = await ctx.to_image(
            f"https://api.genshin.dev/characters/{character}/icon"
        )
        icon_file = discord.File(icon_fp, filename=f"{character}.png")

        embed.set_author(
            name=f"{results['name']}  •  {results['rarity']} \U00002b50",
            icon_url=f"attachment://{character}.png",
        )
        embed.set_thumbnail(url=f"attachment://{character}.png")

        embed.add_field(name="Weapon", value=results["weapon"])
        embed.add_field(name="Vision", value=results["vision"])
        embed.add_field(name="Nation", value=results["nation"])
        embed.add_field(name="Affiliation", value=results["affiliation"])
        embed.add_field(name="Constellation", value=results["constellation"])
        embed.add_field(name="Birthday", value=results["birthday"])

        await ctx.send(embed=embed, file=icon_file)
