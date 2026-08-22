from re import Pattern, X
from re import compile as comp

# fmt: off
VIDEOS_RE: Pattern[str] = comp(
    r"""
    # Keep the complete TikTok path, including video IDs.  The final slash is
    # optional because Discord links commonly omit it.
    (https://(?:(?:(?:www|vt|vm|m|vk)\.)?tiktok\.com|(?:www\.)?kktiktok\.com)/[^\s<>()]+)?
    (https://(?:(?:www\.)?instagram\.com|(?:www\.)?kkinstagram\.com)/(p|tv|reel)/[a-zA-Z0-9-_]{5,})?
    (https?://clips.twitch.tv/[a-zA-Z0-9_-])?
    (https?://(?:www\.)?(twitter|x|fxtwitter|vxtwitter|fixupx|girlcockx)\.com/[a-zA-Z0-9_]{1,}/status/[0-9]{19,})?
    (https?://(?:(?:www|old|new|np|sh|nm)\.)?reddit.com/(?:r|user|comments)/[^\s<>()]+)?
    (https?://redd.it/[a-zA-Z0-9]+)?
    (https?://(?:www\.)?threads\.(?:net|com)/[^\s<>()]+)?
    (https?://(?:www\.|m\.|web\.)?facebook.com/[^\s<>()]+)?
    (https?://fb.watch/[^\s<>()]+)?
    (https?://(?:www\.)?pixiv.net/(?:en/)?(?:artworks/\d+|member_illust\.php\?[^\s<>()]+|novel/show\.php\?[^\s<>()]+))?
    (https?://(?:www\.)?tumblr.com/[^\s<>()]+)?
    (https?://[a-zA-Z0-9-]+\.tumblr.com/(?:post|video)/\d+[^\s<>()]*)?
    (https://(www.)?youtube.com/clip/[A-Za-z0-9_-]{1,})?
    (https://(www.)?youtube.com/shorts/[a-zA-Z0-9_-]{11})?
    (https://(www.)?youtu(.be|be.com)/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11}))?
    (https?://(on.)?soundcloud.com/[a-zA-Z0-9_-]{3,25}/?([a-z0-9_-]{3,255})?)?
    (https?://(www\.pinterest\.com/pin/[0-9]{1,}/)?(pin\.it/[a-zA-Z0-9]{1,})?)?
    """,
    X,
)

