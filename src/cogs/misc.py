from __future__ import annotations

import datetime
import difflib
import re
import textwrap
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import discord
import psutil
from discord.ext import commands

from utils import (
    BlankException,
    ImageConverter,
    RPSView,
    SteamConverter,
    Unauthorized,
    get_or_fetch_user,
    get_steam_data,
    human_join,
    human_timedelta,
    natural_size,
    status_state,
    to_bytesio,
    response_checker,
)

from .image.functions import gif_maker, text_to_image

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Miscellaneous(bot))


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
        cr = await get_or_fetch_user(bot=self.bot, user_id=766953372309127168)

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
        icon_fp = await to_bytesio(
            ctx.session, f"https://api.genshin.dev/characters/{character}/icon"
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

    @commands.command(name="stats", hidden=True)
    @commands.cooldown(1, 30)
    async def stats(self, ctx: Context):
        """This shows a bit more info than about

        Can be hard to read for mobile users, sorry."""
        bot = self.bot
        await ctx.typing(ephemeral=True)
        members_count = sum(g.member_count for g in bot.guilds)  # type: ignore
        start = discord.utils.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        avatars = await bot.pool.fetch("""SELECT * FROM avatars""")
        avatars_today = len(
            [result for result in avatars if result["created_at"] >= start]
        )

        commands = await bot.pool.fetch("""SELECT * FROM command_logs""")
        commands_today = len(
            [result for result in commands if result["created_at"] >= start]
        )

        usernames = await bot.pool.fetch("""SELECT * FROM username_logs""")
        usernames_today = len(
            [result for result in usernames if result["created_at"] >= start]
        )

        nicknames = await bot.pool.fetch("""SELECT * FROM nickname_logs""")
        nicknames_today = len(
            [result for result in nicknames if result["created_at"] >= start]
        )

        discrims = await bot.pool.fetch("""SELECT * FROM discrim_logs""")
        discrims_today = len(
            [result for result in discrims if result["created_at"] >= start]
        )

        psql_start = perf_counter()
        await bot.pool.execute("SELECT 1")
        psql_end = perf_counter()

        redis_start = perf_counter()
        await self.bot.redis.ping()
        redis_end = perf_counter()

        members: List[discord.Member] = []
        [members.extend(g.members) for g in bot.guilds]
        spotify = [
            m
            for m in members
            if discord.utils.find(
                lambda a: isinstance(a, discord.Spotify), m.activities
            )
        ]
        games = [
            m
            for m in members
            if discord.utils.find(lambda a: isinstance(a, discord.Game), m.activities)
        ]
        activities = [m for m in members if m.activities]
        mem = self.process.memory_full_info()
        memory_usage = mem.uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

        message = f"""
        memory                 : {memory_usage:.2f} MiB
        virtual memory         : {natural_size(mem.vms)}
        cpu                    : {cpu_usage:.2f}%
        pid                    : {self.process.pid}
        threads                : {self.process.num_threads():,}
        discord.py             : {discord.__version__}
        guilds                 : {len(bot.guilds):,}
        sus guilds             : {len(ctx.sus_guilds):,}
        members                : {members_count:,}
        spotify activities     : {len(spotify):,}
        games                  : {len(games):,}
        activities             : {len(activities):,}
        users                  : {len(bot.users):,}
        emojis                 : {len(bot.emojis):,}
        stickers               : {len(bot.stickers):,}
        cogs                   : {len(bot.cogs):,}
        cached messages        : {len(bot.cached_messages):,}
        websocket latency      : {round(bot.latency * 1000, 3)}ms
        postgresql latency     : {round(psql_end - psql_start, 3)}ms
        redis latency          : {round(redis_end - redis_start, 3)}ms
        voice clients          : {len(bot.voice_clients):,}
        avatars logged         : {len(avatars):,}
        avatars logged today   : {avatars_today:,}
        usernames logged       : {len(usernames):,}
        usernames logged today : {usernames_today:,}
        discrims logged        : {len(discrims):,}
        discrims logged today  : {discrims_today:,}
        nicknames logged       : {len(nicknames):,}
        nicknames logged today : {nicknames_today:,}
        commands ran           : {len(commands):,}
        commands ran today     : {commands_today:,}
        """

        await ctx.send(f"```yaml{textwrap.dedent(message)}```")

    @commands.command(name="monark", hidden=True)
    @commands.cooldown(1, 5)
    async def monark(self, ctx: Context):
        """monark said this"""
        await ctx.send(
            "https://cdn.discordapp.com/attachments/884188416835723285/1006540930448375919/IMG_0886.jpg"
        )

    @commands.command(name="merica", hidden=True)
    @commands.cooldown(1, 5)
    async def merica(self, ctx: Context, *, text: str):
        """we love america!!!"""

        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", text)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="feedback")
    @commands.is_owner()
    async def feedback(
        self,
        ctx: Context,
        *,
        text: str = commands.parameter(displayed_default="<text>"),
    ):
        """We appreciate your love and feedback!"""
        await ctx.trigger_typing()
        boxed = await text_to_image(text)
        asset = await gif_maker(
            await ImageConverter().convert(
                ctx,
                "https://cdn.discordapp.com/attachments/1055712784458989598/1055712870857461811/feedback.gif",
            ),
            boxed,
        )

        await ctx.send(file=discord.File(asset, filename=f"feedback.gif"))

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        await ctx.send(view=RPSView(ctx))

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

    async def get_app_id_from_name(self, ctx: Context, name: str) -> int:
        records: List[Dict] = await ctx.bot.pool.fetch("SELECT * FROM steam_games")

        matching_dicts = [
            record for record in records if str(record["name"]).lower() == name.lower()
        ]

        if matching_dicts:
            return matching_dicts[0]["app_id"]

        match = difflib.get_close_matches(name, [record["name"] for record in records])

        prompt = await ctx.prompt(f"Did you mean **{match[0]}**?")

        if not prompt:
            raise BlankException(
                f"Well you can run `fish steam search game {name}` and browse for the correct one."
            )

        matching_dicts = [record for record in records if record["name"] == match[0]]

        return matching_dicts[0]["app_id"]

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
