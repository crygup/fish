from __future__ import annotations

import asyncio
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
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from utils import MESSAGE_RE, Config, EmojiInputType, Emojis, update_pokemon

from .cache import db_cache
from .migrations import check_migrations

SILENT_COMMAND_USERS: dict[str, frozenset[int]] = {
    "crab": frozenset({662378595192274974}),
}

if TYPE_CHECKING:
    from extensions.context import Context
    from extensions.discord_ext import Discord as DiscordCog
    from extensions.events import Events
    from extensions.lastfm import Lastfm
    from extensions.logging import Logging
    from extensions.moderation import Moderation
    from extensions.mudae import Mudae
    from extensions.settings import Settings
    from extensions.tools import Tools

    # from extensions.fishing import Fishing
    from .cog import Cog

FCT = TypeVar("FCT", bound="Context")

APP_PARAMETER_DESCRIPTIONS = {
    "amount": "Amount to use for this command",
    "channel": "Discord channel to use",
    "channel_name": "Channel name to use",
    "command": "Command to use",
    "command_name": "Command name to look up",
    "disabled": "Whether this setting should be disabled",
    "event": "Event type to configure",
    "guild": "Discord server to use",
    "guild_id": "Discord server ID to look up",
    "hidden": "Whether hidden entries should be included",
    "id": "Record ID to use",
    "limit": "Maximum number of results",
    "member": "Server member to view",
    "query": "Search query",
    "server": "Discord server to use",
    "size": "Grid size such as 3x3",
    "time_period": "Time period to display",
    "user": "Discord user to view",
    "username": "Account username or profile URL",
}


def describe_missing_app_parameters(command: Any) -> None:
    """Give every app-command option a useful Discord description."""
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            describe_missing_app_parameters(child)
        return
    if not isinstance(command, app_commands.Command):
        return
    descriptions: dict[str, str] = {}
    for parameter in command.parameters:
        if parameter.description and parameter.description not in {
            "…",
            "No description provided",
        }:
            continue
        descriptions[parameter.name] = APP_PARAMETER_DESCRIPTIONS.get(
            parameter.name,
            f"{parameter.name.replace('_', ' ').capitalize()} for this command",
        )
    for name, description in descriptions.items():
        internal_parameter = command._params.get(name)
        if internal_parameter is not None:
            internal_parameter.description = app_commands.locale_str(description)


