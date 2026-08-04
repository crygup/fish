from __future__ import annotations

import random
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from core import SILENT_COMMAND_USERS, Cog
from utils import get_or_fetch_user
from utils.paths import FILES_ROOT

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


def crab_command():
    async def predicate(ctx: Context) -> bool:
        if ctx.author.id in SILENT_COMMAND_USERS["crab"]:
            return False

        return True

    return commands.check(predicate)


class Corn(Cog):
    """Corn reaction leaderboards."""

    emoji = discord.PartialEmoji(name="\U0001f33d")

    @commands.group(name="corn", invoke_without_command=True)
    async def corn(
        self,
        ctx: Context,
        *,
        target: Optional[str] = None,
    ):
        """See the corn leaderboard for this server, another server, or a user."""
        if target is None:
            if not ctx.guild:
                raise commands.BadArgument(
                    "Provide a guild ID, user, or use `corn global`."
                )
            return await self._server_leaderboard(ctx, ctx.guild)

        if target.isdigit():
            guild = ctx.bot.get_guild(int(target))
            if guild is not None:
                return await self._server_leaderboard(ctx, guild)

        try:
            user = await commands.UserConverter().convert(ctx, target)
        except commands.UserNotFound:
            raise commands.BadArgument(
                "Could not find a user or server with that input."
            )
        await self._user_stats(ctx, user)

    @corn.command(name="global")
    async def corn_global(self, ctx: Context):
        """Global corn leaderboard, totals across all servers."""
        givers = await ctx.bot.pool.fetch(
            "SELECT giver_id, COUNT(*) AS total FROM corn_reacts "
            "GROUP BY giver_id ORDER BY total DESC LIMIT 5"
        )
        receivers = await ctx.bot.pool.fetch(
            "SELECT receiver_id, COUNT(*) AS total FROM corn_reacts "
            "GROUP BY receiver_id ORDER BY total DESC LIMIT 5"
        )

        if not givers and not receivers:
            await ctx.send("No corn reactions yet!")
            return

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name="Corn Leaderboard  •  Global")

        if givers:
            lines = []
            for r in givers:
                user = await get_or_fetch_user(ctx.bot, r["giver_id"])
                name = user.display_name if user else str(r["giver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Givers", value="\n".join(lines), inline=True)
        if receivers:
            lines = []
            for r in receivers:
                user = await get_or_fetch_user(ctx.bot, r["receiver_id"])
                name = user.display_name if user else str(r["receiver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Receivers", value="\n".join(lines), inline=True)
        await ctx.send(embed=embed)

    async def _server_leaderboard(self, ctx: Context, guild: discord.Guild) -> None:
        givers = await ctx.bot.pool.fetch(
            "SELECT giver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE guild_id = $1 GROUP BY giver_id ORDER BY total DESC LIMIT 5",
            guild.id,
        )
        receivers = await ctx.bot.pool.fetch(
            "SELECT receiver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE guild_id = $1 GROUP BY receiver_id ORDER BY total DESC LIMIT 5",
            guild.id,
        )
        if not givers and not receivers:
            await ctx.send(f"No corn reactions in **{guild.name}** yet!")
            return
        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(
            name=f"Corn Leaderboard  •  {guild.name}",
            icon_url=guild.icon.url if guild.icon else None,
        )
        if givers:
            lines = []
            for r in givers:
                user = guild.get_member(r["giver_id"]) or await get_or_fetch_user(
                    ctx.bot, r["giver_id"]
                )
                name = user.display_name if user else str(r["giver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Givers", value="\n".join(lines), inline=True)
        if receivers:
            lines = []
            for r in receivers:
                user = guild.get_member(r["receiver_id"]) or await get_or_fetch_user(
                    ctx.bot, r["receiver_id"]
                )
                name = user.display_name if user else str(r["receiver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Receivers", value="\n".join(lines), inline=True)
        await ctx.send(embed=embed)

    async def _user_stats(self, ctx: Context, user: discord.User) -> None:
        given_total = await ctx.bot.pool.fetchval(
            "SELECT COUNT(*) FROM corn_reacts WHERE giver_id = $1", user.id
        )
        received_total = await ctx.bot.pool.fetchval(
            "SELECT COUNT(*) FROM corn_reacts WHERE receiver_id = $1", user.id
        )

        if not given_total and not received_total:
            await ctx.send(
                f"**{user.display_name}** hasn't given or received any corns yet!"
            )
            return

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(
            name=f"Corn Stats  •  {user.display_name}", icon_url=user.display_avatar.url
        )

        given_rows = await ctx.bot.pool.fetch(
            "SELECT receiver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE giver_id = $1 GROUP BY receiver_id ORDER BY total DESC LIMIT 5",
            user.id,
        )
        if given_rows:
            lines = []
            for r in given_rows:
                target = await get_or_fetch_user(ctx.bot, r["receiver_id"])
                name = target.display_name if target else str(r["receiver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(
                name=f"Given ({given_total:,})", value="\n".join(lines), inline=True
            )
        else:
            embed.add_field(name="Given (0)", value="*None*", inline=True)

        received_rows = await ctx.bot.pool.fetch(
            "SELECT giver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE receiver_id = $1 GROUP BY giver_id ORDER BY total DESC LIMIT 5",
            user.id,
        )
        if received_rows:
            lines = []
            for r in received_rows:
                target = await get_or_fetch_user(ctx.bot, r["giver_id"])
                name = target.display_name if target else str(r["giver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(
                name=f"Received ({received_total:,})",
                value="\n".join(lines),
                inline=True,
            )
        else:
            embed.add_field(name="Received (0)", value="*None*", inline=True)

        await ctx.send(embed=embed)

    @commands.command(name="crab")
    @crab_command()
    async def crab_corn(self, ctx: Context):
        """Send a random crab quote."""
        things = [
            "atreus never ever comment that on profile ever again",
            "Frick off atreus",
            "-rep explain our friend group \nle me:we are ohio only in ohio yeah laughs hystircally",
            "hattori i wont frick you bc im NOT JAY",
            "bro really fricking just doxxes me",
            "bros been downloading destiny for 2 weeks",
            "why is there a meat emoji in the +rep comment.. diddy",
            "I am not crazy! I know he swapped those numbers! I knew it was 1216. One after Magna Carta. As if I could ever make such a mistake. Never. Never! I just - I just couldn't prove it. He - he covered his tracks, he got that idiot at the copy shop to lie for him. You think this is something? You think this is bad? This? This chicanery? He's done worse. That billboard! Are you telling me that a man just happens to fall like that? No! He orchestrated it! Jimmy! He defecated through a sunroof! And I saved him! And I shouldn't have. I took him into my own firm! What was I thinking? He'll never change. He'll never change! Ever since he was 9, always the same! Couldn't keep his hands out of the cash drawer! But not our Jimmy! Couldn't be precious Jimmy! Stealing them blind! And he gets to be a lawyer!? What a sick joke! I should've stopped him when I had the chance! And you - you have to stop him!",
            "You look down on me? You pity me? Walk away, that's right Howard. Y'know why I didn't take the job? Because It's too small! I don't care about it. It's nothing to me. It's a bacterium! I travel in worlds you can't even imagine! You can't conceive of what I'm capable of! I'm so far beyond you! I'm like a god in human clothing. LIGHTNING BOLTS SHOOT FROM MY FINGERTIPS!",
            "hattori you cornball you deleted every comment on your profile fake ass",
            "you can get jiggy and shake ur pu-",
            "ayatolah in iran",
            "aaaaaaaaa my grandpa fought in world War 2 he was such a noble dude and i cant even finish school.. missed my mom and left soon his dad was a fire man who fu*ked fire so violent i think i bored my therapist while playing him my violin",
            "bro this guy.. is a butthole but the adult version.. he.. FRICKING.. ruined my gyatt.. and kelgorath.. youe just a weak butt the adult version boss.. so.. this is my comment.. peace out!",
            "i kinda miss tabole ngl... oh how the times go... pls tell mv to play spire with me BY THE WAY",
            "I just want to see im sorry for everything i have done for you and to anybody else in table, im really sorry",
            "https://crygup.com/images/crab/crab1.jpg",
            "https://crygup.com/images/crab/crab2.png",
            "https://crygup.com/images/crab/crab3.png",
            'I know that 69 means ||" sex number" ||, but what does 420 mean?',
            "I just say I'm 15 sometimes cuz irl I look like I'm 20",
            'I know that 69 means ||" sex number" ||, but what does 420 mean?',
            "Here's who I am:|| IAm the one who cares about the people and  very kind||",
            "VIDEO",
            "https://discord.com/channels/848507662437449750/884188416835723285/993962175318216846",
            "Mf hattori not accepting the friend rwq",
            "If I don't get beginner before I sleep I will commit unalive",
            "3 things I need to do with my ggf\n\n-HEAD SHOULDERS KNEE\n\n-HUG HER\n\n-KILL HER",
            "The diary of a wimpy kid is the coolest thing I've ever read",
        ]

        choice = random.choice(things)

        if choice == "VIDEO":
            videos = [FILES_ROOT / "videos" / "crab_rock.mp4"]
            return await ctx.send(
                file=discord.File(random.choice(videos), filename="crab.mp4")
            )

        await ctx.send(choice)


async def setup(bot: Fishie):
    await bot.add_cog(Corn())
