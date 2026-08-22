from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

REPUTATION_BONUS_USER_ID = 766953372309127168
REPUTATION_BONUS_GUILD_ID = 848507662437449750


class db_cache:
    def __init__(self) -> None:
        self.prefixes: Dict[int, List[str]] = {}
        self.opted_out: Dict[int, List[str]] = {}
        self.auto_downloads: set[int] = set()
        # Channels configured to route media through the review uploader.
        self.auto_uploads: set[int] = set()
        self.auto_upload_media: dict[int, set[str]] = {}
        # Hourly-post destinations are keyed by guild because a guild may
        # have its own destination and media filters.
        self.hourly_posts: Dict[int, int] = {}
        self.hourly_post_media: dict[int, set[str]] = {}
        self.hourly_post_intervals: dict[int, int] = {}
        self.hourly_post_next_at: dict[int, datetime] = {}
        self.hourly_post_blocks: dict[int, set[int]] = {}
        self.poketwo_guilds: set[int] = set()
        self.poketwo_channels: Dict[int, int] = {}
        self.auto_reaction_guilds: set[int] = set()
        # ``auto_reaction_targets`` is absent for guild-wide reaction rules.
        # When present, only the listed channel IDs are eligible.  Keep the
        # legacy single-channel mapping below for extensions that still read
        # it directly while new settings can manage any number of channels.
        self.auto_reaction_targets: dict[int, set[int]] = {}
        self.auto_reaction_channels: Dict[int, int] = {}
        self.nsfw_covers: set[int] = set()
        self.pinboard: Dict[int, int] = {}
        self.lastfm: dict[int, str] = {}
        self.anilist: dict[int, str] = {}
        # Owner-managed userinfo badges.  Keep the source fields available so
        # userinfo can render a custom emoji without reparsing display text.
        self.user_badges: dict[int, dict[str, Any]] = {}
        self.disabled_commands: set[tuple[int, str, int]] = set()
        # Owner-managed global command and user blocks.  These are loaded from
        # PostgreSQL at startup so the restrictions remain active after a
        # restart while still being cheap to check for every invocation.
        self.globally_disabled_commands: set[str] = set()
        self.globally_disabled_cogs: set[str] = set()
        self.globally_blocked_users: set[int] = set()
        self.tracking_disabled_users: set[int] = set()
        self.private_history_users: set[int] = set()
        # Discord bot accounts are always treated as having public history.
        # The IDs are populated whenever a user is resolved and from the
        # member cache at startup, so privacy checks remain synchronous.
        self.bot_users: set[int] = set()
        self.known_non_bot_users: set[int] = set()
        # History is private by default.  Keep an explicit allow-list for
        # users who chose to make their saved history public so an absent
        # database row cannot accidentally expose data.
        self.public_history_users: set[int] = set()
        self.game_tracking_disabled_users: set[int] = set()
        self.private_game_history_users: set[int] = set()
        self.public_game_history_users: set[int] = set()
        self.guild_tracking_disabled: set[int] = set()
        self.private_guild_history: set[int] = set()
        self.public_guild_history: set[int] = set()
        # Users must explicitly acknowledge the non-game history consent
        # prompt before their saved history is exposed.  This is separate
        # from the public-history setting so an absent row cannot be treated
        # as consent.
        self.tracking_consent_users: set[int] = set()
        self.reaction_tracking_users: set[int] = set()
        # Reputation XP bonuses are loaded once at startup and updated when a
        # new Fishie or Tatsu reputation event is recorded.  Keep source sets
        # per giver so Fishie and Tatsu bonuses can stack independently, as
        # can the user and guild bonuses.
        self.reputation_user_bonus_givers: dict[int, set[str]] = {}
        self.reputation_guild_bonus_givers: dict[int, set[str]] = {}
        self._reputation_user_period: date | None = None
        self._reputation_guild_period: date | None = None

    def add_lastfm(self, user_id: int, username: str) -> None:
        self.lastfm[user_id] = username

    def remove_lastfm(self, user_id: int) -> None:
        self.lastfm.pop(user_id, None)

    def add_anilist(self, user_id: int, username: str) -> None:
        self.anilist[user_id] = username

    def remove_anilist(self, user_id: int) -> None:
        self.anilist.pop(user_id, None)

    def update_accounts(
        self,
        user_id: int,
        *,
        last_fm: str | None,
        anilist: str | None,
    ) -> None:
        """Synchronize the cached account names for one user."""
        if last_fm:
            self.add_lastfm(user_id, str(last_fm))
        else:
            self.remove_lastfm(user_id)
        if anilist:
            self.add_anilist(user_id, str(anilist))
        else:
            self.remove_anilist(user_id)

    # Keep the old method name for extensions that still use it.
    def add_account(self, user_id: int, last_fm: str) -> None:
        self.add_lastfm(user_id, last_fm)

    def remove_account(self, user_id: int):
        self.remove_lastfm(user_id)

    def add_disabled_command(
        self, guild_id: int, command: str, channel_id: int
    ) -> None:
        self.disabled_commands.add((guild_id, command.casefold(), channel_id))

    def remove_disabled_command(
        self, guild_id: int, command: str, channel_id: int
    ) -> None:
        self.disabled_commands.discard((guild_id, command.casefold(), channel_id))

    def add_prefix(self, guild_id: int, prefix: str) -> List[str]:
        try:
            if prefix not in self.prefixes[guild_id]:
                self.prefixes[guild_id].append(prefix)
        except KeyError:
            self.prefixes.update({guild_id: [prefix]})

        return self.prefixes[guild_id]

    def remove_prefix(self, guild_id: int, prefix: str) -> List[str]:
        try:
            self.prefixes[guild_id].remove(prefix)
        except KeyError:
            return []
        except ValueError:
            return self.prefixes[guild_id]

        return self.prefixes[guild_id]

    def add_pinboard(self, guild_id: int, channel_id: int):
        self.pinboard.update({guild_id: channel_id})

    def remove_pinboard(self, guild_id: int, channel_id: int):
        return self.pinboard.pop(guild_id, None)

    def add_opt_out(self, object_id: int, value: str):
        try:
            if value not in self.opted_out[object_id]:
                self.opted_out[object_id].append(value)
        except KeyError:
            self.opted_out.update({object_id: [value]})

        return self.opted_out[object_id]

    def remove_opt_out(self, object_id: int, value: str):
        try:
            self.opted_out[object_id].remove(value)
        except KeyError:
            return []
        except ValueError:
            return self.opted_out[object_id]

        return self.opted_out[object_id]

    def add_adl(self, channel_id: int):
        self.auto_downloads.add(channel_id)

    def remove_adl(self, channel_id: int):
        try:
            self.auto_downloads.remove(channel_id)
        except KeyError:
            pass

    def add_auto_upload(
        self,
        channel_id: int,
        media_types: set[str] | None = None,
    ) -> None:
        """Cache an auto-upload destination and its enabled media types."""
        self.auto_uploads.add(channel_id)
        self.auto_upload_media[channel_id] = set(
            {"images", "gifs", "videos"} if media_types is None else media_types
        )

    def remove_auto_upload(self, channel_id: int) -> None:
        self.auto_uploads.discard(channel_id)
        self.auto_upload_media.pop(channel_id, None)

    def set_hourly_posts(
        self,
        guild_id: int,
        channel_id: int | None,
        media_types: set[str] | None = None,
        interval_minutes: int = 60,
        next_post_at: datetime | None = None,
    ) -> None:
        if channel_id is None:
            self.hourly_posts.pop(guild_id, None)
            self.hourly_post_media.pop(guild_id, None)
            self.hourly_post_intervals.pop(guild_id, None)
            self.hourly_post_next_at.pop(guild_id, None)
            return
        self.hourly_posts[guild_id] = channel_id
        self.hourly_post_media[guild_id] = set(
            {"images", "gifs", "videos"} if media_types is None else media_types
        )
        interval = max(10, int(interval_minutes))
        self.hourly_post_intervals[guild_id] = interval
        if next_post_at is None:
            next_post_at = datetime.now(timezone.utc)
        elif next_post_at.tzinfo is None:
            next_post_at = next_post_at.replace(tzinfo=timezone.utc)
        self.hourly_post_next_at[guild_id] = next_post_at

    def set_hourly_post_next_at(
        self, guild_id: int, next_post_at: datetime
    ) -> None:
        if guild_id not in self.hourly_posts:
            return
        if next_post_at.tzinfo is None:
            next_post_at = next_post_at.replace(tzinfo=timezone.utc)
        self.hourly_post_next_at[guild_id] = next_post_at

    def add_hourly_post_block(self, guild_id: int, user_id: int) -> None:
        self.hourly_post_blocks.setdefault(guild_id, set()).add(user_id)

    def remove_hourly_post_block(self, guild_id: int, user_id: int) -> None:
        blocked = self.hourly_post_blocks.get(guild_id)
        if blocked is None:
            return
        blocked.discard(user_id)
        if not blocked:
            self.hourly_post_blocks.pop(guild_id, None)

    def add_poketwo(self, guild_id: int):
        self.poketwo_guilds.add(guild_id)

    def remove_poketwo(self, guild_id: int):
        try:
            self.poketwo_guilds.remove(guild_id)
        except KeyError:
            pass

    def set_poketwo_channel(self, guild_id: int, channel_id: int | None) -> None:
        if channel_id is None:
            self.poketwo_channels.pop(guild_id, None)
        else:
            self.poketwo_channels[guild_id] = channel_id

    def add_reaction_guilds(self, guild_id: int):
        self.auto_reaction_guilds.add(guild_id)

    def remove_reaction_guilds(self, guild_id: int):
        try:
            self.auto_reaction_guilds.remove(guild_id)
        except KeyError:
            pass

    def set_auto_reaction_channels(
        self, guild_id: int, channel_ids: Iterable[int] | None
    ) -> None:
        """Set channel targets for automatic reactions.

        ``None`` (and an empty iterable) means that the enabled rule applies
        server-wide.  A non-empty iterable scopes the rule to those channels.
        The old one-channel cache is maintained for compatibility with older
        settings/API code.
        """
        if channel_ids is None:
            self.auto_reaction_targets.pop(guild_id, None)
            self.auto_reaction_channels.pop(guild_id, None)
            return
        targets = {int(channel_id) for channel_id in channel_ids}
        if not targets:
            self.auto_reaction_targets.pop(guild_id, None)
            self.auto_reaction_channels.pop(guild_id, None)
            return
        self.auto_reaction_targets[guild_id] = targets
        self.auto_reaction_channels[guild_id] = next(iter(targets))

    def add_auto_reaction_channel(self, guild_id: int, channel_id: int) -> None:
        """Add one channel to a scoped automatic-reaction rule."""
        targets = self.auto_reaction_targets.setdefault(guild_id, set())
        targets.add(int(channel_id))
        self.auto_reaction_channels[guild_id] = next(iter(targets))

    def remove_auto_reaction_channel(self, guild_id: int, channel_id: int) -> None:
        """Remove one channel target, falling back to server-wide when empty."""
        targets = self.auto_reaction_targets.get(guild_id)
        if targets is None:
            return
        targets.discard(int(channel_id))
        if not targets:
            self.set_auto_reaction_channels(guild_id, None)
        else:
            self.auto_reaction_channels[guild_id] = next(iter(targets))

    def auto_reaction_channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        """Return whether a channel is eligible for an enabled guild rule."""
        targets = self.auto_reaction_targets.get(guild_id)
        return targets is None or channel_id in targets

    def set_auto_reaction_channel(
        self, guild_id: int, channel_id: int | None
    ) -> None:
        # Compatibility wrapper for the pre-multi-channel setting.  A null
        # value means server-wide when the feature itself remains enabled.
        self.set_auto_reaction_channels(
            guild_id, None if channel_id is None else {channel_id}
        )

    def get_opted_out(self, object_id: int) -> List[str]:
        try:
            return self.opted_out[object_id]
        except KeyError:
            return []

    def user_tracking_opted_out(self, user_id: int, item: str) -> bool:
        return user_id in self.tracking_disabled_users or item in self.get_opted_out(
            user_id
        )

    def user_history_is_public(self, user_id: int) -> bool:
        return user_id in self.public_history_users or user_id in self.bot_users

    def history_visible_to(self, owner_id: int, viewer_id: int) -> bool:
        """Return whether ``viewer_id`` may see an owner's saved history.

        Owners can always view their own data.  Everyone else needs an
        explicit public setting.  Keeping this check in the cache makes the
        private-by-default policy easy to apply consistently to leaderboards.
        """
        return owner_id == viewer_id or self.user_history_is_public(owner_id)

    def set_history_public(self, user_id: int, public: bool) -> None:
        """Cache an explicit saved-history visibility choice.

        A missing choice remains private. Keeping this operation centralized
        prevents callers from only removing the private marker and thereby
        accidentally treating an unknown user as public.
        """
        if public:
            self.public_history_users.add(user_id)
            self.private_history_users.discard(user_id)
        else:
            self.public_history_users.discard(user_id)
            self.private_history_users.add(user_id)

    def tracking_consent_given(self, user_id: int) -> bool:
        return user_id in self.tracking_consent_users

    def set_tracking_consent(self, user_id: int, consented: bool = True) -> None:
        if consented:
            self.tracking_consent_users.add(user_id)
        else:
            self.tracking_consent_users.discard(user_id)

    def user_game_tracking_enabled(self, user_id: int) -> bool:
        """Return whether game results may be saved for a user."""
        return (
            user_id not in self.tracking_disabled_users
            and user_id not in self.game_tracking_disabled_users
        )

    def user_game_history_is_public(self, user_id: int) -> bool:
        return user_id in self.public_game_history_users or user_id in self.bot_users

    def game_history_visible_to(self, owner_id: int, viewer_id: int) -> bool:
        return (
            owner_id == viewer_id
            or owner_id in self.bot_users
            or self.user_game_history_is_public(owner_id)
        )

    def remember_user(self, user_id: int, *, is_bot: bool) -> None:
        """Cache whether a resolved Discord user is a bot account."""
        if is_bot:
            self.bot_users.add(user_id)
            self.known_non_bot_users.discard(user_id)
        else:
            self.bot_users.discard(user_id)
            self.known_non_bot_users.add(user_id)

    def set_game_history_public(self, user_id: int, public: bool) -> None:
        """Cache an explicit game-history visibility choice."""
        if public:
            self.public_game_history_users.add(user_id)
            self.private_game_history_users.discard(user_id)
        else:
            self.public_game_history_users.discard(user_id)
            self.private_game_history_users.add(user_id)

    def guild_tracking_enabled(self, guild_id: int) -> bool:
        return guild_id not in self.guild_tracking_disabled

    def guild_history_is_public(self, guild_id: int) -> bool:
        return guild_id in self.public_guild_history

    def set_guild_history_public(self, guild_id: int, public: bool) -> None:
        """Cache an explicit guild-history visibility choice."""
        if public:
            self.public_guild_history.add(guild_id)
            self.private_guild_history.discard(guild_id)
        else:
            self.public_guild_history.discard(guild_id)
            self.private_guild_history.add(guild_id)

    def enable_reaction_tracking(self, user_id: int) -> None:
        self.reaction_tracking_users.add(user_id)

    def disable_reaction_tracking(self, user_id: int) -> None:
        self.reaction_tracking_users.discard(user_id)

    def reaction_tracking_enabled(self, user_id: int) -> bool:
        return (
            user_id in self.reaction_tracking_users
            and user_id not in self.tracking_disabled_users
        )

    @staticmethod
    def _reputation_periods(now: datetime | None = None) -> tuple[date, date]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        day = current.date()
        week = day - timedelta(days=(current.weekday() + 1) % 7)
        return day, week

    def reset_reputation_bonus_cache(self, now: datetime | None = None) -> None:
        """Clear bonus givers and set the active UTC day/week buckets."""

        day, week = self._reputation_periods(now)
        self.reputation_user_bonus_givers.clear()
        self.reputation_guild_bonus_givers.clear()
        self._reputation_user_period = day
        self._reputation_guild_period = week

    def _prepare_reputation_bonus_cache(self, now: datetime | None = None) -> None:
        """Expire cached bonus entries when the UTC day or week changes."""

        day, week = self._reputation_periods(now)
        if self._reputation_user_period != day:
            self.reputation_user_bonus_givers.clear()
            self._reputation_user_period = day
        if self._reputation_guild_period != week:
            self.reputation_guild_bonus_givers.clear()
            self._reputation_guild_period = week

    def add_reputation_user_bonus(
        self,
        giver_id: int,
        source: str = "fishie",
        now: datetime | None = None,
    ) -> None:
        self._prepare_reputation_bonus_cache(now)
        self.reputation_user_bonus_givers.setdefault(giver_id, set()).add(source)

    def add_reputation_guild_bonus(
        self,
        giver_id: int,
        source: str = "fishie",
        now: datetime | None = None,
    ) -> None:
        self._prepare_reputation_bonus_cache(now)
        self.reputation_guild_bonus_givers.setdefault(giver_id, set()).add(source)

    def reputation_bonus_count(
        self, giver_id: int, now: datetime | None = None
    ) -> int:
        """Return the currently active user and guild reputation bonus count."""

        self._prepare_reputation_bonus_cache(now)
        return len(self.reputation_user_bonus_givers.get(giver_id, ())) + len(
            self.reputation_guild_bonus_givers.get(giver_id, ())
        )