class FirstUseNoticeView(discord.ui.View):
    def __init__(self, ctx: commands.Context[Any]) -> None:
        super().__init__(timeout=180)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "These settings belong to the person who ran the command.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Open settings", style=discord.ButtonStyle.primary)
    async def open_settings(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = self.ctx.bot.get_cog("Settings")
        callback = getattr(cog, "send_privacy_settings_interaction", None)
        if callback is None:
            await interaction.response.send_message(
                "Settings are temporarily unavailable.",
                ephemeral=True,
            )
            return
        await callback(self.ctx, interaction)


def required_intents() -> discord.Intents:
    """Enable only gateway events used by Fishie's loaded extensions."""

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.moderation = True
    intents.emojis_and_stickers = True
    intents.webhooks = True
    intents.messages = True
    intents.reactions = True
    intents.typing = True
    intents.voice_states = True
    intents.message_content = True
    intents.presences = True
    return intents


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
    cached_honeypots: dict[int, int] = {}
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
        pool: asyncpg.Pool,
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
        extension_root = Path(__file__).resolve().parents[1] / "extensions"
        self._extensions = [
            module.name
            for module in pkgutil.iter_modules(
                [str(extension_root)], prefix="extensions."
            )
        ]
        self.spotify_key: Optional[str] = None
        self.cached_covers: Dict[str, Tuple[str, bool]] = {}
        self.cached_roblox_templates: dict[int, tuple[str, dict, datetime.datetime]] = (
            {}
        )
        self.cached_mudae_consent: set[int] = set()
        self.cached_honeypots: dict[int, int] = {}
        self.cached_banned_ips: set[str] = set()
        self.media_semaphore = asyncio.Semaphore(3)
        self._resources_closed = False
        self._oauth_refresh_tasks: set[asyncio.Task[Any]] = set()
        self._eventsub_tasks: set[asyncio.Task[Any]] = set()
        self._restart_message_checked = False
        self.testing: bool = testing
        self.error_logs = None
        self.dagpi_rl = commands.CooldownMapping.from_cooldown(
            60.0, 60.0, commands.BucketType.default
        )
        self.messages: TTLCache[str, discord.Message] = TTLCache[str, discord.Message](
            maxsize=1000, ttl=300.0
        )  # {repr(ctx): message(from ctx.send) }
        self.support_invite: str = "https://discord.gg/rM9u4MRFBE"
        self.lfm_api = "https://ws.audioscrobbler.com/2.0/"
        self.lastfm_api_key = str(self.config["keys"]["lastfm"])
        self.lastfm_response_cache: TTLCache[tuple[tuple[str, str], ...], Any] = (
            TTLCache[tuple[tuple[str, str], ...], Any](maxsize=512, ttl=30)
        )

        super().__init__(
            command_prefix=get_prefix,
            intents=required_intents(),
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
            root_name
            in {
                "enable",
                "disable",
                "invite",
                "about",
                "help",
                "settings",
                "tracking",
                "logging",
            }
            or qualified_name.startswith(("logging-delete", "tracking-delete"))
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

    async def invoke(self, ctx: commands.Context[Fishie]) -> None:
        if (
            ctx.command is not None
            and not ctx.author.bot
            and ctx.author.id not in self.db_cache.first_use_notice_users
        ):
            newly_marked = await self.pool.fetchval(
                """
                INSERT INTO user_settings (user_id, first_use_notice_shown)
                VALUES ($1, TRUE)
                ON CONFLICT (user_id) DO UPDATE
                SET first_use_notice_shown = TRUE
                WHERE user_settings.first_use_notice_shown = FALSE
                RETURNING TRUE
                """,
                ctx.author.id,
            )
            self.db_cache.first_use_notice_users.add(ctx.author.id)
            if newly_marked:
                await ctx.send(
                    "Before using Fishie for the first time, please review your "
                    "privacy settings. Tracking is enabled by default and your "
                    "saved history is public by default. You can change either "
                    "setting whenever you want. Changing a setting does not "
                    "delete data that is already saved.",
                    view=FirstUseNoticeView(ctx),
                    ephemeral=ctx.interaction is not None,
                )
                return

        await super().invoke(ctx)

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
        required = {
            "extensions.context",
            "extensions.events",
            "extensions.logging",
            "extensions.moderation",
            "extensions.settings",
            "extensions.tools",
        }
        failed_required: list[str] = []
        for ext in self._extensions:
            try:
                await self.load_extension(ext)
                self.logger.info(f"Loaded extension: {ext}")
            except Exception:
                self.logger.exception(f"Failed to load extension: {ext}")
                if ext in required:
                    failed_required.append(ext)
                continue
        if failed_required:
            raise RuntimeError(
                "Required extensions failed to load: " + ", ".join(failed_required)
            )

    async def unload_extensions(self):
        for ext in tuple(self.extensions):
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
        async with self.pool.acquire() as connection:
            await check_migrations(connection)

        self.activity = discord.CustomActivity(name="fish help")

        self.error_logs = discord.Webhook.from_url(
            self.config["webhooks"]["error_logs"], session=self.session
        )

        await self.load_extensions()
        for cog in self.cogs.values():
            for command in cog.get_app_commands():
                describe_missing_app_parameters(command)
        await self.populate_cache()
        await update_pokemon(self)
        self.logger.info(f"Added {len(self.pokemon):,} pokemon")

    async def on_ready(self):
        if not hasattr(self, "start_time"):
            self.start_time = discord.utils.utcnow()
            self.logger.info(f"Logged into {str(self.user)}")
        if not self._restart_message_checked:
            try:
                await self._complete_recent_restart()
            except asyncpg.PostgresError:
                self.logger.exception("Could not read the saved restart message")
            else:
                self._restart_message_checked = True

    async def _complete_recent_restart(self) -> None:
        record = await self.pool.fetchrow("""
            SELECT channel_id, message_id, requested_at
            FROM bot_restart_state
            WHERE singleton = TRUE
            """)
        if record is None:
            return

        now = discord.utils.utcnow()
        requested_at = record["requested_at"]
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=datetime.UTC)
        if now - requested_at > datetime.timedelta(minutes=15):
            await self.pool.execute(
                "DELETE FROM bot_restart_state WHERE singleton = TRUE"
            )
            return

        try:
            channel = self.get_channel(record["channel_id"])
            if channel is None:
                channel = await self.fetch_channel(record["channel_id"])
            if not isinstance(channel, Messageable):
                raise TypeError("Restart message channel is not messageable")
            message = await channel.fetch_message(record["message_id"])
            elapsed = max(0.0, (now - message.created_at).total_seconds())
            if elapsed < 60:
                duration = f"{elapsed:.1f} seconds"
            else:
                minutes, seconds = divmod(round(elapsed), 60)
                duration = f"{minutes}m {seconds}s"
            await message.edit(
                content=f"Fishie is back online. Restart took {duration}."
            )
        except (discord.DiscordException, TypeError):
            self.logger.exception("Could not update the saved restart message")
        finally:
            await self.pool.execute(
                "DELETE FROM bot_restart_state WHERE singleton = TRUE"
            )

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
        if self._resources_closed:
            await super().close()
            return
        self._resources_closed = True
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
        self.db_cache.tracking_disabled_users.clear()
        self.db_cache.private_history_users.clear()
        self.db_cache.first_use_notice_users.clear()
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

        user_privacy_settings = await self.pool.fetch("""
            SELECT
                user_id,
                tracking_enabled,
                history_public,
                first_use_notice_shown
            FROM user_settings
            """)
        for row in user_privacy_settings:
            user_id = row["user_id"]
            if not row["tracking_enabled"]:
                self.db_cache.tracking_disabled_users.add(user_id)
            if not row["history_public"]:
                self.db_cache.private_history_users.add(user_id)
            if row["first_use_notice_shown"]:
                self.db_cache.first_use_notice_users.add(user_id)

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
            "SELECT guild_id, channel_id FROM honeypot_channels"
        )
        for row in honeypot_rows:
            self.cached_honeypots[row["guild_id"]] = row["channel_id"]
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
