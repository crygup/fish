from typing import Dict, List


class db_cache:
    def __init__(self) -> None:
        self.prefixes: Dict[int, List[str]] = {}
        self.opted_out: Dict[int, List[str]] = {}
        self.auto_downloads: set[int] = set()
        self.poketwo_guilds: set[int] = set()
        self.auto_reaction_guilds: set[int] = set()
        self.nsfw_covers: set[int] = set()
        self.pinboard: Dict[int, int] = {}
        self.lastfm: dict[int, str] = {}
        self.anilist: dict[int, str] = {}
        self.disabled_commands: set[tuple[int, str, int]] = set()
        self.tracking_disabled_users: set[int] = set()
        self.private_history_users: set[int] = set()
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

    def add_poketwo(self, guild_id: int):
        self.poketwo_guilds.add(guild_id)

    def remove_poketwo(self, guild_id: int):
        try:
            self.poketwo_guilds.remove(guild_id)
        except KeyError:
            pass

    def add_reaction_guilds(self, guild_id: int):
        self.auto_reaction_guilds.add(guild_id)

    def remove_reaction_guilds(self, guild_id: int):
        try:
            self.auto_reaction_guilds.remove(guild_id)
        except KeyError:
            pass

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
        return user_id in self.public_history_users

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
        return user_id in self.public_game_history_users

    def game_history_visible_to(self, owner_id: int, viewer_id: int) -> bool:
        return owner_id == viewer_id or self.user_game_history_is_public(owner_id)

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
