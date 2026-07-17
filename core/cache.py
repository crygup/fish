from typing import Any, Dict, List, Optional


class db_cache:
    prefixes: Dict[int, List[str]] = {}
    opted_out: Dict[int, List[str]] = {}
    auto_downloads: List[int] = []
    poketwo_guilds: List[int] = []
    auto_reaction_guilds: List[int] = []
    nsfw_covers: List[int] = []
    pinboard: Dict[int, int] = {}
    lastfm: dict[int, str] = {}
    disabled_commands: set[tuple[int, str, int]] = set()

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
        try:
            del self.pinboard[guild_id]
        except:
            return self.pinboard[guild_id]

    def add_opt_out(self, object_id: int, value: str):
        try:
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
        self.auto_downloads.append(channel_id)

    def remove_adl(self, channel_id: int):
        try:
            self.auto_downloads.remove(channel_id)
        except ValueError:
            pass

    def add_poketwo(self, guild_id: int):
        self.poketwo_guilds.append(guild_id)

    def remove_poketwo(self, guild_id: int):
        try:
            self.poketwo_guilds.remove(guild_id)
        except ValueError:
            pass

    def add_reaction_guilds(self, guild_id: int):
        self.auto_reaction_guilds.append(guild_id)

    def remove_reaction_guilds(self, guild_id: int):
        try:
            self.auto_reaction_guilds.remove(guild_id)
        except ValueError:
            pass

    def get_opted_out(self, object_id: int) -> List[str]:
        try:
            return self.opted_out[object_id]
        except:  # idc
            return []
