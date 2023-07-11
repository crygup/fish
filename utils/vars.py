from dataclasses import dataclass

import discord

base_header = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
}


@dataclass()
class GoogleImageData:
    image_url: str
    url: str
    snippet: str
    query: str
    author: discord.User | discord.Member


@dataclass()
class SpotifySearchData:
    track: str
    album: str
    artist: str
