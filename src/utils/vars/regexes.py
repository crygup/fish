from re import Pattern, compile as comp, X

# fmt: off
# imgur
IMGUR_PAGE_RE: Pattern = comp(r"https?://(www\.)?imgur.com/(\S+)/?")
IMAGE_URL_RE: Pattern = comp(r"^(https?://)(\S)*((?P<filename>png|jpe?g|gif|webp)$)")

# osu
OSU_BEATMAPSET_RE: Pattern = comp(r"https://osu.ppy.sh/beatmapsets/(?P<set>[0-9]{1,})#(?P<mode>osu|taiko|fruits|mania)/(?P<map>[0-9]{1,})")
OSU_BEATMAP_RE: Pattern = comp(r"https://osu.ppy.sh/beatmaps/(?P<id>[0-9]{1,})")
OSU_ID_RE: Pattern = comp(r"(?P<id>[0-9]{1,})")

# tenor
TENOR_PAGE_RE: Pattern = comp(r"https?://(www\.)?tenor\.com/view/\S+/?")
TENOR_GIF_RE: Pattern = comp(r"https?://(www\.)?c\.tenor\.com/\S+/\S+\.gif/?")

# discord
CUSTOM_EMOJI_RE: Pattern = comp(r"<(a)?:([a-zA-Z0-9_]{2,32}):([0-9]{18,22})>")
BOT_MENTION_RE: Pattern = comp(r"<@!?876391494485950504>")
DISCORD_ID_RE: Pattern = comp(r"([0-9]{13,21})")

# sites
TIKTOK_RE: Pattern = comp(r"https://(www|vt|vm|m).tiktok.com/(@)?[a-zA-Z0-9_-]{3,}(/video/[0-9]{1,})?")
INSTAGRAM_RE: Pattern = comp(r"https://(www.)?instagram.com/(p|tv|reel)/[a-zA-Z0-9-_]{5,}")
TWITCH_RE: Pattern = comp(r"https?://clips.twitch.tv/[a-zA-Z0-9_-]")
TWITTER_RE: Pattern = comp(r"https?://twitter.com/[a-zA-Z0-9_]{1,}/status/[0-9]{19}")
REDDIT_RE: Pattern = comp(r"https?://(www.)reddit.com/r/[a-zA-Z0-9_-]{1,20}/comments/[a-z0-9]{6}")
YT_CLIP_RE: Pattern = comp(r"https://(www.)?youtube.com/clip/[A-Za-z0-9_-]{1,}")
YT_SHORT_RE: Pattern = comp(r"https://(www.)?youtube.com/shorts/[a-zA-Z0-9_-]{11}")
YOUTUBE_RE: Pattern = comp(r"https://(www.)?youtu(.be|be.com)/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11})")
SOUNDCLOUD_RE: Pattern = comp(r"https?://(on.)?soundcloud.com/[a-zA-Z0-9_-]{3,25}/?([a-z0-9_-]{3,255})?")

# misc
VIDEOS_RE = comp(
    r"""
    (https://(www|vt|vm|m).tiktok.com/(@)?[a-zA-Z0-9_-]{3,}(/video/[0-9]{1,})?)?
    (https://(www.)?instagram.com/(p|tv|reel)/[a-zA-Z0-9-_]{5,})?
    (https?://clips.twitch.tv/[a-zA-Z0-9_-])?
    (https?://twitter.com/[a-zA-Z0-9_]{1,}/status/[0-9]{19})?
    (https?://(www.)reddit.com/r/[a-zA-Z0-9_-]{1,20}/comments/[a-z0-9]{6})?
    (https://(www.)?youtube.com/clip/[A-Za-z0-9_-]{1,})?
    (https://(www.)?youtube.com/shorts/[a-zA-Z0-9_-]{11})?
    (https://(www.)?youtu(.be|be.com)/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11}))?
    (https?://(on.)?soundcloud.com/[a-zA-Z0-9_-]{3,25}/?([a-z0-9_-]{3,255})?)?
    """,
    X,
)
OWNER_REP_RE: Pattern = comp(r"<:Reputation_Icon:745225325063176252> \*\*(.*) has given <@766953372309127168> a reputation point!\*\*")
REP_RE: Pattern = comp(r"<:Reputation_Icon:745225325063176252> \*\*(.*) has given <@!?([0-9]{15,20})> a reputation point!\*\*")
