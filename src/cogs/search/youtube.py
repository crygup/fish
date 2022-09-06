import discord
from bot import Context
from discord.ext import commands
from utils import response_checker
from ._base import CogBase


class YoutubeCommands(CogBase):
    @commands.group(name="youtube", aliases=("yt",), invoke_without_command=True)
    async def youtube(self, ctx: Context, *, query: str):
        url = f"https://www.googleapis.com/youtube/v3/search"
        params = {
            "q": query,
            "key": self.bot.config["keys"]["google-search"],
            "part": "snippet",
            "type": "video",
            "maxResults": 1,
        }
        await ctx.trigger_typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            url = f"https://www.youtube.com/watch?v={data['items'][0]['id']['videoId']}"

        await ctx.send(url)

    @youtube.group(name="channel", aliases=("ch",))
    async def youtube_channel(self, ctx: Context, *, query: str):
        url = f"https://www.googleapis.com/youtube/v3/search"
        params = {
            "q": query,
            "key": self.bot.config["keys"]["google-search"],
            "part": "snippet",
            "type": "channel",
            "maxResults": 1,
        }
        await ctx.trigger_typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            url = (
                f"https://www.youtube.com/channel/{data['items'][0]['id']['channelId']}"
            )

        await ctx.send(url)

    @youtube.group(name="playlist", aliases=("pl",))
    async def youtube_playlist(self, ctx: Context, *, query: str):
        url = f"https://www.googleapis.com/youtube/v3/search"
        params = {
            "q": query,
            "key": self.bot.config["keys"]["google-search"],
            "part": "snippet",
            "type": "playlist",
            "maxResults": 1,
        }
        await ctx.trigger_typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            url = f"https://www.youtube.com/playlist?list={data['items'][0]['id']['playlistId']}"

        await ctx.send(url)
