from __future__ import annotations

import datetime
import json
import pkgutil
import re
import sys
import traceback
from io import StringIO
from logging import Logger
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import aiohttp
import asyncpg
import discord
from cachetools import TTLCache
from discord.abc import Messageable
from discord.ext import commands

from utils import MESSAGE_RE, Config, EmojiInputType, Emojis, update_pokemon, drpepper
from .cache import db_cache

SILENT_COMMAND_USERS: dict[str, frozenset[int]] = {
    "crab": frozenset({662378595192274974}),
}

if TYPE_CHECKING:
    from extensions.context import Context
    from extensions.discord_ext import Discord as DiscordCog
    from extensions.events import Events
    from extensions.logging import Logging
    from extensions.settings import Settings
    from extensions.tools import Tools
    from extensions.lastfm import Lastfm
    from extensions.moderation import Moderation
    from extensions.mudae import Mudae

    # from extensions.fishing import Fishing

    from .cog import Cog

FCT = TypeVar("FCT", bound="Context")


async def get_prefix(bot: Fishie, message: discord.Message) -> List[str]:
    default = (
        ["fish ", "fish", "\U0001f41f", "\U0001f41f "] if not bot.testing else [";"]
    )

    if message.guild is None:
        return commands.when_mentioned_or(*default)(bot, message)

    prefixes = bot.db_cache.prefixes.get(message.guild.id, [])

    packed = default + list(prefixes)

    comp = re.compile("^(" + "|".join(map(re.escape, packed)) + ").*", flags=re.I)
    match = comp.match(message.content)

    if match:
        return commands.when_mentioned_or(*[match.group(1)])(bot, message)

    return commands.when_mentioned_or(*packed)(bot, message)


