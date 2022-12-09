import re

# https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/utils/time.py#L23-L34
short_time = re.compile(
    r"""
        (?:(?P<years>[1-9])(?:years?|y))?         
        (?:(?P<months>[1-9]{1,2})(?:months?|mo))? 
        (?:(?P<weeks>[1-9]{1,4})(?:weeks?|w))?    
        (?:(?P<days>[1-9]{1,5})(?:days?|d))?      
        (?:(?P<hours>[1-9]{1,5})(?:hours?|h))?    
        (?:(?P<minutes>[1-9]{1,5})(?:minutes?|m))?
        (?:(?P<seconds>[1-9]{1,5})(?:seconds?|s))?
    """,
    re.VERBOSE,
)

beatmapset_re = re.compile(
    r"https://osu.ppy.sh/beatmapsets/(?P<set>[0-9]{1,})#(?P<mode>osu|taiko|fruits|mania)/(?P<map>[0-9]{1,})"
)
beatmap_re = re.compile(r"https://osu.ppy.sh/beatmaps/(?P<id>[0-9]{1,})")
id_re = re.compile(r"(?P<id>[0-9]{1,})")
