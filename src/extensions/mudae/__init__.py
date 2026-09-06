from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, cast

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import Cog, is_operational_guild
from core.handoff import is_legacy_instance
from utils import mudae_circle
from utils.paths import FILES_ROOT

from .copy import (
    InvalidSourceGuild,
    SameWishCopyGuild,
    parse_copy_mode,
    parse_source_guild_id,
    prepare_wish_copy,
    validate_copy_scope,
)
from .kakera_exchange import (
    FISHIE_OWNER_ID,
    SUPPORT_GUILD_ID,
    MudaeKakeraGift,
    parse_mudae_kakera_gift,
)
from .recent_claims import (
    RecentClaim,
    RecentClaimsView,
    claim_transition,
    claiming_username,
    parse_mudae_spawn,
    reaction_count,
)
from .series_list import (
    ScrapedSeriesBundle,
    ScrapedSeriesEntry,
    SeriesListConfirmationView,
    SeriesListView,
)
from .series_scraper import (
    MudaeSeriesAutoScraper,
    MudaeSeriesBundle,
    parse_mudae_bundle_message,
    scrape_series_from_context,
)

# from .reminders import MudaeReminders
from .sphere import MudaeID, SphereCog
from .wish_list import SeriesWishEntry, SeriesWishListView
from .wishes import (
    ParsedMudaeWish,
    normalize_wish,
    parse_mudae_embed,
    split_series_values,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


MIN_WISHKAKERA = 67
SUPPORTER_BADGE_KEY = "earned:fishie_supporter"
SUPPORTER_BADGE_EMOJI_NAME = "booster_star"
SUPPORTER_BADGE_EMOJI_ID = 1544375074289885236
SUPPORTER_BADGE_TEXT = "Fishie Supporter"
SUPPORTER_MONTHLY_COINS = 100_000
WISH_EXAMPLE_FILES = {
    "character": ("Wish Example", "$im <character>", "wish_example.png"),
    "series": ("Wishseries Example", "$ima <series>", "wishseries_example.png"),
}


@dataclass(slots=True)
class _MudaeWishes:
    characters: set[str] = field(default_factory=set)
    series: set[str] = field(default_factory=set)
    kakera: set[int] = field(default_factory=set)
    # Stable IDs are stored on each series wish by migration 0085.  Keep the
    # normalized name separately so an ``unwishseries <id>`` lookup remains
    # constant-time and old rows without an id continue to work.
    series_ids: dict[int, str] = field(default_factory=dict)
    series_id_types: dict[int, str] = field(default_factory=dict)
    series_kakera: dict[str, set[int]] = field(default_factory=dict)


class Mudae(SphereCog):
    """Mudae tools"""

    emoji = mudae_circle

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        # Mudae owns the canonical wish database.  Fishie keeps a normalized
        # in-memory copy so roll notifications do not need a database query
        # for every embed that Mudae sends.
        self._mudae_wishes: dict[tuple[int, int], _MudaeWishes] = {}
        self._mudae_series_scraper = MudaeSeriesAutoScraper()
        # A manual ``scrapeseries`` response is kept by source Mudae message
        # ID so an edited/paginated bundle can update the acknowledgement that
        # was sent to the user.  The mapping is intentionally in-memory: the
        # acknowledgement is a short-lived convenience, while the catalogue
        # itself is persisted in PostgreSQL.
        self._series_scrape_responses: dict[int, discord.Message] = {}
        # Recent-claim tracking is enabled by default for every guild.  Keep
        # the setting in memory so Mudae message edits do not query PostgreSQL
        # on every gateway event; the toggle updates this cache immediately.
        self._recent_claims_enabled: dict[int, bool] = {}
        # Keep a compact snapshot of unclaimed Mudae cards so a raw message
        # update can be compared with its previous state.  The cache is
        # bounded because a busy server can produce many rolls over time.
        self._recent_claim_snapshots: dict[int, dict[str, Any]] = {}

    @staticmethod
    def _supporter_month(now: datetime | None = None) -> str:
        """Return the UTC calendar month used for supporter rewards."""

        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m")

    def _cache_supporter_badge(self, user_id: int, *, active: bool) -> None:
        """Mirror the supporter badge in the in-memory badge cache."""

        cache = getattr(self.bot, "db_cache", None)
        badges = getattr(cache, "user_badges", None)
        if not isinstance(badges, dict):
            return
        user_id = int(user_id)
        entries = badges.get(user_id)
        if not isinstance(entries, list):
            entries = []
        entries[:] = [
            entry
            for entry in entries
            if str(entry.get("badge_key") or "") != SUPPORTER_BADGE_KEY
        ]
        if active:
            entries.append(
                {
                    "emoji_name": SUPPORTER_BADGE_EMOJI_NAME,
                    "emoji_id": SUPPORTER_BADGE_EMOJI_ID,
                    "is_custom": True,
                    "animated": False,
                    "text": SUPPORTER_BADGE_TEXT,
                    "badge_key": SUPPORTER_BADGE_KEY,
                }
            )
            badges[user_id] = entries
        elif entries:
            badges[user_id] = entries
        else:
            badges.pop(user_id, None)

    async def _set_supporter_badge(self, user_id: int, *, active: bool) -> bool:
        """Activate or deactivate the durable Fishie Supporter badge."""

        pool = getattr(self.bot, "pool", None)
        execute = getattr(pool, "execute", None)
        if not callable(execute):
            self._debug(
                "Could not update Fishie Supporter badge for %s: pool unavailable",
                user_id,
            )
            return False
        try:
            if active:
                await cast(Any, execute)(
                    """
                    INSERT INTO user_badges (
                        user_id, emoji_name, emoji_id, is_custom, unicode, animated,
                        badge_key, text, badge_source, catalog_key, active,
                        revoked_at, refund_amount, revocation_reason
                    )
                    VALUES ($1, $2, $3, TRUE, FALSE, FALSE, $4, $5,
                            'owner', NULL, TRUE, NULL, 0, NULL)
                    ON CONFLICT (user_id, badge_key) DO UPDATE
                    SET emoji_name = EXCLUDED.emoji_name,
                        emoji_id = EXCLUDED.emoji_id,
                        is_custom = TRUE,
                        unicode = FALSE,
                        animated = FALSE,
                        text = EXCLUDED.text,
                        active = TRUE,
                        revoked_at = NULL,
                        refund_amount = 0,
                        revocation_reason = NULL
                    """,
                    int(user_id),
                    SUPPORTER_BADGE_EMOJI_NAME,
                    SUPPORTER_BADGE_EMOJI_ID,
                    SUPPORTER_BADGE_KEY,
                    SUPPORTER_BADGE_TEXT,
                )
            else:
                await cast(Any, execute)(
                    """
                    UPDATE user_badges
                    SET active = FALSE,
                        revoked_at = now(),
                        revocation_reason = 'No longer boosting Fishie support server'
                    WHERE user_id = $1 AND badge_key = $2 AND active
                    """,
                    int(user_id),
                    SUPPORTER_BADGE_KEY,
                )
        except asyncpg.PostgresError:
            self.bot.logger.exception(
                "Failed to %s Fishie Supporter badge for user %s",
                "activate" if active else "deactivate",
                user_id,
            )
            return False
        except Exception:
            self.bot.logger.exception(
                "Failed to %s Fishie Supporter badge for user %s",
                "activate" if active else "deactivate",
                user_id,
            )
            return False
        self._cache_supporter_badge(int(user_id), active=active)
        return True

    async def _award_supporter_monthly_coins(self, user_id: int) -> bool:
        """Award the monthly supporter reward exactly once per UTC month."""

        currency = getattr(self.bot, "currency", None)
        credit = getattr(currency, "credit", None)
        if not callable(credit):
            self._debug(
                "Skipping Fishie Supporter reward for %s: currency service unavailable",
                user_id,
            )
            return False
        reference = f"fishie_supporter:{int(user_id)}:{self._supporter_month()}"
        try:
            await cast(Any, credit)(
                int(user_id),
                SUPPORTER_MONTHLY_COINS,
                "fishie_supporter",
                reference_key=reference,
            )
        except asyncpg.UniqueViolationError:
            # The unique currency reference means this month's reward was
            # already granted, commonly after a reconnect or member update.
            return False
        except Exception:
            self.bot.logger.exception(
                "Failed to award Fishie Supporter Coins for user %s",
                user_id,
            )
            return False
        self.bot.logger.info(
            "Awarded Fishie Supporter Coins user=%s amount=%s month=%s",
            user_id,
            SUPPORTER_MONTHLY_COINS,
            self._supporter_month(),
        )
        return True

    async def _sync_supporter_member(self, member: discord.Member) -> None:
        """Synchronize one support-server member's badge and monthly reward."""

        guild = getattr(member, "guild", None)
        # Reconciliation can receive a partial member from
        # ``Guild.premium_subscribers`` without a guild attribute.  The
        # caller has already obtained it from the support guild in that case;
        # only reject an explicitly different guild.
        if guild is not None and getattr(guild, "id", None) != SUPPORT_GUILD_ID:
            return
        if not self._member_is_support_booster(member):
            await self._set_supporter_badge(member.id, active=False)
            return
        if await self._set_supporter_badge(member.id, active=True):
            await self._award_supporter_monthly_coins(member.id)

    @staticmethod
    def _member_is_support_booster(member: discord.Member) -> bool:
        """Return whether a member currently has supporter/booster status.

        Discord normally exposes this through ``premium_since``.  Some
        gateway payloads, however, deliver the managed premium-subscriber
        role update before (or without) refreshing that timestamp.  Checking
        both sources keeps badge removal and monthly rewards consistent when
        that role is added or removed.
        """

        guild = getattr(member, "guild", None)
        booster_role = getattr(guild, "premium_subscriber_role", None)
        booster_role_id = getattr(booster_role, "id", None)
        if booster_role_id is not None:
            try:
                booster_role_id = int(booster_role_id)
            except (TypeError, ValueError, OverflowError):
                booster_role_id = None
            if booster_role_id is not None:
                roles = getattr(member, "roles", None)
                if roles is not None:
                    for role in roles:
                        try:
                            if int(getattr(role, "id", 0)) == booster_role_id:
                                return True
                        except (TypeError, ValueError, OverflowError):
                            continue
                    # If Discord exposes the managed booster role, its
                    # membership is authoritative.  A stale ``premium_since``
                    # timestamp must not keep a badge/reward active after the
                    # role has been removed.
                    return False
        return getattr(member, "premium_since", None) is not None

    async def _support_booster_ids(self) -> set[int] | None:
        """Return current support-server boosters, or ``None`` if unavailable."""

        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(SUPPORT_GUILD_ID) if callable(get_guild) else None
        if guild is None:
            return None

        # ``premium_subscribers`` is Discord.py's purpose-built view of
        # members currently boosting.  Prefer it when available; it avoids
        # scanning every member on the daily reconciliation pass.
        subscribers = getattr(guild, "premium_subscribers", None)
        result: set[int] = set()
        for member in subscribers or ():
            try:
                result.add(int(member.id))
            except (AttributeError, TypeError, ValueError):
                continue

        members = list(getattr(guild, "members", ()) or ())
        # An unchunked guild may expose only a partial member cache, so its
        # subscriber list and role scan cannot safely establish who is no
        # longer boosting.  Fetch the complete member list before reconciling;
        # if fetching fails, skip removals rather than revoking valid badges
        # based on an incomplete snapshot.
        chunked = getattr(guild, "chunked", True)
        if not chunked:
            fetch_members = getattr(guild, "fetch_members", None)
            if callable(fetch_members):
                try:
                    fetched = cast(
                        AsyncIterator[discord.Member], fetch_members(limit=None)
                    )
                    members = [member async for member in fetched]
                except (discord.Forbidden, discord.HTTPException, TypeError):
                    return None
        if not members and not result:
            # An empty cache is not evidence that every booster disappeared.
            # Avoid revoking active badges when member/presence intents left
            # the guild snapshot incomplete.
            return None
        for member in members:
            if not self._member_is_support_booster(member):
                continue
            try:
                result.add(int(member.id))
            except (AttributeError, TypeError, ValueError):
                continue
        return result

    async def _reconcile_support_boosters(self) -> None:
        """Refresh supporter badges and monthly rewards for current boosters."""

        # Only the replacement bot owns supporter rewards.  The legacy
        # instance shares this cog/database during handoff; letting it run the
        # reconciliation would duplicate monthly credits and race badge state.
        if is_legacy_instance(self.bot):
            return
        current = await self._support_booster_ids()
        if current is None:
            return
        pool = getattr(self.bot, "pool", None)
        fetch = getattr(pool, "fetch", None)
        if not callable(fetch):
            return
        try:
            rows = await cast(Any, fetch)(
                """
                SELECT user_id
                FROM user_badges
                WHERE badge_key = $1 AND active
                """,
                SUPPORTER_BADGE_KEY,
            )
        except asyncpg.PostgresError:
            self.bot.logger.debug(
                "Could not reconcile Fishie Supporter badges", exc_info=True
            )
            return
        except Exception:
            self.bot.logger.debug(
                "Could not reconcile Fishie Supporter badges", exc_info=True
            )
            return

        known = set()
        for row in rows or ():
            try:
                known.add(int(self._row_value(row, "user_id")))
            except (TypeError, ValueError):
                continue

        guild = self.bot.get_guild(SUPPORT_GUILD_ID)
        for user_id in current:
            member = guild.get_member(user_id) if guild is not None else None
            if member is not None:
                await self._sync_supporter_member(member)
            else:
                # The subscriber list can contain a partial member.  Keep the
                # persisted badge active and award the monthly transaction;
                # the next member update will refresh the full cache entry.
                if await self._set_supporter_badge(user_id, active=True):
                    await self._award_supporter_monthly_coins(user_id)
        for user_id in known - current:
            await self._set_supporter_badge(user_id, active=False)

    @tasks.loop(hours=24.0)
    async def support_booster_reconcile_task(self) -> None:
        """Daily reconciliation for booster joins, leaves, and month rewards."""

        try:
            await self._reconcile_support_boosters()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Fishie Supporter reconciliation failed")

    @support_booster_reconcile_task.before_loop
    async def before_support_booster_reconcile_task(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_load(self) -> None:
        """Load persisted, per-server wish filters before handling events."""

        self._mudae_wishes.clear()
        rows = await self.bot.pool.fetch("""
            SELECT id, guild_id, user_id, wish_type, wish_value,
                   kakera_threshold
            FROM mudae_wishes
            """)
        for row in rows:
            key = (int(row["guild_id"]), int(row["user_id"]))
            wishes = self._mudae_wishes.setdefault(key, _MudaeWishes())
            wish_type = str(row["wish_type"])
            wish_value = str(row["wish_value"])
            normalized_value = normalize_wish(wish_value)
            if wish_type == "character":
                wishes.characters.add(normalized_value)
            elif wish_type == "series":
                wishes.series.add(normalized_value)
                self._cache_series_id(wishes, row, normalized_value, wish_type)
            elif wish_type == "series_kakera":
                wishes.series_kakera.setdefault(normalized_value, set())
                threshold = self._row_value(row, "kakera_threshold")
                try:
                    if threshold is not None:
                        wishes.series_kakera[normalized_value].add(int(threshold))
                except (TypeError, ValueError):
                    self.bot.logger.warning(
                        "Skipping invalid Mudae series kakera threshold %r for guild %s/user %s",
                        threshold,
                        key[0],
                        key[1],
                    )
                self._cache_series_id(wishes, row, normalized_value, wish_type)
            elif wish_type == "kakera":
                try:
                    wishes.kakera.add(int(wish_value))
                except ValueError:
                    self.bot.logger.warning(
                        "Skipping invalid Mudae kakera wish %r for guild %s/user %s",
                        wish_value,
                        key[0],
                        key[1],
                    )
        self.bot.logger.info("Cached %d Mudae wish owner(s)", len(self._mudae_wishes))

        # Auto-scraping is an explicit per-server setting.  Keep this query
        # best-effort so older test fixtures/databases (before migration 0085)
        # can still load the ordinary wish cache.
        try:
            settings = await self.bot.pool.fetch(
                "SELECT guild_id, mudae_auto_scrape_series FROM guild_settings"
            )
        except Exception:
            settings = ()
        for row in settings:
            enabled = self._row_value(row, "mudae_auto_scrape_series", False)
            if enabled:
                guild_id = self._row_value(row, "guild_id")
                if guild_id is not None:
                    self._mudae_series_scraper.set_enabled(int(guild_id))

        try:
            recent_settings = await self.bot.pool.fetch(
                "SELECT guild_id, mudae_recent_claims FROM guild_settings"
            )
        except Exception:
            recent_settings = ()
        for row in recent_settings:
            guild_id = self._row_value(row, "guild_id")
            if guild_id is None:
                continue
            enabled = self._row_value(row, "mudae_recent_claims", True)
            self._recent_claims_enabled[int(guild_id)] = (
                True if enabled is None else bool(enabled)
            )

        # Synchronize supporter badges/rewards once at startup and then daily.
        # ``before_loop`` waits for the gateway to be ready, so the first loop
        # iteration performs the initial reconciliation with a populated guild
        # cache.  Never start it on the legacy instance: both processes share
        # the database and only the replacement bot should grant rewards.
        wait_until_ready = getattr(self.bot, "wait_until_ready", None)
        if (
            not is_legacy_instance(self.bot)
            and callable(wait_until_ready)
            and not self.support_booster_reconcile_task.is_running()
        ):
            try:
                self.support_booster_reconcile_task.start()
            except RuntimeError:
                # A cog can be loaded by a management/test process without a
                # running event loop.  The live bot will start the task when
                # loaded in its event loop; do not make startup fail here.
                self.bot.logger.debug(
                    "Could not start Fishie Supporter reconciliation task",
                    exc_info=True,
                )

    def cog_unload(self) -> None:
        task = self.support_booster_reconcile_task
        if task.is_running():
            task.cancel()

    @staticmethod
    def _row_value(row: object, key: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return getattr(row, key, default)

    def _debug(self, message: str, *args: object) -> None:
        logger = getattr(self.bot, "logger", None)
        debug = getattr(logger, "debug", None)
        if callable(debug):
            debug(message, *args)

    @staticmethod
    def _recent_claim_embed_data(embed: object) -> dict[str, Any]:
        """Copy the small portion of an embed needed for claim detection."""

        if isinstance(embed, dict):
            return dict(embed)
        to_dict = getattr(embed, "to_dict", None)
        if callable(to_dict):
            try:
                value = to_dict()
            except Exception:
                value = None
            if isinstance(value, dict):
                return value

        author = getattr(embed, "author", None)
        footer = getattr(embed, "footer", None)
        return {
            "author": {
                "name": getattr(author, "name", None),
            },
            "description": getattr(embed, "description", "") or "",
            "footer": {
                "text": getattr(footer, "text", None),
            },
        }

    @classmethod
    def _recent_claim_snapshot(
        cls,
        source: object,
        *,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make a stable, raw-message-like snapshot for edit comparisons."""

        if isinstance(source, dict):
            data: dict[str, Any] = dict(source)
        else:
            data = {
                "id": getattr(source, "id", None),
                "guild_id": getattr(getattr(source, "guild", None), "id", None),
                "channel_id": getattr(getattr(source, "channel", None), "id", None),
                "edited_timestamp": getattr(source, "edited_at", None),
                "timestamp": getattr(source, "created_at", None),
            }
            author = getattr(source, "author", None)
            if author is not None:
                data["author"] = {"id": getattr(author, "id", None)}
            embeds = getattr(source, "embeds", None)
            if embeds is not None:
                data["embeds"] = [
                    cls._recent_claim_embed_data(embed) for embed in embeds
                ]
            reactions = getattr(source, "reactions", None)
            if reactions is not None:
                raw_reactions: list[dict[str, Any]] = []
                for reaction in reactions:
                    emoji = getattr(reaction, "emoji", None)
                    raw_reactions.append(
                        {
                            "count": getattr(reaction, "count", 0),
                            "emoji": {
                                "id": getattr(emoji, "id", None),
                                "name": getattr(emoji, "name", None),
                            },
                        }
                    )
                data["reactions"] = raw_reactions

        if fallback is not None:
            merged = dict(fallback)
            merged.update(data)
            data = merged

        embeds = data.get("embeds")
        if not isinstance(embeds, (list, tuple)) or not embeds:
            return None
        data["embeds"] = [cls._recent_claim_embed_data(embed) for embed in embeds]
        reactions = data.get("reactions")
        if reactions is None:
            data["reactions"] = []
        elif not isinstance(reactions, (list, tuple)):
            data["reactions"] = []
        return data

    @staticmethod
    def _recent_claim_timestamp(snapshot: dict[str, Any]) -> datetime:
        """Return the edit time carried by Discord, falling back to now."""

        value = snapshot.get("edited_timestamp") or snapshot.get("edited_at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            parsed = discord.utils.parse_time(value)
            if parsed is not None:
                return parsed
        return discord.utils.utcnow()

    @classmethod
    def _recent_claim_event_key(
        cls,
        snapshot: dict[str, Any],
        *,
        message_id: int,
        claimant_id: int,
    ) -> str:
        """Build a stable idempotency key for one Mudae message edit."""

        edited = snapshot.get("edited_timestamp") or snapshot.get("edited_at")
        if isinstance(edited, datetime):
            if edited.tzinfo is None:
                edited = edited.replace(tzinfo=timezone.utc)
            edited = edited.isoformat()
        if isinstance(edited, str) and edited.strip():
            parsed = discord.utils.parse_time(edited.strip())
            return parsed.isoformat() if parsed is not None else edited.strip()
        return f"{message_id}:{reaction_count(snapshot)}:{claimant_id}"

    def _recent_claims_enabled_for(self, guild_id: int) -> bool:
        """Return the cached toggle, defaulting to enabled for new guilds."""

        return self._recent_claims_enabled.get(int(guild_id), True)

    def _cache_recent_claim_message(self, message: object) -> None:
        """Remember a valid Mudae card for a later edit event."""

        guild = getattr(message, "guild", None)
        message_id = getattr(message, "id", None)
        if guild is None or message_id is None:
            return
        guild_id = getattr(guild, "id", None)
        if guild_id is None or not self._recent_claims_enabled_for(int(guild_id)):
            return
        snapshot = self._recent_claim_snapshot(message)
        if snapshot is None:
            return
        embeds = snapshot.get("embeds") or ()
        if not embeds or not parse_mudae_spawn(embeds[0], allow_claimed=True):
            return
        self._recent_claim_snapshots[int(message_id)] = snapshot
        # Keep the cache bounded without introducing a second dependency.
        while len(self._recent_claim_snapshots) > 5000:
            self._recent_claim_snapshots.pop(next(iter(self._recent_claim_snapshots)))

    async def _resolve_recent_claimant(
        self,
        guild: discord.Guild,
        snapshot: dict[str, Any],
        message: discord.Message | None = None,
    ) -> tuple[int, str]:
        """Resolve Mudae's visible claimant username to a guild member."""

        snapshot_embed = next(iter(snapshot.get("embeds") or ()), None)
        visible_name = claiming_username(snapshot_embed) if snapshot_embed else None
        normalized_name = (visible_name or "").casefold().strip()
        if normalized_name:
            for member in getattr(guild, "members", ()):
                names = {
                    str(getattr(member, "name", "")).casefold(),
                    str(getattr(member, "global_name", "") or "").casefold(),
                }
                if normalized_name in names:
                    return int(member.id), str(getattr(member, "name", visible_name))

        # With a custom `$setfooter`, Mudae may omit the username.  The claim
        # reaction still identifies the claimant when the message is cached;
        # use it as a best-effort fallback rather than guessing from a footer.
        if message is not None:
            for reaction in getattr(message, "reactions", ()) or ():
                users = getattr(reaction, "users", None)
                if not callable(users):
                    continue
                try:
                    async for user in cast(Any, users(limit=25)):
                        if getattr(user, "id", None) == MudaeID:
                            continue
                        return int(user.id), str(getattr(user, "name", "Unknown"))
                except (discord.Forbidden, discord.HTTPException):
                    continue
        return 1, "Unknown"

    async def _record_recent_claim_edit(
        self,
        before: object,
        after: object,
        *,
        guild: discord.Guild,
        message: discord.Message | None = None,
    ) -> None:
        """Persist one unclaimed-to-claimed Mudae card transition."""

        if not self._recent_claims_enabled_for(guild.id):
            return
        transition = claim_transition(before, after)
        if transition is None:
            return
        after_snapshot = self._recent_claim_snapshot(after)
        if after_snapshot is None:
            return
        message_id = after_snapshot.get("id") or getattr(message, "id", None)
        channel_id = after_snapshot.get("channel_id")
        if channel_id is None and message is not None:
            channel_id = getattr(getattr(message, "channel", None), "id", None)
        if message_id is None or channel_id is None:
            return
        try:
            message_id = int(message_id)
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            return
        embeds = after_snapshot.get("embeds") or ()
        if not embeds:
            return
        claimant_id, claimant_name = await self._resolve_recent_claimant(
            guild, after_snapshot, message
        )
        claimed_at = self._recent_claim_timestamp(after_snapshot)
        event_key = self._recent_claim_event_key(
            after_snapshot,
            message_id=message_id,
            claimant_id=claimant_id,
        )
        character = transition[1].character.strip()
        try:
            await self.bot.pool.execute(
                """
                INSERT INTO mudae_recent_claims (
                    guild_id, channel_id, message_id, character_name,
                    claiming_username, claiming_user_id, claimed_at, event_key
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (guild_id, message_id, event_key) DO NOTHING
                """,
                guild.id,
                channel_id,
                message_id,
                character,
                claimant_name,
                claimant_id,
                claimed_at,
                event_key,
            )
        except Exception:
            self.bot.logger.exception(
                "Could not save Mudae recent claim guild=%s message=%s",
                guild.id,
                message_id,
            )
            return
        self._debug(
            "Recorded Mudae claim %s for %s in guild %s",
            character,
            claimant_name,
            guild.id,
        )

    async def _recent_claims_command(self, ctx: Context) -> None:
        """Show the latest Mudae claims for the current server."""

        if ctx.guild is None:
            raise commands.NoPrivateMessage(
                "Recent Mudae claims can only be viewed in a server."
            )
        rows = await self.bot.pool.fetch(
            """
            SELECT id, channel_id, character_name, claiming_username,
                   claiming_user_id, claimed_at
            FROM mudae_recent_claims
            WHERE guild_id = $1
            ORDER BY claimed_at DESC, id DESC
            """,
            ctx.guild.id,
        )
        claims: list[RecentClaim] = []
        for row in rows:
            try:
                claims.append(
                    RecentClaim(
                        id=int(self._row_value(row, "id")),
                        channel_id=int(self._row_value(row, "channel_id")),
                        character_name=str(self._row_value(row, "character_name", "")),
                        claiming_username=str(
                            self._row_value(row, "claiming_username", "Unknown")
                        ),
                        claiming_user_id=int(
                            self._row_value(row, "claiming_user_id", 1)
                        ),
                        claimed_at=self._row_value(
                            row, "claimed_at", discord.utils.utcnow()
                        ),
                    )
                )
            except (TypeError, ValueError):
                continue
        view = RecentClaimsView(ctx, claims)
        view.message = await ctx.send(
            view=view,
            ephemeral=getattr(ctx, "interaction", None) is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @classmethod
    def _cache_series_id(
        cls,
        wishes: _MudaeWishes,
        row: object,
        normalized_value: str,
        wish_type: str | None = None,
    ) -> None:
        raw_id = cls._row_value(row, "id")
        try:
            if raw_id is not None:
                numeric_id = int(raw_id)
                wishes.series_ids[numeric_id] = normalized_value
                wishes.series_id_types[numeric_id] = wish_type or "series"
        except (TypeError, ValueError):
            return

    async def _register_mudae_wish(
        self,
        ctx: Context,
        *,
        kind: str,
        value: str | int,
        kakera_threshold: int | None = None,
        source_metadata: dict[str, Any] | None = None,
        send_response: bool = True,
    ) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Mudae wishes can only be used in servers.")
        if kind == "character":
            text = str(value).strip()
            if not text:
                raise commands.BadArgument("Provide a character name to wish for.")
            stored_value = normalize_wish(text)
            target = f"character **{discord.utils.escape_markdown(text)}**"
        elif kind == "series":
            text = str(value).strip()
            if not text:
                raise commands.BadArgument("Provide a series name to wish for.")
            stored_value = normalize_wish(text)
            target = f"series **{discord.utils.escape_markdown(text)}**"
        elif kind == "series_kakera":
            text = str(value).strip()
            if not text:
                raise commands.BadArgument("Provide a series name to wish for.")
            if kakera_threshold is None:
                raise commands.BadArgument(
                    f"Provide a kakera threshold of at least {MIN_WISHKAKERA:,}."
                )
            try:
                threshold = int(kakera_threshold)
            except (TypeError, ValueError) as exc:
                raise commands.BadArgument(
                    "Kakera threshold must be a number."
                ) from exc
            if threshold < MIN_WISHKAKERA:
                raise commands.BadArgument(
                    f"Kakera threshold must be at least {MIN_WISHKAKERA:,}."
                )
            kakera_threshold = threshold
            stored_value = normalize_wish(text)
            target = (
                f"series **{discord.utils.escape_markdown(text)}** when it is worth "
                f"at least **{threshold:,} kakera**"
            )
        elif kind == "kakera":
            amount = int(value)
            if amount < MIN_WISHKAKERA:
                current = self._mudae_wishes.get((ctx.guild.id, ctx.author.id))
                if current is not None and current.kakera:
                    await self._clear_mudae_wishes(
                        ctx,
                        kind="kakera",
                        response="Cleared your kakera wishes.",
                    )
                else:
                    await ctx.send(
                        f"Kakera wishes must be at least {MIN_WISHKAKERA:,}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return
            stored_value = str(amount)
            target = f"rolls worth at least **{amount:,} kakera**"
        else:
            raise commands.BadArgument(f"Unsupported Mudae wish type: {kind}")

        # Persist first.  Updating the cache only after the insert succeeds
        # keeps notifications consistent if the database is temporarily
        # unavailable.  Keep the historical four-argument query for ordinary
        # wishes; a number of integrations rely on that stable shape.
        if kind == "series_kakera":
            metadata = source_metadata or {}
            await self.bot.pool.execute(
                """
                INSERT INTO mudae_wishes (
                    guild_id, user_id, wish_type, wish_value, kakera_threshold,
                    bundle_id, bundle_key, bundle_name, bundle_created_at,
                    source_guild_id, source_channel_id, source_message_id,
                    source_page, source_entry, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now())
                ON CONFLICT (guild_id, user_id, wish_type, wish_value)
                DO UPDATE SET kakera_threshold = EXCLUDED.kakera_threshold,
                              updated_at = now()
                """,
                ctx.guild.id,
                ctx.author.id,
                kind,
                stored_value,
                kakera_threshold,
                metadata.get("bundle_id"),
                metadata.get("bundle_key"),
                metadata.get("bundle_name"),
                metadata.get("bundle_created_at"),
                metadata.get("source_guild_id", ctx.guild.id),
                metadata.get("source_channel_id"),
                metadata.get("source_message_id"),
                metadata.get("source_page"),
                metadata.get("source_entry"),
            )
        else:
            await self.bot.pool.execute(
                """
                INSERT INTO mudae_wishes (guild_id, user_id, wish_type, wish_value)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, user_id, wish_type, wish_value) DO NOTHING
                """,
                ctx.guild.id,
                ctx.author.id,
                kind,
                stored_value,
            )

        # The row id is generated by PostgreSQL.  Fetch it when available so
        # users can remove a series by its stable id; lightweight test doubles
        # and pre-0085 databases simply leave the map empty.
        wish_id: int | None = None
        fetchval = getattr(self.bot.pool, "fetchval", None)
        if callable(fetchval) and kind in {"series", "series_kakera"}:
            try:
                wish_id = await cast(Any, fetchval)(
                    """
                    SELECT id FROM mudae_wishes
                    WHERE guild_id = $1 AND user_id = $2
                      AND wish_type = $3 AND wish_value = $4
                    """,
                    ctx.guild.id,
                    ctx.author.id,
                    kind,
                    stored_value,
                )
                if wish_id is not None:
                    wish_id = int(wish_id)
            except Exception:
                wish_id = None

        wishes = self._mudae_wishes.setdefault(
            (ctx.guild.id, ctx.author.id), _MudaeWishes()
        )
        if kind == "character":
            wishes.characters.add(stored_value)
        elif kind == "series":
            wishes.series.add(stored_value)
            if wish_id is not None:
                wishes.series_ids[wish_id] = stored_value
                wishes.series_id_types[wish_id] = kind
        elif kind == "series_kakera":
            # The database key is one row per (guild, user, type, series), so
            # changing a threshold replaces the prior value rather than
            # leaving stale in-memory thresholds that would still notify.
            wishes.series_kakera[stored_value] = {int(kakera_threshold or 0)}
            if wish_id is not None:
                wishes.series_ids[wish_id] = stored_value
                wishes.series_id_types[wish_id] = kind
        else:
            wishes.kakera.add(int(stored_value))

        if kind in {"series", "series_kakera"}:
            detail = (
                "-# *Run the **`$ima <series>`** command on Mudae and copy first "
                "title, that title is the one that shows up on embeds that we "
                "search for. Capitalization does not matter*"
            )
        elif kind == "character":
            detail = (
                "-# *Run the **`$im <character>`** command on Mudae and copy "
                "their full name, that name is the one that shows up on embeds "
                "that we search for. Capitalization does not matter*"
            )
        else:
            detail = (
                "-# Capitalization and extra spaces are ignored when matching the name."
            )
        if send_response:
            await ctx.send(
                f"I will notify you when Mudae shows a {target}.\n{detail}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _register_series_wishes(
        self,
        ctx: Context,
        value: str,
        *,
        kind: str = "series",
        kakera_threshold: int | None = None,
    ) -> None:
        """Register one or more ``$``-separated series wishes atomically-ish."""

        values = split_series_values(value)
        if not values:
            raise commands.BadArgument("Provide at least one series name to wish for.")
        for series in values:
            await self._register_mudae_wish(
                ctx,
                kind=kind,
                value=series,
                kakera_threshold=kakera_threshold,
                send_response=len(values) == 1,
            )
        if len(values) > 1:
            await ctx.send(
                f"I will notify you when Mudae shows {len(values)} wished series.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _saved_series_names(self) -> tuple[str, ...]:
        """Return the display names in the scraped series catalogue.

        The catalogue is optional (servers may not have run ``scrapeseries``
        yet), so an unavailable/older database must not prevent a user from
        wishing for a new series.  Keep this lookup best-effort and preserve
        the first spelling for each normalized name.
        """

        try:
            rows = await self.bot.pool.fetch("""
                SELECT series_name
                FROM mudae_series
                WHERE series_name IS NOT NULL
                ORDER BY id
                """)
        except Exception as exc:
            self._debug("Could not load saved Mudae series for wish matching: %s", exc)
            return ()

        names: list[str] = []
        seen: set[str] = set()
        for row in rows or ():
            value = self._row_value(row, "series_name")
            if not isinstance(value, str):
                continue
            value = " ".join(value.split()).strip()
            key = normalize_wish(value)
            if not key or key in seen:
                continue
            seen.add(key)
            names.append(value)
        return tuple(names)

    @staticmethod
    def _closest_saved_series(value: str, saved_names: tuple[str, ...]) -> str | None:
        """Return a likely saved-series correction for a mistyped value.

        Exact normalized matches are deliberately excluded: they should be
        added directly without an unnecessary confirmation.  The threshold
        is conservative enough to avoid proposing unrelated series while
        still catching common typos and missing punctuation.
        """

        normalized = normalize_wish(value)
        if not normalized:
            return None
        best: tuple[float, str] | None = None
        for candidate in saved_names:
            candidate_key = normalize_wish(candidate)
            if not candidate_key or candidate_key == normalized:
                continue
            score = SequenceMatcher(None, normalized, candidate_key).ratio()
            if best is None or score > best[0]:
                best = (score, candidate)
        if best is None:
            return None
        score, candidate = best
        # Short values need a stronger match because a single typo can still
        # produce a deceptively high ratio.  Longer titles benefit from the
        # lower bound to catch punctuation/spacing mistakes.
        minimum = 0.8 if len(normalized) < 12 else 0.72
        return candidate if score >= minimum else None

    async def _register_series_wishes_with_suggestions(
        self, ctx: Context, value: str
    ) -> None:
        """Register series wishes, confirming likely catalogue typos first."""

        values = split_series_values(value)
        if not values:
            raise commands.BadArgument("Provide at least one series name to wish for.")

        saved_names = await self._saved_series_names()
        prepared: list[str] = []
        for series in values:
            normalized = normalize_wish(series)
            # An exact saved name (including case/line-wrap differences) is
            # unambiguous.  Keep the user's spelling so the acknowledgement
            # remains familiar; matching itself is normalized.
            if any(normalize_wish(name) == normalized for name in saved_names):
                prepared.append(series)
                continue

            suggestion = self._closest_saved_series(series, saved_names)
            if suggestion is None:
                # New series are valid wishes even before a bundle scrape has
                # recorded them, so proceed without confirmation.
                prepared.append(series)
                continue

            prompt = (
                f"Did you mean **{discord.utils.escape_markdown(suggestion)}** "
                "for your series wish?"
            )
            prompt_kwargs: dict[str, Any] = {
                "confirm_label": "Yes",
                "cancel_label": "No",
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if getattr(ctx, "interaction", None) is not None:
                prompt_kwargs["ephemeral"] = True
            confirmed = await ctx.prompt(prompt, **prompt_kwargs)
            if confirmed is None:
                return
            prepared.append(suggestion)

        for series in prepared:
            await self._register_mudae_wish(
                ctx,
                kind="series",
                value=series,
                send_response=len(prepared) == 1,
            )
        if len(prepared) > 1:
            await ctx.send(
                f"I will notify you when Mudae shows {len(prepared)} wished series.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _wish_bundle_command(self, ctx: Context, bundle_name: str | None) -> None:
        """Offer to add every saved series in a scraped bundle to the user."""

        text = (bundle_name or "").strip()
        if not text:
            raise commands.BadArgument("Provide the name of a saved Mudae bundle.")
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Mudae wishes can only be used in servers.")
        guild_id = ctx.guild.id

        bundles, _total_series, _total_bundles, _highest_guilds = (
            await self._fetch_scraped_series_catalog()
        )
        normalized = normalize_wish(text)
        # Users often copy the visible ``(Bundle)`` marker from Mudae's
        # author line.  The catalogue stores the canonical name without that
        # presentation suffix, so accept either form.
        if normalized.endswith(" (bundle)"):
            normalized = normalized[: -len(" (bundle)")].rstrip()
        matches = [
            bundle for bundle in bundles if normalize_wish(bundle.name) == normalized
        ]
        if not matches:
            await ctx.send(
                "I could not find that saved Mudae bundle. We either do not have "
                "it saved yet, or your spelling may be wrong.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # A bundle name can be scraped in more than one server.  Prefer this
        # server's copy, then the copy with the most saved series.
        bundle = max(
            matches,
            key=lambda candidate: (
                int(candidate.guild_id == guild_id),
                len(candidate.entries),
            ),
        )
        if not bundle.entries:
            await ctx.send(
                f"No series are saved for **{discord.utils.escape_markdown(bundle.name)}** yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        prompt = (
            f"Add **{len(bundle.entries):,}** series from "
            f"**{discord.utils.escape_markdown(bundle.name)}** to your wishlist?"
        )
        prompt_kwargs: dict[str, Any] = {
            "confirm_label": "Yes",
            "cancel_label": "No",
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if getattr(ctx, "interaction", None) is not None:
            prompt_kwargs["ephemeral"] = True
        confirmed = await ctx.prompt(prompt, **prompt_kwargs)
        if confirmed is None:
            return

        wishes = self._wishes_for(ctx)
        existing = set(wishes.series)
        added = 0
        for entry in bundle.entries:
            normalized_entry = normalize_wish(entry.name)
            await self._register_mudae_wish(
                ctx,
                kind="series",
                value=entry.name,
                send_response=False,
            )
            if normalized_entry not in existing:
                existing.add(normalized_entry)
                added += 1
        await ctx.send(
            f"Added **{added:,}** series from **{discord.utils.escape_markdown(bundle.name)}** "
            "to your wishlist.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _wishes_for(self, ctx: Context) -> _MudaeWishes:
        """Return this member's cached wishes for the current server."""

        if ctx.guild is None:
            raise commands.NoPrivateMessage("Mudae wishes can only be used in servers.")
        return self._mudae_wishes.get((ctx.guild.id, ctx.author.id), _MudaeWishes())

    @staticmethod
    def _wish_list_text(wishes: _MudaeWishes, kind: str) -> str:
        if kind == "character":
            values = sorted(wishes.characters)
            heading = "Character wishes"
        elif kind == "series":
            values = sorted(wishes.series)
            heading = "Series wishes"
            # Keep IDs type-aware: a user may have both an ordinary series
            # wish and a threshold wish for the same normalized name.  Using
            # one name-to-ID map would otherwise display the threshold row's
            # ID beside the ordinary wish (and make the list misleading).
            ids_by_type: dict[str, dict[str, int]] = {
                "series": {},
                "series_kakera": {},
            }
            for wish_id, value in wishes.series_ids.items():
                wish_type = wishes.series_id_types.get(wish_id, "series")
                if wish_type in ids_by_type:
                    ids_by_type[wish_type].setdefault(value, wish_id)
            values = [
                (
                    f"`{ids_by_type['series'][value]}` · {value}"
                    if value in ids_by_type["series"]
                    else value
                )
                for value in values
            ]
            values.extend(
                (
                    f"`{ids_by_type['series_kakera'][series]}` · {series} · at least "
                    f"{threshold:,} kakera"
                    if series in ids_by_type["series_kakera"]
                    else f"{series} · at least {threshold:,} kakera"
                )
                for series, thresholds in sorted(wishes.series_kakera.items())
                for threshold in sorted(thresholds)
            )
        elif kind == "series_kakera":
            values = [
                (
                    f"`{wish_id}` · {series} · {threshold:,} kakera"
                    if (
                        wish_id := next(
                            (
                                wish_id
                                for wish_id, wish_value in wishes.series_ids.items()
                                if wish_value == series
                                and wishes.series_id_types.get(wish_id)
                                == "series_kakera"
                            ),
                            None,
                        )
                    )
                    is not None
                    else f"{series} · {threshold:,} kakera"
                )
                for series, thresholds in sorted(wishes.series_kakera.items())
                for threshold in sorted(thresholds)
            ]
            heading = "Series kakera wishes"
        else:
            values = [f"{value:,} kakera" for value in sorted(wishes.kakera)]
            heading = "Kakera wishes"
        if not values:
            return f"**{heading}:** None"
        return f"**{heading}:**\n" + "\n".join(f"- {value}" for value in values)

    async def _send_wish_example(self, ctx: Context, kind: str) -> None:
        """Explain character/series wishes with the supplied Mudae example."""

        title, command, filename = WISH_EXAMPLE_FILES[kind]
        if kind == "series":
            text = (
                f"Run the `{command}` command on Mudae and copy first title, "
                "that title is the one that shows up on embeds that we search "
                "for. Capitalization does not matter."
            )
        else:
            text = (
                f"Run the `{command}` command on Mudae and copy their full name, "
                "that name is the one that shows up on embeds that we search for. "
                "Capitalization does not matter."
            )
        path = FILES_ROOT / "images" / "mudae_examples" / filename
        if not path.is_file():
            raise commands.BadArgument("The Mudae example image is unavailable.")

        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.TextDisplay(text),
            discord.ui.Separator(),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(f"attachment://{filename}")
            ),
        ]
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                *children,
                accent_color=getattr(self.bot, "embedcolor", discord.Colour.blurple()),
            )
        )
        await ctx.send(
            view=view,
            file=discord.File(path, filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _series_wish_entries(
        self, ctx: Context, wishes: _MudaeWishes
    ) -> tuple[SeriesWishEntry, ...]:
        """Return series wishes with stable ids and catalogue match status.

        The in-memory wishes cache is still the source of truth for ordinary
        command/event matching.  The small read below only supplies the
        display ids and the spelling preserved by the database; a cache
        fallback keeps the list usable with older test fixtures or a transient
        database failure.
        """

        rows: list[object] = []
        pool = getattr(self.bot, "pool", None)
        fetch = getattr(pool, "fetch", None)
        if callable(fetch) and ctx.guild is not None:
            try:
                result = await cast(Any, fetch)(
                    """
                    SELECT id, wish_type, wish_value
                    FROM mudae_wishes
                    WHERE guild_id = $1 AND user_id = $2
                      AND wish_type IN ('series', 'series_kakera')
                    ORDER BY id
                    """,
                    ctx.guild.id,
                    ctx.author.id,
                )
                rows = list(result or ())
            except Exception as exc:
                self._debug("Could not load Mudae series wish rows: %s", exc)

        # ``series_name`` preserves the spelling shown by Mudae while
        # ``normalized_name`` is the comparison key used by wishes.
        saved_names: dict[str, str] = {}
        if callable(fetch):
            try:
                result = await cast(Any, fetch)(
                    "SELECT normalized_name, series_name FROM mudae_series"
                )
                for row in result or ():
                    normalized = self._row_value(row, "normalized_name")
                    name = self._row_value(row, "series_name")
                    if name is None:
                        continue
                    # Older catalogue rows may not have populated the
                    # normalized column.  Derive the same comparison key from
                    # the display name so those wishes are still recognized
                    # as exact matches in the list.
                    key = normalize_wish(str(normalized or name))
                    if key:
                        saved_names.setdefault(key, str(name))
            except Exception as exc:
                self._debug("Could not load saved Mudae series names: %s", exc)

        entries: list[SeriesWishEntry] = []
        if rows:
            for row in rows:
                raw_value = self._row_value(row, "wish_value")
                if raw_value is None:
                    continue
                value_key = normalize_wish(str(raw_value))
                canonical = saved_names.get(value_key)
                raw_id = self._row_value(row, "id")
                try:
                    wish_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    wish_id = None
                entries.append(
                    SeriesWishEntry(
                        id=wish_id,
                        name=canonical or str(raw_value),
                        exact=canonical is not None,
                    )
                )
        else:
            # Lightweight contexts may only populate the cache.  Prefer
            # stable ids there, and retain threshold rows as separate wishes.
            cached_values: list[tuple[int | None, str]] = []
            for wish_id, value in wishes.series_ids.items():
                wish_type = wishes.series_id_types.get(wish_id, "series")
                if wish_type in {"series", "series_kakera"}:
                    cached_values.append((wish_id, value))
            known_ids = {value for _wish_id, value in cached_values}
            cached_values.extend(
                (None, value)
                for value in sorted(wishes.series)
                if value not in known_ids
            )
            cached_values.extend(
                (None, value)
                for value in sorted(wishes.series_kakera)
                if value not in known_ids
            )
            for wish_id, value in sorted(
                cached_values, key=lambda item: (item[0] is None, item[0] or 0, item[1])
            ):
                canonical = saved_names.get(normalize_wish(value))
                entries.append(
                    SeriesWishEntry(
                        id=wish_id,
                        name=canonical or value,
                        exact=canonical is not None,
                    )
                )
        return tuple(entries)

    async def _send_wish_list(self, ctx: Context, kind: str) -> None:
        wishes = self._wishes_for(ctx)
        if kind == "series":
            entries = await self._series_wish_entries(ctx, wishes)
            view = SeriesWishListView(ctx, entries)
            await view.start(ephemeral=getattr(ctx, "interaction", None) is not None)
            return

        label = "Wish list" if kind == "character" else "Wishseries list"
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## {ctx.author.name}'s {label}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(self._wish_list_text(wishes, kind)),
                accent_color=getattr(self.bot, "embedcolor", discord.Colour.blurple()),
            )
        )
        await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _show_kakera_wish(self, ctx: Context) -> None:
        wishes = self._wishes_for(ctx)
        if not wishes.kakera:
            message = "You do not have a kakera wish set."
        else:
            values = ", ".join(f"{value:,}" for value in sorted(wishes.kakera))
            message = f"Your current kakera wish: **{values}**."
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    async def _clear_mudae_wishes(
        self,
        ctx: Context,
        *,
        kind: str,
        value: str | None = None,
        response: str,
    ) -> None:
        """Delete wishes from PostgreSQL and mirror the deletion in memory."""

        if ctx.guild is None:
            raise commands.NoPrivateMessage("Mudae wishes can only be used in servers.")
        # A series removal applies to both ordinary and kakera-threshold
        # series wishes. This keeps ``unwishseries`` and
        # ``clearwishseries`` intuitive; ``unwishkakera`` remains reserved for
        # numeric kakera wishes.
        series_kinds = kind == "series"
        kind_clause = (
            "wish_type IN ('series', 'series_kakera')"
            if series_kinds
            else "wish_type = $3"
        )
        if value is None:
            await self.bot.pool.execute(
                f"""
                DELETE FROM mudae_wishes
                WHERE guild_id = $1 AND user_id = $2 AND {kind_clause}
                """,
                *(  # Keep the old argument shape for non-series wishes.
                    (ctx.guild.id, ctx.author.id)
                    if series_kinds
                    else (ctx.guild.id, ctx.author.id, kind)
                ),
            )
        else:
            await self.bot.pool.execute(
                f"""
                DELETE FROM mudae_wishes
                  WHERE guild_id = $1 AND user_id = $2
                  AND {kind_clause} AND wish_value = ${3 if series_kinds else 4}
                """,
                *(  # ``series`` needs no third kind parameter in the query.
                    (ctx.guild.id, ctx.author.id, value)
                    if series_kinds
                    else (ctx.guild.id, ctx.author.id, kind, value)
                ),
            )

        key = (ctx.guild.id, ctx.author.id)
        wishes = self._mudae_wishes.get(key)
        if wishes is not None:
            if kind == "character":
                if value is None:
                    wishes.characters.clear()
                else:
                    wishes.characters.discard(value)
            elif kind == "series":
                if value is None:
                    wishes.series.clear()
                else:
                    wishes.series.discard(value)
                if value is None:
                    wishes.series_kakera.clear()
                    wishes.series_ids.clear()
                    wishes.series_id_types.clear()
                else:
                    wishes.series_kakera.pop(value, None)
                    for wish_id, wish_value in tuple(wishes.series_ids.items()):
                        if wish_value == value:
                            wishes.series_ids.pop(wish_id, None)
                            wishes.series_id_types.pop(wish_id, None)
            elif kind == "series_kakera":
                if value is None:
                    wishes.series_kakera.clear()
                    for wish_id in tuple(wishes.series_id_types):
                        if wishes.series_id_types.get(wish_id) == "series_kakera":
                            wishes.series_ids.pop(wish_id, None)
                            wishes.series_id_types.pop(wish_id, None)
                else:
                    wishes.series_kakera.pop(value, None)
                    for wish_id, wish_value in tuple(wishes.series_ids.items()):
                        if (
                            wish_value == value
                            and wishes.series_id_types.get(wish_id) == "series_kakera"
                        ):
                            wishes.series_ids.pop(wish_id, None)
                            wishes.series_id_types.pop(wish_id, None)
            elif kind == "kakera":
                if value is None:
                    wishes.kakera.clear()
                else:
                    wishes.kakera.discard(int(value))
            if (
                not wishes.characters
                and not wishes.series
                and not wishes.kakera
                and not wishes.series_kakera
            ):
                self._mudae_wishes.pop(key, None)

        await ctx.send(response, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _parse_series_kakera_arguments(
        arguments: str | None,
    ) -> tuple[str, int]:
        """Extract a threshold from a flexible series-kakera invocation."""

        import re

        text = (arguments or "").strip()
        if not text:
            raise commands.BadArgument(
                f"Provide one or more series and a kakera threshold ({MIN_WISHKAKERA:,}+)."
            )
        matches = list(re.finditer(r"(?<![\w])([\d][\d,]*)(?![\w])", text))
        threshold_match = next(
            (
                match
                for match in matches
                if int(match.group(1).replace(",", "")) >= MIN_WISHKAKERA
            ),
            None,
        )
        if threshold_match is None:
            raise commands.BadArgument(
                f"Provide a kakera threshold of at least {MIN_WISHKAKERA:,}."
            )
        threshold = int(threshold_match.group(1).replace(",", ""))
        series = (
            text[: threshold_match.start()] + " " + text[threshold_match.end() :]
        ).strip(" $")
        if not split_series_values(series):
            raise commands.BadArgument("Provide at least one series name to wish for.")
        return series, threshold

    async def _save_series_bundle(
        self, bundle: MudaeSeriesBundle, *, guild_id: int | None = None
    ) -> int:
        """Persist a scraped bundle and its series entries.

        The bundle row is shared across all users in a guild, while each
        series entry receives its own stable ID.  Upserts retain the original
        ``first_seen_at`` and refresh page/message metadata when Mudae edits
        or advances the response.
        """

        destination_guild = guild_id or bundle.guild_id
        if destination_guild is None:
            return 0
        pool = self.bot.pool
        bundle_row: Any = None
        fetchrow = getattr(pool, "fetchrow", None)
        if callable(fetchrow):
            try:
                bundle_row = await cast(Any, fetchrow)(
                    """
                    INSERT INTO mudae_series_bundles (
                        guild_id, source_guild_id, source_channel_id,
                        source_message_id, bundle_name, bundle_key,
                        latest_page, latest_total_pages, latest_message_id,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                    ON CONFLICT (guild_id, bundle_key) DO UPDATE SET
                        source_guild_id = COALESCE(EXCLUDED.source_guild_id,
                                                   mudae_series_bundles.source_guild_id),
                        source_channel_id = COALESCE(EXCLUDED.source_channel_id,
                                                    mudae_series_bundles.source_channel_id),
                        source_message_id = COALESCE(EXCLUDED.source_message_id,
                                                    mudae_series_bundles.source_message_id),
                        latest_page = EXCLUDED.latest_page,
                        latest_total_pages = EXCLUDED.latest_total_pages,
                        latest_message_id = EXCLUDED.latest_message_id,
                        updated_at = now()
                    RETURNING id
                    """,
                    destination_guild,
                    bundle.guild_id,
                    bundle.channel_id,
                    bundle.message_id,
                    bundle.name,
                    bundle.bundle_key,
                    bundle.page,
                    bundle.pages,
                    bundle.message_id,
                )
            except Exception as exc:
                self._debug("Could not persist Mudae series bundle: %s", exc)
        if bundle_row is None:
            try:
                await pool.execute(
                    """
                    INSERT INTO mudae_series_bundles (
                        guild_id, source_guild_id, source_channel_id,
                        source_message_id, bundle_name, bundle_key,
                        latest_page, latest_total_pages, latest_message_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (guild_id, bundle_key) DO UPDATE SET
                        latest_page = EXCLUDED.latest_page,
                        latest_total_pages = EXCLUDED.latest_total_pages,
                        latest_message_id = EXCLUDED.latest_message_id,
                        updated_at = now()
                    """,
                    destination_guild,
                    bundle.guild_id,
                    bundle.channel_id,
                    bundle.message_id,
                    bundle.name,
                    bundle.bundle_key,
                    bundle.page,
                    bundle.pages,
                    bundle.message_id,
                )
                fetchval = getattr(pool, "fetchval", None)
                if callable(fetchval):
                    bundle_row = await cast(Any, fetchval)(
                        """
                        SELECT id FROM mudae_series_bundles
                        WHERE guild_id = $1 AND bundle_key = $2
                        """,
                        destination_guild,
                        bundle.bundle_key,
                    )
            except Exception as exc:
                self._debug("Could not persist Mudae series bundle: %s", exc)
        bundle_id = (
            self._row_value(bundle_row, "id") if bundle_row is not None else None
        )
        if bundle_id is None and isinstance(bundle_row, (int, str)):
            bundle_id = bundle_row
        try:
            bundle_id = int(bundle_id) if bundle_id is not None else None
        except (TypeError, ValueError):
            bundle_id = None
        if bundle_id is None:
            # The insert may still have succeeded with a lightweight pool
            # double that cannot return rows.  Report entries as persisted
            # only when their parent ID is known.
            return 0

        saved = 0
        for position, entry in enumerate(bundle.entries, 1):
            try:
                # A scrape is append-only: an edited or repeated page must
                # not replace an existing series or inflate the "saved"
                # count.  ``RETURNING`` gives us an atomic inserted/not
                # inserted result even when two gateway events arrive close
                # together.
                fetchval = getattr(pool, "fetchval", None)
                if callable(fetchval):
                    inserted_id = await cast(Any, fetchval)(
                        """
                        INSERT INTO mudae_series (
                            bundle_id, series_name, normalized_name, character_count,
                            source_guild_id, source_channel_id, source_message_id,
                            source_page, source_entry, updated_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                        ON CONFLICT (bundle_id, normalized_name) DO NOTHING
                        RETURNING id
                        """,
                        bundle_id,
                        entry.name,
                        entry.normalized_name,
                        entry.character_count,
                        bundle.guild_id,
                        bundle.channel_id,
                        bundle.message_id,
                        getattr(entry, "page", None) or bundle.page,
                        getattr(entry, "position", None) or position,
                    )
                    if inserted_id is not None:
                        saved += 1
                    continue

                status = await pool.execute(
                    """
                    INSERT INTO mudae_series (
                        bundle_id, series_name, normalized_name, character_count,
                        source_guild_id, source_channel_id, source_message_id,
                        source_page, source_entry, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                    ON CONFLICT (bundle_id, normalized_name) DO NOTHING
                    """,
                    bundle_id,
                    entry.name,
                    entry.normalized_name,
                    entry.character_count,
                    bundle.guild_id,
                    bundle.channel_id,
                    bundle.message_id,
                    getattr(entry, "page", None) or bundle.page,
                    getattr(entry, "position", None) or position,
                )
                # asyncpg returns ``INSERT 0 1`` for a new row and
                # ``INSERT 0 0`` for a conflict.  Lightweight test doubles
                # may return None; treating that as inserted preserves their
                # historical behaviour while production remains atomic.
                if status is None or str(status).rsplit(" ", 1)[-1] == "1":
                    saved += 1
            except Exception as exc:
                self._debug("Could not persist Mudae series %r: %s", entry.name, exc)
        return saved

    @staticmethod
    def _series_scrape_status(bundle: MudaeSeriesBundle, saved: int) -> str:
        """Build the acknowledgement for a manual bundle scrape."""

        bundle_name = discord.utils.escape_markdown(bundle.name)
        page = (
            f" (page {bundle.page}/{bundle.pages})"
            if bundle.page and bundle.pages
            else ""
        )
        if saved:
            return f"Saved **{saved}** series from **{bundle_name}**{page}."
        return f"Everything on that page has already been saved from **{bundle_name}**{page}."

    async def _fetch_scraped_series_catalog(
        self,
    ) -> tuple[list[ScrapedSeriesBundle], int, int, list[tuple[str, int]]]:
        """Load the saved series catalogue and summary statistics."""

        try:
            summary = await self.bot.pool.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM mudae_series) AS total_series,
                    (SELECT COUNT(*) FROM mudae_series_bundles) AS total_bundles
                """)
            bundle_rows = await self.bot.pool.fetch("""
                WITH bundle_counts AS (
                    SELECT b.id, b.guild_id, b.bundle_name,
                           COUNT(s.id)::BIGINT AS series_count
                    FROM mudae_series_bundles AS b
                    LEFT JOIN mudae_series AS s ON s.bundle_id = b.id
                    GROUP BY b.id, b.guild_id, b.bundle_name
                )
                SELECT bc.id, bc.guild_id, bc.bundle_name, bc.series_count,
                       s.series_name, s.character_count, s.source_page,
                       s.source_entry, s.id AS series_id
                FROM bundle_counts AS bc
                LEFT JOIN mudae_series AS s ON s.bundle_id = bc.id
                ORDER BY bc.series_count DESC,
                         LOWER(bc.bundle_name),
                         s.source_page NULLS LAST,
                         s.source_entry NULLS LAST,
                         s.id NULLS LAST
                """)
            guild_rows = await self.bot.pool.fetch("""
                SELECT b.guild_id, COUNT(s.id)::BIGINT AS series_count
                FROM mudae_series_bundles AS b
                LEFT JOIN mudae_series AS s ON s.bundle_id = b.id
                GROUP BY b.guild_id
                ORDER BY series_count DESC, b.guild_id
                LIMIT 3
                """)
        except Exception as exc:
            self._debug("Could not load the saved Mudae series catalogue: %s", exc)
            raise commands.BadArgument(
                "The saved Mudae series catalogue is unavailable right now."
            ) from exc

        def integer(value: object, default: int = 0) -> int:
            try:
                return int(cast(Any, value)) if value is not None else default
            except (TypeError, ValueError, OverflowError):
                return default

        total_series = integer(self._row_value(summary, "total_series"))
        total_bundles = integer(self._row_value(summary, "total_bundles"))
        grouped: dict[int, dict[str, Any]] = {}
        for row in bundle_rows or ():
            bundle_id = integer(self._row_value(row, "id"), -1)
            if bundle_id < 0:
                continue
            bundle = grouped.setdefault(
                bundle_id,
                {
                    "name": str(self._row_value(row, "bundle_name", "Bundle")),
                    "guild_id": integer(self._row_value(row, "guild_id")),
                    "entries": [],
                },
            )
            series_name = self._row_value(row, "series_name")
            if series_name is None:
                continue
            count_value = self._row_value(row, "character_count")
            count = integer(count_value) if count_value is not None else None
            bundle["entries"].append(
                ScrapedSeriesEntry(name=str(series_name), character_count=count)
            )

        bundles = [
            ScrapedSeriesBundle(
                id=bundle_id,
                name=str(data["name"]),
                guild_id=int(data["guild_id"]),
                entries=tuple(data["entries"]),
            )
            for bundle_id, data in grouped.items()
        ]
        # The SQL ordering is the desired order, but explicitly sorting here
        # keeps the view deterministic for pools that do not preserve query
        # ordering in test fixtures.
        bundles.sort(
            key=lambda item: (-len(item.entries), item.name.casefold(), item.id)
        )

        highest_guilds: list[tuple[str, int]] = []
        for row in guild_rows or ():
            guild_id = integer(self._row_value(row, "guild_id"), 0)
            count = integer(self._row_value(row, "series_count"), 0)
            get_guild = getattr(self.bot, "get_guild", None)
            guild = get_guild(guild_id) if callable(get_guild) else None
            name = getattr(guild, "name", None) or str(guild_id)
            highest_guilds.append((str(name), count))
        return bundles, total_series, total_bundles, highest_guilds

    @staticmethod
    def _catalog_match(
        query: str,
        bundles: list[ScrapedSeriesBundle],
    ) -> tuple[int, str, bool] | None:
        """Find an exact or close bundle/series match.

        The boolean in the return value is true for a fuzzy match and is used
        to ask the user for confirmation before opening the suggested page.
        """

        normalized = normalize_wish(query)
        if not normalized:
            return None
        candidates: list[tuple[str, str, int]] = []
        for index, bundle in enumerate(bundles, 1):
            candidates.append((bundle.name, "bundle", index))
            candidates.extend((entry.name, "series", index) for entry in bundle.entries)
        for name, kind, page in candidates:
            if normalize_wish(name) == normalized:
                return page, name, False

        scored = [
            (
                SequenceMatcher(None, normalized, normalize_wish(name)).ratio(),
                name,
                kind,
                page,
            )
            for name, kind, page in candidates
        ]
        if not scored:
            return None
        score, name, _kind, page = max(scored, key=lambda item: item[0])
        candidate_key = normalize_wish(name)
        if score >= 0.58 or normalized in candidate_key or candidate_key in normalized:
            return page, name, True
        return None

    async def _series_list_command(
        self, ctx: Context, query: str | None = None
    ) -> None:
        bundles, total_series, total_bundles, highest_guilds = (
            await self._fetch_scraped_series_catalog()
        )

        initial_page = 0
        if query and query.strip():
            match = self._catalog_match(query, bundles)
            if match is None:
                await ctx.send(
                    "I could not find that saved bundle or series. We either do not "
                    "have it saved yet, or your spelling may be wrong.\n"
                    "-# *Note: aliases dont work*",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            initial_page, name, fuzzy = match
            if fuzzy:
                prompt = f"Did you mean **{discord.utils.escape_markdown(name)}**?"

                def create_view() -> SeriesListView:
                    return SeriesListView(
                        ctx,
                        bundles,
                        total_series=total_series,
                        total_bundles=total_bundles,
                        highest_guilds=highest_guilds,
                        initial_page=initial_page,
                    )

                confirmation = SeriesListConfirmationView(ctx, prompt, create_view)
                confirmation.message = await ctx.send(
                    view=confirmation,
                    ephemeral=getattr(ctx, "interaction", None) is not None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        view = SeriesListView(
            ctx,
            bundles,
            total_series=total_series,
            total_bundles=total_bundles,
            highest_guilds=highest_guilds,
            initial_page=initial_page,
        )
        await view.start(ephemeral=getattr(ctx, "interaction", None) is not None)

    async def _scrape_series_command(self, ctx: Context) -> None:
        guild = getattr(ctx, "guild", None)
        scraper = getattr(self, "_mudae_series_scraper", None)
        if guild is not None and scraper is not None and scraper.is_enabled(guild.id):
            await ctx.send(
                "Automatic Mudae series scraping is enabled for this server, "
                "so `scrapeseries` was not run.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        bundle = await scrape_series_from_context(ctx, limit=10)
        if bundle is None:
            raise commands.BadArgument(
                "I could not find a Mudae `$imab` bundle in your reply or the last 10 messages."
            )
        saved = await self._save_series_bundle(bundle)
        response = await ctx.send(
            self._series_scrape_status(bundle, saved),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if bundle.message_id is not None and response is not None:
            self._series_scrape_responses[int(bundle.message_id)] = response

    async def _update_series_scrape_response(
        self, bundle: MudaeSeriesBundle, saved: int
    ) -> None:
        """Refresh a manual scrape acknowledgement after a bundle edit."""

        if bundle.message_id is None:
            return
        response = self._series_scrape_responses.get(int(bundle.message_id))
        if response is None:
            return
        try:
            await response.edit(
                content=self._series_scrape_status(bundle, saved),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self._series_scrape_responses.pop(int(bundle.message_id), None)

    async def _toggle_auto_scrape_series(self, ctx: Context) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage(
                "This setting can only be changed in a server."
            )
        fetchval = getattr(self.bot.pool, "fetchval", None)
        current = None
        if callable(fetchval):
            try:
                current = await cast(Any, fetchval)(
                    "SELECT mudae_auto_scrape_series FROM guild_settings WHERE guild_id = $1",
                    ctx.guild.id,
                )
            except Exception:
                current = None
        enabled = not bool(current)
        await self.bot.pool.execute(
            """
            INSERT INTO guild_settings (guild_id, mudae_auto_scrape_series)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE
            SET mudae_auto_scrape_series = EXCLUDED.mudae_auto_scrape_series
            """,
            ctx.guild.id,
            enabled,
        )
        self._mudae_series_scraper.set_enabled(ctx.guild.id, enabled)
        state = "enabled" if enabled else "disabled"
        await ctx.send(
            f"Mudae automatic series scraping is now **{state}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _toggle_recent_claims(self, ctx: Context) -> None:
        """Toggle recent-claim tracking for the current server."""

        if ctx.guild is None:
            raise commands.NoPrivateMessage(
                "This setting can only be changed in a server."
            )
        guild_id = int(ctx.guild.id)
        enabled = not self._recent_claims_enabled_for(guild_id)
        await self.bot.pool.execute(
            """
            INSERT INTO guild_settings (guild_id, mudae_recent_claims)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE
            SET mudae_recent_claims = EXCLUDED.mudae_recent_claims
            """,
            guild_id,
            enabled,
        )
        self._recent_claims_enabled[guild_id] = enabled
        if not enabled:
            self._recent_claim_snapshots = {
                message_id: snapshot
                for message_id, snapshot in self._recent_claim_snapshots.items()
                if int(snapshot.get("guild_id") or 0) != guild_id
            }
        state = "enabled" if enabled else "disabled"
        await ctx.send(
            f"Mudae recent-claim tracking is now **{state}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _copy_wishes(self, ctx: Context, mode: str, source_value: str) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage(
                "Mudae wish copies can only be used in servers."
            )
        destination_id = ctx.guild.id
        visible_guilds = getattr(self.bot, "guilds", None)
        visible_ids = (
            [getattr(guild, "id", guild) for guild in visible_guilds]
            if visible_guilds
            else None
        )
        try:
            source_id = parse_source_guild_id(source_value)
            validate_copy_scope(
                source_id,
                destination_id,
                available_guild_ids=visible_ids,
            )
            canonical = parse_copy_mode(mode)
        except (InvalidSourceGuild, SameWishCopyGuild, ValueError) as exc:
            raise commands.BadArgument(str(exc)) from exc

        rows = await self.bot.pool.fetch(
            """
            SELECT id, guild_id, user_id, wish_type, wish_value,
                   kakera_threshold, created_at, bundle_created_at
            FROM mudae_wishes
            WHERE guild_id = $1 AND user_id = $2
            """,
            source_id,
            ctx.author.id,
        )
        prepared = prepare_wish_copy(
            rows,
            mode=canonical,
            source_guild_id=source_id,
            source_user_id=ctx.author.id,
        )
        destination_wishes = self._mudae_wishes.setdefault(
            (destination_id, ctx.author.id), _MudaeWishes()
        )
        fetchval = getattr(self.bot.pool, "fetchval", None)
        for row in prepared:
            values = row.insert_values(
                destination_guild_id=destination_id,
                user_id=ctx.author.id,
            )
            await self.bot.pool.execute(
                """
                INSERT INTO mudae_wishes (
                    guild_id, user_id, wish_type, wish_value, kakera_threshold,
                    source_guild_id, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6,
                        COALESCE($7, now()), now())
                ON CONFLICT (guild_id, user_id, wish_type, wish_value)
                DO UPDATE SET kakera_threshold = EXCLUDED.kakera_threshold,
                              updated_at = now()
                """,
                values["guild_id"],
                values["user_id"],
                values["wish_type"],
                values["wish_value"],
                values.get("kakera_threshold"),
                row.source_guild_id,
                row.source_created_at,
            )
            if row.wish_type in {"series", "series_kakera"} and callable(fetchval):
                try:
                    copied_id = await cast(Any, fetchval)(
                        """
                        SELECT id FROM mudae_wishes
                        WHERE guild_id = $1 AND user_id = $2
                          AND wish_type = $3 AND wish_value = $4
                        """,
                        destination_id,
                        ctx.author.id,
                        row.wish_type,
                        row.wish_value,
                    )
                    if copied_id is not None:
                        destination_wishes.series_ids[int(copied_id)] = row.wish_value
                        destination_wishes.series_id_types[int(copied_id)] = (
                            row.wish_type
                        )
                except Exception:
                    pass
        # Avoid a full reload for a copy: add the prepared rows to the same
        # cache the listener reads from.
        wishes = destination_wishes
        for row in prepared:
            if row.wish_type == "character":
                wishes.characters.add(row.wish_value)
            elif row.wish_type == "series":
                wishes.series.add(row.wish_value)
            elif row.wish_type == "series_kakera":
                wishes.series_kakera[row.wish_value] = {
                    int(row.kakera_threshold or MIN_WISHKAKERA)
                }
            elif row.wish_type == "kakera":
                wishes.kakera.add(int(row.wish_value))
        await ctx.send(
            f"Copied **{len(prepared)}** Mudae wish(es) from server `{source_id}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="wish")
    @commands.guild_only()
    async def wish(self, ctx: Context, *, character: str | None = None) -> None:
        """Notify you when Mudae rolls a matching character."""
        if not character or not character.strip():
            await self._send_wish_example(ctx, "character")
            return
        await self._register_mudae_wish(ctx, kind="character", value=character)

    @commands.command(name="wishseries")
    @commands.guild_only()
    async def wishseries(self, ctx: Context, *, series: str | None = None) -> None:
        """Notify you when Mudae rolls a matching series."""
        if not series or not series.strip():
            await self._send_wish_example(ctx, "series")
            return
        await self._register_series_wishes_with_suggestions(ctx, series)

    @commands.command(name="wishbundle")
    @commands.guild_only()
    async def wishbundle(self, ctx: Context, *, bundle: str | None = None) -> None:
        """Add every saved series from a Mudae bundle to your wishlist."""

        await self._wish_bundle_command(ctx, bundle)

    @commands.command(name="wishkakera")
    @commands.guild_only()
    async def wishkakera(self, ctx: Context, amount: int | None = None) -> None:
        """Notify you when Mudae rolls at least this much kakera."""
        if amount is None:
            await self._show_kakera_wish(ctx)
            return
        await self._register_mudae_wish(ctx, kind="kakera", value=amount)

    @commands.command(name="wishlist", aliases=("wl",))
    @commands.guild_only()
    async def wishlist(self, ctx: Context) -> None:
        """Show your character wishes for this server."""
        await self._send_wish_list(ctx, "character")

    @commands.command(name="wishserieslist", aliases=("wsl",))
    @commands.guild_only()
    async def wishserieslist(self, ctx: Context) -> None:
        """Show your series wishes for this server."""
        await self._send_wish_list(ctx, "series")

    @commands.command(name="scrapeseries")
    @commands.guild_only()
    async def scrapeseries(self, ctx: Context) -> None:
        """Scrape series names from a recent or replied-to Mudae ``$imab``."""
        await self._scrape_series_command(ctx)

    @commands.command(
        name="serieslist",
        aliases=("scraped", "scrapedseries"),
    )
    @commands.guild_only()
    async def serieslist(self, ctx: Context, *, query: str | None = None) -> None:
        """Browse series saved from Mudae bundles."""
        await self._series_list_command(ctx, query)

    @commands.command(
        name="wisherieska",
        aliases=("wishserieska",),
        extras={"usage": "<series> [series...] <kakera>"},
    )
    @commands.guild_only()
    async def wisherieska(self, ctx: Context, *, arguments: str | None = None) -> None:
        """Wish one or more series only when the roll reaches a kakera value."""
        series, threshold = self._parse_series_kakera_arguments(arguments)
        await self._register_series_wishes(
            ctx, series, kind="series_kakera", kakera_threshold=threshold
        )

    @commands.command(name="unwish")
    @commands.guild_only()
    async def unwish(self, ctx: Context, *, character: str) -> None:
        """Remove one character wish from this server."""
        await self._unwish_character(ctx, character)

    async def _unwish_character(self, ctx: Context, character: str) -> None:
        text = character.strip()
        if not text:
            raise commands.BadArgument("Provide a character name to unwish.")
        normalized = normalize_wish(text)
        await self._clear_mudae_wishes(
            ctx,
            kind="character",
            value=normalized,
            response=(
                f"Removed your character wish for "
                f"**{discord.utils.escape_markdown(text)}**."
            ),
        )

    @commands.command(name="clearwish")
    @commands.guild_only()
    async def clearwish(self, ctx: Context) -> None:
        """Remove all character wishes from this server."""
        await self._clear_mudae_wishes(
            ctx, kind="character", response="Cleared your character wishes."
        )

    @commands.command(name="unwishseries")
    @commands.guild_only()
    async def unwishseries(self, ctx: Context, *, series: str) -> None:
        """Remove one series wish from this server."""
        await self._unwish_series(ctx, series)

    async def _unwish_series(self, ctx: Context, series: str) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Mudae wishes can only be used in servers.")
        text = series.strip()
        if not text:
            raise commands.BadArgument("Provide a series name to unwish.")
        if text.isdecimal():
            wish_id = int(text)
            wishes = self._wishes_for(ctx)
            normalized = wishes.series_ids.get(wish_id)
            if normalized is not None:
                wish_type = wishes.series_id_types.get(wish_id, "series")
                await self.bot.pool.execute(
                    """
                    DELETE FROM mudae_wishes
                    WHERE guild_id = $1 AND user_id = $2 AND id = $3
                    """,
                    ctx.guild.id,
                    ctx.author.id,
                    wish_id,
                )
                if wish_type == "series_kakera":
                    wishes.series_kakera.pop(normalized, None)
                else:
                    wishes.series.discard(normalized)
                wishes.series_ids.pop(wish_id, None)
                wishes.series_id_types.pop(wish_id, None)
                await ctx.send(
                    f"Removed your series wish **`{wish_id}`**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        normalized = normalize_wish(text)
        await self._clear_mudae_wishes(
            ctx,
            kind="series",
            value=normalized,
            response=(
                f"Removed your series wish for "
                f"**{discord.utils.escape_markdown(text)}**."
            ),
        )

    @commands.command(name="clearwishseries")
    @commands.guild_only()
    async def clearwishseries(self, ctx: Context) -> None:
        """Remove all series wishes from this server."""
        await self._clear_mudae_wishes(
            ctx, kind="series", response="Cleared your series wishes."
        )

    @commands.command(name="unwishkakera")
    @commands.guild_only()
    async def unwishkakera(self, ctx: Context) -> None:
        """Remove all kakera wishes from this server."""
        await self._clear_mudae_wishes(
            ctx, kind="kakera", response="Cleared your kakera wishes."
        )

    @commands.command(
        name="recent-claimed",
        aliases=("recentclaims", "recentc", "rc"),
    )
    @commands.guild_only()
    async def recent_claimed(self, ctx: Context) -> None:
        """Show the most recently claimed Mudae characters in this server."""

        await self._recent_claims_command(ctx)

    @cast(Any, SphereCog.mudae).command(
        name="wish",
        description="Notify yourself when Mudae rolls a character.",
    )
    @commands.guild_only()
    @app_commands.describe(
        character="The character name as Mudae displays it; leave blank for an example."
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wish(
        self,
        ctx: Context,
        *,
        character: str | None = commands.param(
            default=None,
            description="Character name, or blank to view the example.",
        ),
    ) -> None:
        if not character or not character.strip():
            await self._send_wish_example(ctx, "character")
            return
        await self._register_mudae_wish(ctx, kind="character", value=character)

    @cast(Any, SphereCog.mudae).command(
        name="wishseries",
        description="Notify yourself when Mudae rolls a series.",
    )
    @commands.guild_only()
    @app_commands.describe(
        series="Series name(s), separated with $; leave blank for an example."
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wishseries(
        self,
        ctx: Context,
        *,
        series: str | None = commands.param(
            default=None,
            description="Series name(s) separated with $, or blank for an example.",
        ),
    ) -> None:
        if not series or not series.strip():
            await self._send_wish_example(ctx, "series")
            return
        await self._register_series_wishes_with_suggestions(ctx, series)

    @cast(Any, SphereCog.mudae).command(
        name="wishbundle",
        description="Add all saved series from a Mudae bundle to your wishlist.",
    )
    @commands.guild_only()
    @app_commands.describe(bundle="The saved Mudae bundle name.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wishbundle(
        self,
        ctx: Context,
        *,
        bundle: str = commands.param(description="Saved Mudae bundle name."),
    ) -> None:
        await self._wish_bundle_command(ctx, bundle)

    @cast(Any, SphereCog.mudae).command(
        name="wishkakera",
        description="Notify yourself when Mudae rolls at least this much kakera.",
    )
    @commands.guild_only()
    @app_commands.describe(
        amount="Minimum kakera value to watch for (67 or more); leave blank to view it."
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wishkakera(
        self,
        ctx: Context,
        amount: int | None = commands.param(
            default=None,
            description="Minimum kakera value, or blank to view the current value.",
        ),
    ) -> None:
        if amount is None:
            await self._show_kakera_wish(ctx)
            return
        await self._register_mudae_wish(ctx, kind="kakera", value=amount)

    @cast(Any, SphereCog.mudae).command(
        name="wishlist",
        aliases=("wl",),
        description="Show your character wishes for this server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wishlist(self, ctx: Context) -> None:
        await self._send_wish_list(ctx, "character")

    @cast(Any, SphereCog.mudae).command(
        name="wishserieslist",
        aliases=("wsl",),
        description="Show your series wishes for this server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wishserieslist(self, ctx: Context) -> None:
        await self._send_wish_list(ctx, "series")

    @cast(Any, SphereCog.mudae).command(
        name="unwish",
        description="Remove one character wish from this server.",
    )
    @commands.guild_only()
    @app_commands.describe(character="The character wish to remove.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_unwish(self, ctx: Context, *, character: str) -> None:
        await self._unwish_character(ctx, character)

    @cast(Any, SphereCog.mudae).command(
        name="clearwish",
        description="Remove all character wishes from this server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_clearwish(self, ctx: Context) -> None:
        await self._clear_mudae_wishes(
            ctx, kind="character", response="Cleared your character wishes."
        )

    @cast(Any, SphereCog.mudae).command(
        name="unwishseries",
        description="Remove one series wish from this server.",
    )
    @commands.guild_only()
    @app_commands.describe(series="The series wish to remove.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_unwishseries(self, ctx: Context, *, series: str) -> None:
        await self._unwish_series(ctx, series)

    @cast(Any, SphereCog.mudae).command(
        name="clearwishseries",
        description="Remove all series wishes from this server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_clearwishseries(self, ctx: Context) -> None:
        await self._clear_mudae_wishes(
            ctx, kind="series", response="Cleared your series wishes."
        )

    @cast(Any, SphereCog.mudae).command(
        name="unwishkakera",
        description="Remove all kakera wishes from this server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_unwishkakera(self, ctx: Context) -> None:
        await self._clear_mudae_wishes(
            ctx, kind="kakera", response="Cleared your kakera wishes."
        )

    @cast(Any, SphereCog.mudae).command(
        name="recent-claimed",
        aliases=("recentclaims", "recentc", "rc"),
        description="Show the most recently claimed Mudae characters in this server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_recent_claimed(self, ctx: Context) -> None:
        await self._recent_claims_command(ctx)

    @cast(Any, SphereCog.mudae).command(
        name="scrapeseries",
        description="Save series names from a Mudae $imab bundle.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_scrapeseries(self, ctx: Context) -> None:
        await self._scrape_series_command(ctx)

    @cast(Any, SphereCog.mudae).command(
        name="serieslist",
        aliases=("scraped", "scrapedseries"),
        description="Browse series saved from Mudae bundles.",
    )
    @commands.guild_only()
    @app_commands.describe(
        query="A saved bundle or series to open (leave blank for the summary)."
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_serieslist(
        self,
        ctx: Context,
        *,
        query: str | None = commands.param(
            default=None,
            description="A saved bundle or series, or blank for the summary.",
        ),
    ) -> None:
        await self._series_list_command(ctx, query)

    @cast(Any, SphereCog.mudae).command(
        name="wishserieska",
        aliases=("wisherieska",),
        description="Wish a series when its characters reach a kakera value.",
        extras={"usage": "<series> [series...] <kakera>"},
    )
    @commands.guild_only()
    @app_commands.describe(
        arguments="Series name(s) separated by $ and a minimum kakera value (67+)."
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_wishserieska(
        self,
        ctx: Context,
        *,
        arguments: str = commands.param(
            description="Series name(s) and minimum kakera value, in either order."
        ),
    ) -> None:
        series, threshold = self._parse_series_kakera_arguments(arguments)
        await self._register_series_wishes(
            ctx, series, kind="series_kakera", kakera_threshold=threshold
        )

    @cast(Any, SphereCog.mudae).group(
        name="toggle",
        description="Toggle Mudae server features.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_toggle(self, ctx: Context) -> None:
        """Configure optional Mudae server features."""
        await ctx.send(
            "Choose `auto-scrape-series` to toggle automatic `$imab` scraping, "
            "or `recent-claims` to toggle claim tracking.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @mudae_toggle.command(
        name="auto-scrape-series",
        description="Toggle automatic Mudae $imab series scraping.",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_toggle_auto_scrape_series(self, ctx: Context) -> None:
        await self._toggle_auto_scrape_series(ctx)

    @mudae_toggle.command(
        name="recent-claims",
        aliases=("recentclaimed", "recentclaims", "recentc", "rc"),
        description="Toggle recent Mudae character claim tracking.",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_toggle_recent_claims(self, ctx: Context) -> None:
        await self._toggle_recent_claims(ctx)

    @cast(Any, SphereCog.mudae).group(
        name="copy",
        description="Copy your Mudae wishes from another server.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_copy(self, ctx: Context) -> None:
        await ctx.send(
            "Choose `wishlist`, `serieswishlist`, `serieswishlistka`, or `all`, "
            "then provide a source server ID.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @mudae_copy.command(
        name="wishlist",
        description="Copy your character wishes from another server.",
    )
    @commands.guild_only()
    @app_commands.describe(serverid="The source server ID.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_copy_wishlist(self, ctx: Context, serverid: str) -> None:
        await self._copy_wishes(ctx, "wishlist", serverid)

    @mudae_copy.command(
        name="serieswishlist",
        description="Copy your series wishes from another server.",
    )
    @commands.guild_only()
    @app_commands.describe(serverid="The source server ID.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_copy_serieswishlist(self, ctx: Context, serverid: str) -> None:
        await self._copy_wishes(ctx, "serieswishlist", serverid)

    @mudae_copy.command(
        name="serieswishlistka",
        description="Copy your series kakera wishes from another server.",
    )
    @commands.guild_only()
    @app_commands.describe(serverid="The source server ID.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_copy_serieswishlistka(self, ctx: Context, serverid: str) -> None:
        await self._copy_wishes(ctx, "serieswishlistka", serverid)

    @mudae_copy.command(
        name="all",
        description="Copy all of your Mudae wishes from another server.",
    )
    @commands.guild_only()
    @app_commands.describe(serverid="The source server ID.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_copy_all(self, ctx: Context, serverid: str) -> None:
        await self._copy_wishes(ctx, "all", serverid)

    @staticmethod
    def _parse_mudae_roll(embed: discord.Embed) -> tuple[str, str, int | None] | None:
        parsed = parse_mudae_embed(embed)
        if parsed is None:
            return None
        return parsed.character, parsed.series or "", parsed.kakera

    async def _award_mudae_kakera_exchange(
        self, message: discord.Message, gift: MudaeKakeraGift
    ) -> bool:
        """Credit the giver for a validated Mudae kakera gift.

        Currency transactions already have a unique ``reference_key`` index,
        so the Mudae message ID is used as the idempotency boundary.  This is
        important when the gateway redelivers a message or both Fishie
        processes observe it during a rolling handoff: at most one wallet
        credit can be committed.  No response is sent to the channel; this
        exchange is intentionally silent, like imported Tatsu reputation
        rewards.
        """

        if gift.coins <= 0:
            # A one-kakera gift cannot produce a whole Coin.  Ignore it rather
            # than creating a zero-value ledger row (which the currency schema
            # intentionally disallows).
            return False

        raw_message_id = getattr(message, "id", None)
        if raw_message_id is None:
            self._debug(
                "Skipping Mudae kakera gift without a message ID: giver=%s",
                gift.giver_id,
            )
            return False
        try:
            message_id = int(raw_message_id)
        except (TypeError, ValueError, OverflowError):
            self._debug(
                "Skipping Mudae kakera gift without a valid message ID: giver=%s",
                gift.giver_id,
            )
            return False

        reference_key = f"mudae_kakera_exchange:{message_id}"
        pool = getattr(self.bot, "pool", None)
        fetchval = getattr(pool, "fetchval", None)
        if callable(fetchval):
            try:
                existing = await cast(Any, fetchval)(
                    """
                    SELECT 1
                    FROM currency_transactions
                    WHERE user_id = $1 AND reference_key = $2
                    LIMIT 1
                    """,
                    gift.giver_id,
                    reference_key,
                )
            except Exception as exc:
                # A transient read failure should not turn into a lost reward;
                # the credit transaction remains the authoritative check and
                # will still reject a duplicate reference atomically.
                self._debug(
                    "Could not check existing Mudae kakera exchange %s: %s",
                    message_id,
                    exc,
                )
                existing = None
            if existing is not None:
                return False

        currency = getattr(self.bot, "currency", None)
        credit = getattr(currency, "credit", None)
        if not callable(credit):
            self._debug(
                "Skipping Mudae kakera exchange %s: currency service unavailable",
                message_id,
            )
            return False
        try:
            await cast(Any, credit)(
                gift.giver_id,
                gift.coins,
                "mudae_kakera_exchange",
                reference_key=reference_key,
            )
        except asyncpg.UniqueViolationError:
            # Another event handler committed this exact gift first.
            return False
        except Exception:
            self.bot.logger.exception(
                "Failed to credit Mudae kakera exchange message=%s giver=%s "
                "kakera=%s coins=%s",
                message_id,
                gift.giver_id,
                gift.kakera,
                gift.coins,
            )
            return False
        self.bot.logger.info(
            "Credited Mudae kakera exchange message=%s giver=%s kakera=%s coins=%s",
            message_id,
            gift.giver_id,
            gift.kakera,
            gift.coins,
        )
        return True

    @commands.Cog.listener("on_message")
    async def _mudae_kakera_exchange_listener(self, message: discord.Message) -> None:
        """Exchange kakera gifted to Fishie in the support server.

        Only Mudae's exact gift message in the Fishie support guild is
        eligible.  Restricting both the author and recipient prevents ordinary
        messages that happen to contain ``kakera`` from minting Coins.
        """

        if is_legacy_instance(self.bot):
            return
        guild = getattr(message, "guild", None)
        if guild is None or getattr(guild, "id", None) != SUPPORT_GUILD_ID:
            return
        if getattr(getattr(message, "author", None), "id", None) != MudaeID:
            return
        gift = parse_mudae_kakera_gift(getattr(message, "content", None))
        if gift is None:
            return
        if gift.receiver_id != FISHIE_OWNER_ID or gift.giver_id == FISHIE_OWNER_ID:
            return
        await self._award_mudae_kakera_exchange(message, gift)

    @commands.Cog.listener("on_member_update")
    async def _support_booster_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Keep the supporter badge/reward in sync with boost changes."""

        if is_legacy_instance(self.bot):
            return
        guild = getattr(after, "guild", None) or getattr(before, "guild", None)
        if guild is None or getattr(guild, "id", None) != SUPPORT_GUILD_ID:
            return
        # A role update can be the only signal Discord sends for a boost
        # transition.  Compare the combined timestamp/managed-role status,
        # not just ``premium_since``.
        if self._member_is_support_booster(before) == self._member_is_support_booster(
            after
        ):
            return
        await self._sync_supporter_member(after)

    @commands.Cog.listener("on_message")
    async def _mudae_wish_listener(self, message: discord.Message) -> None:
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(message):
            return
        if message.guild is not None and getattr(message.author, "id", None) == MudaeID:
            self._cache_recent_claim_message(message)
        # ``$imab`` is opt-in per server.  Handle it before ordinary character
        # spawn parsing; the bundle parser has a strict ``(Bundle)`` guard so
        # information/profile embeds cannot be scraped accidentally.
        scraper = getattr(self, "_mudae_series_scraper", None)
        if (
            scraper is not None
            and message.guild is not None
            and scraper.is_enabled(message.guild.id)
        ):
            bundle = scraper.handle_message(message)
            if bundle is not None:
                await self._save_series_bundle(bundle)
        if message.author.id != MudaeID or message.guild is None:
            return
        if not message.embeds:
            return
        parsed: ParsedMudaeWish | None = parse_mudae_embed(message.embeds[0])
        if parsed is None:
            return
        character = parsed.character
        series = parsed.series or ""
        kakera = parsed.kakera
        normalized_character = normalize_wish(character)
        normalized_series = normalize_wish(series)

        for (guild_id, user_id), wishes in tuple(self._mudae_wishes.items()):
            if guild_id != message.guild.id:
                continue
            matches: list[str] = []
            if normalized_character in wishes.characters:
                matches.append("character")
            if normalized_series in wishes.series:
                matches.append("series")
            matching_series_kakera = wishes.series_kakera.get(normalized_series, set())
            if kakera is not None and any(
                threshold <= kakera for threshold in matching_series_kakera
            ):
                matches.append("series_kakera")
            if kakera is not None and any(value <= kakera for value in wishes.kakera):
                matches.append("kakera")
            if not matches:
                continue

            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except (discord.Forbidden, discord.HTTPException):
                    self.bot.logger.debug(
                        "Could not resolve Mudae wish user %s for guild %s",
                        user_id,
                        message.guild.id,
                    )
                    continue

            escaped_character = discord.utils.escape_markdown(character)
            escaped_series = discord.utils.escape_markdown(series)
            notification: list[str]
            if "series" in matches or "series_kakera" in matches:
                notification = [
                    f"Mudae character, **{escaped_character}** from series "
                    f"**{escaped_series}** spawned.",
                    f"Series: **{escaped_series}**",
                ]
                if kakera is not None:
                    notification.append(f"Kakera: **{kakera:,}**")
            elif "character" in matches:
                notification = [
                    f"Mudae character, **{escaped_character}** spawned.",
                ]
                if kakera is not None:
                    notification.append(f"Kakera: **{kakera:,}**")
            else:
                # A kakera-only wish still includes the character so the user
                # can identify the roll that crossed their threshold.
                notification = [
                    f"Mudae character with {kakera:,} kakera spawned.",
                    f"Character: **{escaped_character}**",
                ]
            notification.extend(("", f"[Jump to the Message]({message.jump_url})"))
            try:
                await user.send(
                    "\n".join(notification),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                self.bot.logger.debug(
                    "Could not DM Mudae wish notification to user %s",
                    user_id,
                )

    @commands.Cog.listener("on_message_edit")
    async def _mudae_recent_claim_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Record an unclaimed Mudae card when a member claims it."""

        if is_legacy_instance(self.bot):
            return
        guild = getattr(after, "guild", None) or getattr(before, "guild", None)
        if guild is None or is_operational_guild(guild):
            return
        before_author = getattr(getattr(before, "author", None), "id", None)
        after_author = getattr(getattr(after, "author", None), "id", None)
        if before_author != MudaeID and after_author != MudaeID:
            return
        if not self._recent_claims_enabled_for(int(guild.id)):
            return
        before_snapshot = self._recent_claim_snapshot(before)
        after_snapshot = self._recent_claim_snapshot(after)
        if before_snapshot is None or after_snapshot is None:
            return
        await self._record_recent_claim_edit(
            before_snapshot,
            after_snapshot,
            guild=guild,
            message=after,
        )
        self._recent_claim_snapshots[int(after.id)] = after_snapshot

    @commands.Cog.listener("on_raw_message_edit")
    async def _mudae_recent_claim_raw_edit(
        self, payload: discord.RawMessageUpdateEvent
    ) -> None:
        """Handle claim edits even when Discord did not cache the message."""

        if is_legacy_instance(self.bot):
            return
        guild_id = getattr(payload, "guild_id", None)
        if guild_id is None or is_operational_guild(guild_id):
            return
        try:
            guild_id = int(guild_id)
            message_id = int(payload.message_id)
        except (TypeError, ValueError, AttributeError):
            return
        if not self._recent_claims_enabled_for(guild_id):
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        data = getattr(payload, "data", None) or {}
        if not isinstance(data, dict):
            return
        raw_author = data.get("author")
        if isinstance(raw_author, dict) and raw_author.get("id") is not None:
            try:
                if int(raw_author["id"]) != MudaeID:
                    return
            except (TypeError, ValueError):
                return
        elif data.get("application_id") is not None:
            try:
                if int(data["application_id"]) != MudaeID:
                    return
            except (TypeError, ValueError):
                return
        cached_message = getattr(payload, "cached_message", None)
        before_snapshot = self._recent_claim_snapshots.get(message_id)
        if before_snapshot is None and cached_message is not None:
            before_snapshot = self._recent_claim_snapshot(cached_message)
        if before_snapshot is None:
            return
        # Discord usually includes a complete embed/reaction payload.  If a
        # gateway version sends only changed fields, retain the cached values
        # for the missing parts while still replacing the edited timestamp.
        after_snapshot = self._recent_claim_snapshot(
            data,
            fallback=before_snapshot,
        )
        if after_snapshot is None:
            return
        after_snapshot["id"] = message_id
        after_snapshot["guild_id"] = guild_id
        after_snapshot.setdefault("channel_id", getattr(payload, "channel_id", None))
        if claim_transition(before_snapshot, after_snapshot) is None:
            self._recent_claim_snapshots[message_id] = after_snapshot
            return
        after_message: discord.Message | None = None
        # A raw payload does not expose reaction users.  Fetch the updated
        # message only after the local transition check has found a likely
        # claim, so ordinary Mudae edits do not add an API request.
        after_embed = next(iter(after_snapshot.get("embeds") or ()), None)
        if after_embed is not None and claiming_username(after_embed) is None:
            channel_id = after_snapshot.get("channel_id")
            channel = self.bot.get_channel(channel_id) if channel_id else None
            fetch_message = getattr(channel, "fetch_message", None)
            if callable(fetch_message):
                try:
                    after_message = await cast(Any, fetch_message)(message_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    after_message = None
        await self._record_recent_claim_edit(
            before_snapshot,
            after_snapshot,
            guild=guild,
            message=after_message,
        )
        self._recent_claim_snapshots[message_id] = after_snapshot

    @commands.Cog.listener("on_raw_message_edit")
    async def _mudae_series_edit_listener(
        self, payload: discord.RawMessageUpdateEvent
    ) -> None:
        """Persist a newly visible page when Mudae edits an ``$imab`` message."""

        if is_legacy_instance(self.bot):
            return
        guild_id = getattr(payload, "guild_id", None)
        if is_operational_guild(guild_id):
            return
        scraper = getattr(self, "_mudae_series_scraper", None)
        raw_message_id = getattr(payload, "message_id", None)
        try:
            message_id = int(raw_message_id) if raw_message_id is not None else None
        except (TypeError, ValueError, OverflowError):
            message_id = None
        if (
            scraper is None
            or guild_id is None
            or (
                not scraper.is_enabled(guild_id)
                and (
                    message_id is None
                    or message_id not in self._series_scrape_responses
                )
            )
        ):
            return
        data = getattr(payload, "data", None) or {}
        embeds = data.get("embeds") if isinstance(data, dict) else None
        if not embeds:
            return
        # Prefer the gateway's updated/cached author when Discord includes it.
        # Only Mudae messages are eligible.  Raw MESSAGE_UPDATE payloads from
        # older Discord gateway versions may omit the author, so verify the
        # message through the cached channel (or one fetch) before using the
        # synthetic identity required by the strict parser below.
        verified_mudae = False
        updated_message = getattr(payload, "message", None) or getattr(
            payload, "cached_message", None
        )
        author = getattr(updated_message, "author", None)
        if author is not None:
            if getattr(author, "id", None) != MudaeID:
                return
            verified_mudae = True
        raw_author = data.get("author") if isinstance(data, dict) else None
        if isinstance(raw_author, dict) and raw_author.get("id") is not None:
            try:
                if int(raw_author["id"]) != MudaeID:
                    return
            except (TypeError, ValueError):
                return
            verified_mudae = True
        if not verified_mudae:
            channel = None
            get_channel = getattr(self.bot, "get_channel", None)
            channel_id = getattr(payload, "channel_id", None)
            if callable(get_channel) and channel_id is not None:
                channel = get_channel(channel_id)
            if channel is None:
                fetch_channel = getattr(self.bot, "fetch_channel", None)
                if callable(fetch_channel) and channel_id is not None:
                    try:
                        channel = await cast(Any, fetch_channel)(channel_id)
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        channel = None
            fetch_message = getattr(channel, "fetch_message", None)
            if not callable(fetch_message):
                return
            try:
                source_message = await cast(Any, fetch_message)(payload.message_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
            source_author = getattr(source_message, "author", None)
            if getattr(source_author, "id", None) != MudaeID:
                return
        # Provide Mudae's verified application id for the strict parser.  The
        # parser still requires a bundle author title and valid series entries
        # before anything is persisted.
        message_data = {
            "id": message_id,
            "guild_id": guild_id,
            "channel_id": (data.get("channel_id") if isinstance(data, dict) else None)
            or getattr(payload, "channel_id", None),
            "application_id": MudaeID,
            "author": {"id": MudaeID},
            "embeds": embeds,
        }
        if scraper.is_enabled(guild_id):
            bundle = scraper.handle_message(message_data)
        else:
            # Manual ``scrapeseries`` is allowed without enabling the optional
            # auto-scraper.  Its source message is tracked above, so edits to
            # that message still add new entries and refresh the acknowledgement.
            bundle = parse_mudae_bundle_message(message_data)
        if bundle is not None:
            saved = await self._save_series_bundle(bundle, guild_id=int(guild_id))
            await self._update_series_scrape_response(bundle, saved)


async def setup(bot: Fishie):
    await bot.add_cog(Mudae(bot))
