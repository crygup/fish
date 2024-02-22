from dataclasses import dataclass

import discord

from .emojis import *

USER_FLAGS = {
    "staff": f"{staff} Discord Staff",
    "partner": f"{partner} Discord Partner",
    "hypesquad": f"{hypesquad} HypeSquad",
    "bug_hunter": f"{bughunterlv1} Bug Hunter",
    "bug_hunter_level_2": f"{bughunterlv2} Bug Hunter 2",
    "hypesquad_bravery": f"{bravery} HypeSquad Bravery",
    "hypesquad_brilliance": f"{brillance} HypeSquad Brilliance",
    "hypesquad_balance": f"{balance} HypeSquad Balance",
    "early_supporter": f"{earlysupporter} Early Supporter",
    "verified_bot_developer": f"{bot_dev} Bot Developer",
    "verified_bot": f"{bot} Verified Bot",
    "discord_certified_moderator": f"{certified_moderator} Moderator",
    "system": f"{system} System",
    "active_developer": f"{active_developer} Active Developer",
    "owner": f"{fish_owner} Bot Owner",
    "server_owner": f"<:owner:949147456376033340> Server Owner",
    "booster": f"<:booster:949147430786596896> Server Booster",
    "nitro": f"<:nitro:949147454991896616> Nitro",
    639539828400062485: f"{jpj} jpjordon",
    117666744021024771: f"{bfr} bfr",
    592310159133376512: f"{razy} Razy",
    809275012980539453: f"{regor} Regor",
    671777334906454026: f"{kaylynn} Kaylynn",
    780272096688996363: f"{kami} Kami",
    466325650710724619: f"{lunachup} Lunachup!",
    121738169900204034: f"{mv} mv",
    479993118737956874: f"{skeezr} Skeezr",
    117603839858835460: f"{spike} Spike",
    364105382261424128: f"{eli} Eli",
    309409635327148033: f"{tuco} 2co",
    725443744060538912: f"{cola} Cola",
    396923243803574282: f"{yaz} Yaz",
    349373972103561218: f"{leo} Leonardo",
    1034697215991619584: f"{leog} Leo",
    391004605032431616: f"{monark} Monark",
    198145509242699777: f"{jawn} Jawn",
    659385299809599500: f"{samir} Samir",
    829421985846263839: f"{sybel} Sybel",
    739641420645924986: f"{drew} Drewskeky"
}

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
