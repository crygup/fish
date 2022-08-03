import datetime
import re
from typing import Dict, List, Optional, Union

import discord
import psutil
from bot import Bot
from bot import Bot, Context
from discord.ext import commands
from utils import (
    GuildContext,
    SteamClient,
    SteamConverter,
    SteamIDConverter,
    Unauthorized,
    human_join,
    human_timedelta,
)

from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Miscellaneous(bot))


class MyHelp(commands.HelpCommand):
    context: GuildContext

    async def send_bot_help(self, mapping: Dict[commands.Cog, List[commands.Command]]):
        bot = self.context.bot
        ctx = self.context
        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{bot.user.name} help", icon_url=bot.user.display_avatar.url
        )
        for cog, commands in mapping.items():
            if filtered_commands := await self.filter_commands(commands):
                if len(commands) == 0:
                    continue

                if cog is None:
                    continue

                embed.add_field(
                    name=cog.qualified_name.capitalize(),
                    value=human_join(
                        [
                            f"**`{command.qualified_name}`**"
                            for command in cog.get_commands()
                        ],
                        final="and",
                    )
                    or "No commands found here.",
                    inline=False,
                )

        await ctx.send(embed=embed)

    async def send_command_help(self, command: commands.Command):
        bot = self.context.bot
        ctx = self.context

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{command.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = (
            f"```{command.help}```" if command.help else "No help yet..."
        )

        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in command.aliases], final="and"
                ),
                inline=False,
            )

        if command.cooldown:
            cd = command.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        await ctx.send(embed=embed)

    async def send_group_help(self, group: commands.Group):
        bot = self.context.bot
        ctx = self.context

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{group.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{group.help}```" if group.help else "No help yet..."

        if group.commands:
            embed.add_field(
                name="Commands",
                value=human_join(
                    [f"**`{command.name}`**" for command in group.commands], final="and"
                ),
                inline=False,
            )

        if group.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in group.aliases], final="and"
                ),
                inline=False,
            )

        if group.cooldown:
            cd = group.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        await ctx.send(embed=embed)

    async def send_cog_help(self, cog: commands.Cog):
        ctx = self.context
        bot = ctx.bot

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{cog.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{cog.description}```" or "No help yet..."

        commands = await self.filter_commands(cog.get_commands())

        if commands:
            embed.add_field(
                name="Commands",
                value=human_join(
                    [f"**`{command.name}`**" for command in cog.get_commands()],
                    final="and",
                ),
                inline=False,
            )

        if hasattr(cog, "aliases"):
            embed.add_field(name="Aliases", value=human_join([f"**`{alias}`**" for alias in cog.aliases], final="and"), inline=False)  # type: ignore

        await ctx.send(embed=embed)

    async def send_error_message(self, error: commands.CommandError):
        ctx = self.context
        bot = ctx.bot

        if bot.user is None:
            return

        pattern = re.compile(r'No command called "(?P<name>[a-zA-Z0-9]{1,25})" found.')
        results = pattern.match(str(error))

        if results:
            error_command_name = results.group("name").lower()

            for name, cog in bot.cogs.items():
                if error_command_name == cog.qualified_name.lower():
                    await self.send_cog_help(cog)
                    return

                if hasattr(cog, "aliases"):
                    if error_command_name in cog.aliases:  # type: ignore
                        _cog = bot.get_cog(cog.qualified_name)

                        if _cog is None:
                            continue

                        await self.send_cog_help(_cog)
                        return

        else:
            await ctx.send(str(error))


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
        self._og_help = commands.DefaultHelpCommand()
        self.bot.help_command = MyHelp()
        self.process = psutil.Process()

    async def cog_unload(self):
        self.bot.help_command = self._og_help

    async def cog_load(self) -> None:
        self.bot.help_command = MyHelp()

    @commands.command(name="invite", aliases=("join",))
    async def invite(self, ctx: commands.Context):
        """Sends an invite link to the bot"""
        bot = self.bot
        if bot.user is None:
            return

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

        await ctx.send(
            f'{discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot",))}'
        )

    @commands.command(name="about")
    async def about(self, ctx: Context):
        """Tells you information about the bot itself."""

        if ctx.bot.user is None:
            raise TypeError("Bot is not logged in.")

        embed = discord.Embed(
            description="An information bot made by cr#0333.",
            timestamp=ctx.bot.user.created_at,
            color=ctx.bot.embedcolor,
        )
        embed.add_field(
            name="Guilds",
            value=f"{len(ctx.bot.guilds):,}",
        )
        embed.add_field(
            name="Uptime",
            value=human_timedelta(
                ctx.bot.uptime, accuracy=None, brief=True, suffix=False
            ),
        )
        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
        embed.add_field(
            name="Process", value=f"{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU"
        )
        embed.set_footer(text="Created at")
        await ctx.send(embed=embed)

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
