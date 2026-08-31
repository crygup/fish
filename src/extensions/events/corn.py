from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from cachetools import TTLCache
from discord.ext import commands

from core import Cog, is_operational_guild
from core.badges import schedule_stat_badge_refresh
from core.handoff import is_legacy_instance

if TYPE_CHECKING:
    from core import Fishie

CORN_EMOJI = "\U0001f33d"

# Messages from these users are eligible for Fishie's automatic reactions.
# This list is intentionally explicit: the event is an opt-in easter egg for
# a small set of accounts rather than a reaction applied to every message.
CORN_REACTION_USER_IDS = frozenset(
    {
        1312685725325721651,
        1323759367371231263,
        891372917978452028,
        662378595192274974,
        766953372309127168,
        974297735559806986,
        1116395117071306903,
        1374443296637718721,
    }
)

# Mudae is eligible only in these two servers.  Keeping the restriction in a
# shared helper ensures the corn and random-reaction listeners make the same
# decision and prevents reactions to Mudae messages elsewhere.
MUDAE_BOT_ID = 432610292342587392
MUDAE_REACTION_GUILD_IDS = frozenset({1149718335261528197, 848507662437449750})

# Each tuple is one random outcome.  The 6/7 outcome is deliberately grouped
# so it is always applied as 6 first, then 7, rather than as independent rolls.
SPECIAL_REACTION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("💩",),
    ("🍦",),
    ("<:monark:860930737951997972>",),
    ("⭐",),
    ("♥️",),
    ("6️⃣", "7️⃣"),
    ("<:yaz:1162178612393410641>",),
    ("😂",),
    ("😭",),
    ("<:nocorn:1540437439561076927>",),
    ("<:Tomahto:1431002655706316924>",),
    ("🤡",),
    ("❤️",),
    ("❌",),
    ("✅",),
    ("🌊",),
    ("<:enemymissing:993455027706396712>",),
    ("<:rUpvote:895007496568188928>",),
    ("<:THIS:1448274411504271443>",),
    ("🖕",),
)

SPECIAL_REACTION_UNICODE_EMOJIS = frozenset(
    emoji
    for group in SPECIAL_REACTION_GROUPS
    for emoji in group
    if not emoji.startswith("<")
)
SPECIAL_REACTION_CUSTOM_EMOJI_IDS = frozenset(
    {
        860930737951997972,
        1162178612393410641,
        1540437439561076927,
        1431002655706316924,
        993455027706396712,
        895007496568188928,
        1448274411504271443,
    }
)


def is_special_reaction_target(user_id: int, guild_id: int | None) -> bool:
    """Return whether a message author may receive automatic reactions."""

    if int(user_id) in CORN_REACTION_USER_IDS:
        return True
    return int(user_id) == MUDAE_BOT_ID and guild_id in MUDAE_REACTION_GUILD_IDS


def is_special_reaction_emoji(emoji: object) -> bool:
    """Return whether *emoji* is one of the automatic random reactions."""

    emoji_id = getattr(emoji, "id", None)
    if emoji_id is not None:
        try:
            return int(emoji_id) in SPECIAL_REACTION_CUSTOM_EMOJI_IDS
        except (TypeError, ValueError, OverflowError):
            return False
    return str(emoji) in SPECIAL_REACTION_UNICODE_EMOJIS


class CornReacts(Cog):
    """Track corn reactions independently, one per giver per message."""

    def __init__(self) -> None:
        self._seen: TTLCache[tuple[int, int], bool] = TTLCache[tuple[int, int], bool](
            maxsize=10_000, ttl=600.0
        )

    @commands.Cog.listener("on_raw_reaction_add")
    async def on_corn_react(self, payload: discord.RawReactionActionEvent) -> None:
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(payload):
            return
        if payload.message_author_id is None:
            return
        if payload.user_id == payload.message_author_id:
            return
        if self.bot.db_cache.user_tracking_opted_out(
            payload.user_id, "corn"
        ) or self.bot.db_cache.user_tracking_opted_out(
            payload.message_author_id, "corn"
        ):
            return

        emoji = str(payload.emoji)
        if emoji != CORN_EMOJI and (
            not hasattr(payload.emoji, "name") or payload.emoji.name != "corn"
        ):
            return

        key = (payload.user_id, payload.message_id)
        if key in self._seen:
            return
        self._seen[key] = True

        try:
            await self.bot.pool.execute(
                "INSERT INTO corn_reacts "
                "(receiver_id, giver_id, guild_id, message_id, channel_id) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (giver_id, message_id) DO NOTHING",
                payload.message_author_id,
                payload.user_id,
                payload.guild_id,
                payload.message_id,
                payload.channel_id,
            )
            schedule_stat_badge_refresh(self.bot)
        except Exception:
            self._seen.pop(key, None)
            raise


async def setup(bot: Fishie):
    await bot.add_cog(CornReacts())
