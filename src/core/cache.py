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
        self.disabled_commands: set[tuple[int, str, int]] = set()
        self.tracking_disabled_users: set[int] = set()
        self.private_history_users: set[int] = set()
        self.first_use_notice_users: set[int] = set()

    def add_account(
        self, user_id: int, last_fm: str
    ):  # later change this and add more than just last_fm and be dynamic
        self.lastfm.update({user_id: last_fm})

    def remove_account(self, user_id: int):
        del self.lastfm[user_id]

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
        return user_id not in self.private_history_users