# sites
TIKTOK_RE: Pattern[str] = comp(
    r"https://(?:(?:(?:www|vt|vm|m|vk)\.)?tiktok\.com|(?:www\.)?kktiktok\.com)/"
    r"(@?[a-zA-Z0-9_.]{1,})?/?(@?[a-zA-Z0-9_.]{1,})?/?(@?[a-zA-Z0-9_.]{1,})?/"
)
INSTAGRAM_RE: Pattern[str] = comp(
    r"https://(?:(?:www\.)?instagram\.com|(?:www\.)?kkinstagram\.com)/"
    r"(p|tv|reel)/[a-zA-Z0-9-_]{5,}"
)
TWITCH_RE: Pattern[str] = comp(
    r"https?://(?:clips\.twitch\.tv/[a-zA-Z0-9_-]+|(?:www\.)?twitch\.tv/[A-Za-z0-9_]{2,25}/clip/[A-Za-z0-9_-]+)"
)
# Twitter-compatible mirrors expose the same status URL structure and media.
TWITTER_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?(?:twitter|x|fxtwitter|vxtwitter|fixupx|girlcockx)\.com/[a-zA-Z0-9_]{1,}/status/[0-9]{19,}"
)
# Explicit live-stream routes that should never be passed to the downloader.
YOUTUBE_LIVE_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?youtube\.com/(?:live/[A-Za-z0-9_-]+|(?:@[A-Za-z0-9_.-]+|channel/[A-Za-z0-9_-]+|c/[A-Za-z0-9_-]+|user/[A-Za-z0-9_-]+)/live)(?:[/?#]|$)"
)
TWITCH_LIVE_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?twitch\.tv/(?!directory(?:[/?#]|$)|videos?(?:[/?#]|$)|clips?(?:[/?#]|$)|search(?:[/?#]|$)|downloads(?:[/?#]|$)|[A-Za-z0-9_]{2,25}/clip(?:[/?#]|$))[A-Za-z0-9_]{2,25}(?:[/?#]|$)"
)
KICK_LIVE_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?kick\.com/[A-Za-z0-9][A-Za-z0-9_-]{1,24}(?:[/?#]|$)"
)
LIVE_STREAM_RE: tuple[Pattern[str], ...] = (
    YOUTUBE_LIVE_RE,
    TWITCH_LIVE_RE,
    KICK_LIVE_RE,
)
REDDIT_RE: Pattern[str] = comp(
    r"https?://(?:(?:www|old|new|np|sh|nm)\.)?reddit\.com/(?:r|user|comments)/[^\s<>()]+|https?://redd\.it/[a-zA-Z0-9]+"
)
THREADS_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?threads\.(?:net|com)/[^\s<>()]+"
)
FACEBOOK_RE: Pattern[str] = comp(
    r"https?://(?:fb\.watch/[^\s<>()]+|(?:www\.|m\.|web\.)?facebook\.com/[^\s<>()]+)"
)
PIXIV_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?pixiv\.net/(?:en/)?(?:artworks/\d+|member_illust\.php\?[^\s<>()]+|novel/show\.php\?[^\s<>()]+)"
)
TUMBLR_RE: Pattern[str] = comp(
    r"https?://(?:www\.)?tumblr\.com/[^\s<>()]+|https?://[a-zA-Z0-9-]+\.tumblr\.com/(?:post|video)/\d+[^\s<>()]*"
)
YT_CLIP_RE: Pattern[str] = comp(r"https://(www.)?youtube.com/clip/[A-Za-z0-9_-]{1,}")
YT_SHORT_RE: Pattern[str] = comp(r"https://(www.)?youtube.com/shorts/[a-zA-Z0-9_-]{11}")
YOUTUBE_RE: Pattern[str] = comp(r"https://(www.)?youtu(.be|be.com)/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11})")
SOUNDCLOUD_RE: Pattern[str] = comp(r"https?://(on.)?soundcloud.com/[a-zA-Z0-9_-]{3,25}/?([a-z0-9_-]{3,255})?")
PINTEREST_RE: Pattern[str] = comp(r"https?://(www\.pinterest\.com/pin/[0-9]{1,}/)?(pin\.it/[a-zA-Z0-9]{1,})?")
ROBLOX_ASSET_RE: Pattern[str] = comp(r"(https://)(www.)?(roblox.com/catalog/)([0-9]{0,99})/[a-zA-Z-0-9]{0,999}/?")

# discord
MESSAGE_RE: Pattern[str] = comp(r"https://discord.com/channels/(?P<parent_id>@me|[0-9]{8,})/(?P<channel_id>[0-9]{8,})/(?P<message_id>[0-9]{8,})")
EMOJI_RE: Pattern[str] = comp(r"<a?:[a-zA-Z0-9\_]{1,}:[0-9]{1,}>")

# tenor
TENOR_PAGE_RE: Pattern = comp(r"https?://(www\.)?tenor\.com/view/\S+/?")
TENOR_GIF_RE: Pattern = comp(r"https?://(www\.)?c\.tenor\.com/\S+/\S+\.gif/?")

# klipy
KLIPY_RE: Pattern[str] = comp(r"https?://(?:www\.)?klipy\.com/gifs/[a-zA-Z0-9_-]+(?:[/?#]|$)")
# websites
LBD_URL_RE: Pattern[str] = comp(r"https?://(?:www\.)?letterboxd\.com/([a-zA-Z0-9_\-]+)/?")
STEAM_URL_RE: Pattern[str] = comp(r"https?://(?:www\.)?steamcommunity\.com/(?:profiles/(\d+)|id/([a-zA-Z0-9_\-]+))/?")
STEAM_ID64_RE: Pattern[str] = comp(r"^7656119\d{10}$")
STEAM_GROUP_URL_RE: Pattern[str] = comp(r"https?://steamcommunity\.com/groups?/([a-zA-Z0-9_\-]+)/?")
LASTFM_USERNAME: Pattern = comp(r"[a-zA-Z\_\-]{2,15}")
