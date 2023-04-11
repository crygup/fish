from re import Pattern, X
from re import compile as comp

# fmt: off
VIDEOS_RE: Pattern[str] = comp(
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

# sites
TIKTOK_RE: Pattern[str] = comp(r"https://(www|vt|vm|m).tiktok.com/(@)?[a-zA-Z0-9_-]{3,}(/video/[0-9]{1,})?")
INSTAGRAM_RE: Pattern[str] = comp(r"https://(www.)?instagram.com/(p|tv|reel)/[a-zA-Z0-9-_]{5,}")
TWITCH_RE: Pattern[str] = comp(r"https?://clips.twitch.tv/[a-zA-Z0-9_-]")
TWITTER_RE: Pattern[str] = comp(r"https?://twitter.com/[a-zA-Z0-9_]{1,}/status/[0-9]{19}")
REDDIT_RE: Pattern[str] = comp(r"https?://(www.)reddit.com/r/[a-zA-Z0-9_-]{1,20}/comments/[a-z0-9]{6}")
YT_CLIP_RE: Pattern[str] = comp(r"https://(www.)?youtube.com/clip/[A-Za-z0-9_-]{1,}")
YT_SHORT_RE: Pattern[str] = comp(r"https://(www.)?youtube.com/shorts/[a-zA-Z0-9_-]{11}")
YOUTUBE_RE: Pattern[str] = comp(r"https://(www.)?youtu(.be|be.com)/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11})")
SOUNDCLOUD_RE: Pattern[str] = comp(r"https?://(on.)?soundcloud.com/[a-zA-Z0-9_-]{3,25}/?([a-z0-9_-]{3,255})?")

# discord
MESSAGE_RE: Pattern[str] = comp(r"https://discord.com/channels/(?P<parent_id>@me|[0-9]{8,})/(?P<channel_id>[0-9]{8,})/(?P<message_id>[0-9]{8,})")
