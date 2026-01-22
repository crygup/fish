# from __future__ import annotations

# from typing import TYPE_CHECKING, Any, Dict, Literal, TypeAlias, Union

# import discord
# from discord.ext import commands
# from discord import Optional, app_commands

# from core import Cog
# from utils import (
#     lastfm_command,
#     LastfmTimeConverter,
#     interaction_only,
#     SimplePages,
#     plural,
#     lastfm_period,
# )

# if TYPE_CHECKING:
#     from core import Fishie
#     from extensions.context import Context
#     from .__init__ import Lastfm


# class WhoKnows(Cog):

#     async def populate_artist(self, ctx: Context, artist: str):
#         results = await self.bot.pool.fetch("SELECT user_id, lastfm FROM accounts")

#         for row in results:
#             data = {"method": "artist.getinfo", "artist": artist, "username": row["lastfm"]}
#             r = await self.bot.lfm_get(data)
#             count = int(r["artist"]["stats"]["userplaycount"])

            

#     @commands.hybrid_group(name="whoknows", fallback="artist", aliases=("wk",))
#     @app_commands.allowed_installs(guilds=True, users=True)
#     @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
#     @lastfm_command()
#     async def wk_group(
#         self,
#         ctx: Context,
#         artist: Optional[str] = None,
#     ):
#         """Show your top artists"""

#         async with ctx.typing():
#             lfm_name = self.bot.db_cache.lastfm[ctx.author.id]
#             if not artist:
#                 data = {"method": "user.getRecentTracks", "user": lfm_name, "limit": 1}

#                 r: Dict[Any, Any] = await self.bot.lfm_get(data)
#                 artist = r["recenttracks"]["track"][0]["artist"]["#text"]

#             data = {"method": "artist.getinfo", "artist": artist, "username": lfm_name}

#             r: Dict[Any, Any] = await self.bot.lfm_get(data)

#             if r.get("error"):
#                 raise commands.BadArgument("Specified artist could not be found")
            
#             await ctx.send(artist)