class Fishie(commands.Bot):
    custom_emojis = Emojis()
    cached_covers: Dict[str, Tuple[str, bool]] = {}
    cached_roblox_templates: dict[int, tuple[str, dict, datetime.datetime]] = {}
    cached_mudae_consent: set[int] = set()
    cached_honeypots: set[int] = set()
    cached_banned_ips: set[str] = set()
    _steam_oauth_states: dict[str, dict[str, int | str]]
    _spotify_oauth_states: dict[str, dict[str, int | str]]
    _anilist_oauth_states: dict[str, dict[str, int | str]]
    pokemon: List[str]
    error_logs: discord.Webhook | None

    def __init__(
        self,
        config: Config,
        logger: Logger,
        pool: "asyncpg.Pool[asyncpg.Record]",
        session: aiohttp.ClientSession,
        testing: bool = False,
    ):
        self.config: Config = config
        self.db_cache = db_cache()
        self.logger: Logger = logger
        self.pool = pool
        self.session = session
        self.start_time: datetime.datetime
        self.context_cls: Type[commands.Context[Fishie]] = commands.Context
        self._extensions = [
            m.name for m in pkgutil.iter_modules(["./extensions"], prefix="extensions.")
        ]
        self.spotify_key: Optional[str] = None
        self.cached_covers: Dict[str, Tuple[str, bool]] = {}
        self.cached_roblox_templates: dict[int, tuple[str, dict, datetime.datetime]] = (
            {}
        )
        self.cached_mudae_consent: set[int] = set()
        self.cached_honeypots: set[int] = set()
        self.cached_banned_ips: set[str] = set()
        self.testing: bool = testing
        self.current_downloads: List[str] = []
        self.error_logs = None
        self.dagpi_rl = commands.CooldownMapping.from_cooldown(
            60.0, 60.0, commands.BucketType.default
        )
        self.messages: TTLCache[str, discord.Message] = TTLCache[str, discord.Message](
            maxsize=1000, ttl=300.0
        )  # {repr(ctx): message(from ctx.send) }
        self.support_invite: str = f"https://discord.gg/rM9u4MRFBE"
        self.lfm_api = "https://ws.audioscrobbler.com/2.0/"
        self.lastfm_api_key = str(self.config["keys"]["lastfm"])
        self.lastfm_response_cache: TTLCache[tuple[tuple[str, str], ...], Any] = (
            TTLCache[tuple[tuple[str, str], ...], Any](maxsize=512, ttl=30)
        )

        super().__init__(
            command_prefix=get_prefix,
            intents=discord.Intents.all(),
            strip_after_prefix=True,
        )
        self.add_check(self._check_command_disabled)

    @staticmethod
    def _command_disable_excluded(command: commands.Command[Any, Any, Any]) -> bool:
        qualified_name = command.qualified_name.casefold()
        root_name = qualified_name.split(" ", 1)[0]
        cog = command.cog
        module = getattr(cog.__class__, "__module__", "") if cog is not None else ""
        return (
            root_name in {"enable", "disable", "invite", "about", "help"}
            or qualified_name.startswith("logging-delete")
            or root_name == "jsk"
            or module.startswith("extensions.owner")
            or module.startswith("extensions.jishaku")
        )

    async def _check_command_disabled(self, ctx: commands.Context[Fishie]) -> bool:
        if getattr(ctx, "_skip_command_disable_check", False):
            return True
        if ctx.guild is None or ctx.command is None:
            return True
        if self._command_disable_excluded(ctx.command):
            return True

        qualified_name = ctx.command.qualified_name.casefold()
        parts = qualified_name.split()
        channel_id = getattr(ctx.channel, "id", 0)
        for index in range(len(parts), 0, -1):
            command_name = " ".join(parts[:index])
            if (
                ctx.guild.id,
                command_name,
                channel_id,
            ) in self.db_cache.disabled_commands or (
                ctx.guild.id,
                command_name,
                0,
            ) in self.db_cache.disabled_commands:
                raise commands.CheckFailure(
                    f"The `{command_name}` command is disabled in this channel."
                )
        return True

    # thanks leo
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if before.content != after.content:
            await self.process_commands(after)

    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        _repr_regex = f"<extensions\\.context bound to message \\({payload.channel_id}-{payload.message_id}-[0-9]+\\)>"
        pattern = re.compile(_repr_regex)
        messages = {r: m for r, m in self.messages.items() if pattern.fullmatch(r)}
        for _repr, message in messages.items():
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                del self.messages[_repr]
            except KeyError:
                pass

    _secrets: set[str] | None = None

    def _build_secrets(self) -> set[str]:
        """Return every leaf string value from the config, for redaction."""
        secrets: set[str] = set()

        def walk(obj: object) -> None:
            if isinstance(obj, str):
                if len(obj) > 3:
                    secrets.add(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(self.config)
        return secrets

    def redact(self, text: str) -> str:
        if self._secrets is None:
            self._secrets = self._build_secrets()
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def too_big(self, text: str) -> discord.File:
        s = StringIO()
        s.write(text)
        s.seek(0)
        file = discord.File(s, "large.txt")  # type: ignore
        return file

    async def log_error(self, error: Exception | commands.CommandError | BaseException):
        excinfo = "".join(
            traceback.format_exception(
                type(error), error, error.__traceback__, chain=False
            )
        )

        excinfo = self.redact(excinfo)

        formatted = f"```py\n{excinfo}\n```"
        if len(formatted) > 2000:
            files = [self.too_big(excinfo)]
            content = "File too large"
        else:
            files = []
            content = formatted

        if self.error_logs is None:
            self.logger.error("Error webhook is not initialized; cannot send report")
            return

        try:
            await self.error_logs.send(content=content, files=files)
        except Exception:
            self.logger.exception("Failed to send error report")

    async def on_error(self, event: str, *args: Any, **kwargs: Any) -> None:
        _, error, _ = sys.exc_info()
        if not error:
            raise

        self.logger.info(f"Event {event} errored")

        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )

        await self.log_error(error)

        return await super().on_error(event, *args, **kwargs)

    async def load_extensions(self):
        for ext in self._extensions:
            try:
                await self.load_extension(ext)
                self.logger.info(f"Loaded extension: {ext}")
            except Exception:
                self.logger.exception(f"Failed to load extension: {ext}")
                continue

    async def unload_extensions(self):
        for ext in self._extensions:
            try:
                await self.unload_extension(ext)
                self.logger.info(f"Unloaded extension: {ext}")
            except Exception:
                self.logger.exception(f"Failed to unload extension: {ext}")
                continue

    async def reload_extensions(self):
        for ext in self._extensions:
            try:
                await self.reload_extension(ext)
                self.logger.info(f"Reloaded extension: {ext}")
            except Exception:
                self.logger.exception(f"Failed to reload extension: {ext}")
                continue

    async def setup_hook(self) -> None:
        schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
        with schema_path.open(encoding="utf-8") as fp:
            await self.pool.execute(fp.read())

        self.activity = discord.CustomActivity(name="fish help")

        self.error_logs = discord.Webhook.from_url(
            self.config["webhooks"]["error_logs"], session=self.session
        )

        await self.load_extensions()
        await self.populate_cache()
        await update_pokemon(self)
        self.logger.info(f"Added {len(self.pokemon):,} pokemon")

    async def on_ready(self):
        if not hasattr(self, "start_time"):
            self.start_time = discord.utils.utcnow()
            self.logger.info(f"Logged into {str(self.user)}")

    async def fetch_message(
        self, *, message: Union[str, int], channel: Optional[Messageable] = None
    ) -> discord.Message:
        if isinstance(message, int):
            if channel is None:
                raise TypeError("Channel is required when providing message ID")

            return await channel.fetch_message(message)

        msg_match = MESSAGE_RE.match(message)

        if msg_match:
            _channel = self.get_channel(int(msg_match.group(2)))

            if _channel is None:
                raise commands.ChannelNotFound(msg_match.group(2))

            if not isinstance(_channel, Messageable):
                raise TypeError("Channel is not messageable")

            return await _channel.fetch_message(int(msg_match.group(3)))

        raise ValueError("Could not find channel with provided arguments, try again.")

    async def get_context(
        self,
        message: discord.Message | discord.Interaction[Fishie],
        *,
        cls: Optional[Type[FCT]] = None,
    ) -> Context | commands.Context[Fishie]:
        new_cls = cls or self.context_cls
        return await super().get_context(message, cls=new_cls)

    async def close(self) -> None:
        self.logger.info("Logging out")
        await self.unload_extensions()
        await self.close_sessions()
        await super().close()

    async def close_sessions(self):
        await self.pool.close()
        self.logger.info("Closed Postgres session")
        await self.session.close()
        self.logger.info("Closed aiohttp session")

    async def populate_cache(self):
        self.db_cache.prefixes.clear()
        self.db_cache.opted_out.clear()
        self.db_cache.auto_downloads.clear()
        self.db_cache.poketwo_guilds.clear()
        self.db_cache.auto_reaction_guilds.clear()
        self.db_cache.nsfw_covers.clear()
        self.db_cache.pinboard.clear()
        self.db_cache.lastfm.clear()
        self.db_cache.disabled_commands.clear()
        self.cached_roblox_templates.clear()
        self.cached_mudae_consent.clear()
        self.cached_honeypots.clear()
        self.cached_banned_ips.clear()

        prefixes = await self.pool.fetch("""SELECT * FROM guild_prefixes""")

        for record in prefixes:
            guild_id = record["guild_id"]
            prefix = record["prefix"]
            self.db_cache.add_prefix(guild_id, prefix)
            self.logger.info(f'Added prefix "{prefix}" to "{guild_id}"')

        disabled_commands = await self.pool.fetch(
            "SELECT guild_id, command, channel_id FROM command_disables"
        )
        for row in disabled_commands:
            self.db_cache.add_disabled_command(
                row["guild_id"], row["command"], row["channel_id"]
            )

        opted_out = await self.pool.fetch("SELECT * FROM opted_out")
        for row in opted_out:
            for item in row["items"]:
                user_id = row["user_id"]
                self.db_cache.add_opt_out(user_id, item)

                self.logger.info(f'Added "{item}" to opted out for user "{user_id}"')

        guild_opted_out = await self.pool.fetch("SELECT * FROM guild_opted_out")
        for row in guild_opted_out:
            for item in row["items"]:
                guild_id = row["guild_id"]
                self.db_cache.add_opt_out(guild_id, item)
                self.logger.info(f'Added "{item}" to opted out for guild "{guild_id}"')

        guild_settings = await self.pool.fetch("SELECT * FROM guild_settings")
        for row in guild_settings:
            guild_id = row["guild_id"]
            adl = row["auto_download"]
            poketwo = row["poketwo"]
            auto_reactions = row["auto_reactions"]
            pinboard = row["pinboard"]

            if adl:
                self.db_cache.add_adl(adl)
                self.logger.info(
                    f'Added auto download channel "{adl}" to guild "{guild_id}"'
                )

            if pinboard:
                self.db_cache.add_pinboard(guild_id, pinboard)
                self.logger.info(
                    f'Added Pinboard channel "{pinboard}" to guild "{guild_id}"'
                )

            if poketwo:
                self.db_cache.add_poketwo(guild_id)
                self.logger.info(f'Added auto poketwo solving to guild "{guild_id}"')

            if auto_reactions:
                self.db_cache.add_reaction_guilds(guild_id)
                self.logger.info(f'Added auto media reactions to guild "{guild_id}"')

        accounts = await self.pool.fetch("SELECT * FROM accounts")
        for row in accounts:
            last_fm: Optional[str] = row["lastfm"]
            user_id: int = row["user_id"]

            if last_fm:
                self.db_cache.add_account(user_id=user_id, last_fm=last_fm)
                self.logger.info(
                    f'Added last.fm account "{last_fm}" to user "{user_id}"'
                )

        roblox_templates = await self.pool.fetch(
            "SELECT asset_id, image_url, extra FROM roblox_templates"
        )
        for row in roblox_templates:
            extra = (
                json.loads(row["extra"])
                if isinstance(row["extra"], str)
                else (row["extra"] or {})
            )
            self.cached_roblox_templates[row["asset_id"]] = (
                row["image_url"],
                extra,
                datetime.datetime.now(datetime.timezone.utc),
            )
            self.logger.info(f'Cached Roblox template for asset {row["asset_id"]}')

        consent_rows = await self.pool.fetch(
            "SELECT user_id FROM mudae_dm_consent WHERE consented = TRUE"
        )
        for row in consent_rows:
            self.cached_mudae_consent.add(row["user_id"])
        self.logger.info(f"Cached {len(self.cached_mudae_consent)} Mudae DM consent(s)")

        honeypot_rows = await self.pool.fetch(
            "SELECT channel_id FROM honeypot_channels"
        )
        for row in honeypot_rows:
            self.cached_honeypots.add(row["channel_id"])
        self.logger.info(f"Cached {len(self.cached_honeypots)} honeypot channel(s)")

        banned_rows = await self.pool.fetch("SELECT ip FROM banned_ips")
        for row in banned_rows:
            self.cached_banned_ips.add(row["ip"])
        self.logger.info(f"Cached {len(self.cached_banned_ips)} banned IP(s)")

    async def add_reactions(
        self,
        message: discord.Message,
        reactions: List[EmojiInputType | discord.Reaction],
    ):
        for reaction in reactions:
            try:
                await message.add_reaction(reaction)
            except (discord.HTTPException, TypeError, ValueError) as exc:
                self.logger.debug(
                    "Failed to add reaction %r to message %s",
                    reaction,
                    message.id,
                    exc_info=exc,
                )

    @property
    def bot_permissions(self) -> discord.Permissions:
        perms = discord.Permissions()
        perms.send_messages = True
        perms.add_reactions = True
        perms.manage_emojis_and_stickers = True
        perms.embed_links = True
        perms.attach_files = True
        perms.external_emojis = True
        perms.external_stickers = True
        perms.read_message_history = True

        return perms

    def get_cog(self, name: str) -> Optional[Cog]:
        return super().get_cog(name)  # type: ignore

    @property
    def cogs(self) -> Mapping[str, Cog]:
        return super().cogs  # type: ignore

    @property
    def tools(self) -> Optional[Tools]:
        return self.get_cog("Tools")  # type: ignore

    @property
    def events(self) -> Optional[Events]:
        return self.get_cog("Events")  # type: ignore

    @property
    def logging(self) -> Optional[Logging]:
        return self.get_cog("Logging")  # type: ignore

    @property
    def settings(self) -> Optional[Settings]:
        return self.get_cog("Settings")  # type: ignore

    @property
    def discord(self) -> Optional[DiscordCog]:
        return self.get_cog("Discord")  # type: ignore

    @property
    def mudae(self) -> Optional[Mudae]:
        return self.get_cog("Mudae")  # type: ignore

    @property
    def moderation(self) -> Optional[Moderation]:
        return self.get_cog("Moderation")  # type: ignore

    @property
    def lastfm(self) -> Optional[Lastfm]:
        return self.get_cog("Lastfm")  # type: ignore

    # @property
    # def fishing(self) -> Optional[Fishing]:
    #     return self.get_cog("Fishing")  # type: ignore

    @property
    def embedcolor(self) -> int:
        return 0xFAA0C1

    async def lfm_get(self, data: Dict[Any, Any]):
        cacheable = data.get("method") != "user.getrecenttracks"
        cache_key = tuple(sorted((str(key), str(value)) for key, value in data.items()))
        if cacheable:
            try:
                return self.lastfm_response_cache[cache_key]
            except KeyError:
                pass

        params = dict(data)
        params.setdefault("api_key", self.lastfm_api_key)
        params.setdefault("format", "json")
        timeout = aiohttp.ClientTimeout(total=15)
        async with self.session.get(
            self.lfm_api, params=params, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
            if not isinstance(result, dict):
                raise ValueError("Last.fm returned an invalid response")
        if cacheable and isinstance(result, dict) and not result.get("error"):
            self.lastfm_response_cache[cache_key] = result
        return result
