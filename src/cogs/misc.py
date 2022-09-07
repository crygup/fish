import datetime
import difflib
import re
import textwrap
from time import perf_counter
from typing import Dict, List, Optional, Union

import discord
import psutil
from discord.ext import commands

from bot import Bot, Context
from cogs.context import Context
from utils import (
    SteamConverter,
    Unauthorized,
    get_steam_data,
    human_join,
    human_timedelta,
    natural_size,
    to_bytesio,
)


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
        intents value          : {bot.intents.value}
        members intent         : {bot.intents.members}
        presences intent       : {bot.intents.presences}
        message content intent : {bot.intents.message_content}
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
