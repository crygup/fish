from __future__ import annotations

import asyncio
import datetime
import json
import pkgutil
import random
import re
import sys
import traceback
from io import StringIO
from logging import Logger
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
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

from utils import (
    MESSAGE_RE,
    Config,
    EmojiInputType,
    Emojis,
    TrackingConsentRequired,
    update_pokemon,
)

from .cache import db_cache
from .migrations import check_migrations

SILENT_COMMAND_USERS: dict[str, frozenset[int]] = {
    "crab": frozenset({662378595192274974}),
}

# Leave room for the code-block wrapper and the report header. Discord applies
# a 4,000-character displayable-text limit to the Components V2 payload.
# The inline report also contains the source, author, invocation, and headings.
# Keep these values comfortably below Components V2's 4,000-character aggregate
# displayable-text limit. Longer diagnostics are sent in ``large.txt``.
ERROR_COMPONENT_CHUNK = 2_200
ERROR_INVOCATION_LIMIT = 700
_ERROR_URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s<>\"']+)\?[^\s<>\"']*")
_ERROR_AUTH_RE = re.compile(
    r"(?i)(\b(?:authorization|x-api-key|api[_-]?key|token|secret|password)\s*[:=]\s*(?:bearer\s+)?)\S+"
)


def _error_chunks(value: str, limit: int = ERROR_COMPONENT_CHUNK) -> list[str]:
    """Split error text into safe Components V2-sized chunks."""
    value = value or "No error text was provided."
    value = value.replace("```", "`\u200b``")
    return [value[index : index + limit] for index in range(0, len(value), limit)]


def _error_code_block(value: str) -> str:
    return f"```text\n{value.replace('```', '`\u200b``')}\n```"


def _redact_error_text(value: object, redact: Callable[[str], str]) -> str:
    """Remove configured secrets and common bearer data from diagnostics."""
    text = redact(str(value))
    text = _ERROR_URL_QUERY_RE.sub(r"\1?[REDACTED]", text)
    return _ERROR_AUTH_RE.sub(r"\1[REDACTED]", text)


def _error_inline(value: object, redact: Callable[[str], str]) -> str:
    text = _redact_error_text(value, redact)
    text = discord.utils.escape_markdown(text)
    text = discord.utils.escape_mentions(text)
    return text


def _error_user_name(user: object | None) -> str | None:
    if user is None:
        return None
    return str(
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or user
    )


def _interaction_invocation(
    interaction: discord.Interaction | None,
    command_name: str | None = None,
) -> str | None:
    if interaction is None:
        return None
    data = interaction.data
    if not isinstance(data, dict):
        return f"/{command_name}" if command_name else None

    def render_options(options: object) -> str:
        if not isinstance(options, list):
            return ""
        parts: list[str] = []
        for option in options:
            if not isinstance(option, dict) or not option.get("name"):
                continue
            name = str(option["name"])
            nested = render_options(option.get("options"))
            if nested:
                parts.append(f"{name} {nested}")
            elif "value" in option:
                parts.append(f"{name}={option['value']}")
            else:
                parts.append(name)
        return " ".join(parts)

    name = command_name or str(data.get("name") or "unknown")
    options = render_options(data.get("options"))
    invocation = f"/{name}" + (f" {options}" if options else "")
    custom_id = data.get("custom_id")
    if custom_id:
        invocation += f" [component: {custom_id}]"
    return invocation


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

ROTATING_STATUSES: tuple[str, ...] = (
    "fish help",
    "fish anime mob psycho",
    "fish manga mob psycho",
    "fish image dr pepper",
    "fish character emilia",
    "fish remind 67 minutes",
    "fish download",
    "fish video",
    "fish prefix",
    "fish fm",
    "fish anilist",
    "fish caption i love dr pepper",
    "fish click",
    "fish movie",
    "fish letterboxd",
    "fish twitch follow @pge4",
    "fish highlight",
    "fish cube",
    "fish globe",
    "fish chart",
    "fish phone",
    "fish badapple",
)
STATUS_ROTATION_INTERVAL = 60 * 30


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


# These commands read saved, non-game history that is private until the user
# acknowledges the tracking notice. Live lookups and public statistics such
# as activity, command/download/emoji stats, and corn are deliberately not
# included. Snipe and editsnipe have their own moderation/privacy controls.
TRACKING_CONSENT_COMMANDS = frozenset(
    {
        "avatars",
        "avatarhistory",
        "discrims",
        "icons",
        "joins",
        "names",
        "nicknames",
        "servertags",
        "servernames",
        "status",
        "statuscalendar",
        "statuscal",
        "statushistory",
        "statuses",
        "usernames",
        "uptime",
        "user",
    }
)

# ``stats`` is also the parent for a few history leaderboards. Keep those
# explicitly scoped rather than requiring consent for the public command and
# download/emoji statistics that use the same parent group.
TRACKING_CONSENT_EXACT_COMMANDS = frozenset(
    {
        "stats joins",
        "stats join",
        "tag stats",
    }
)

TRACKING_CONSENT_EXCLUDED_COMMANDS = (
    "stats click",
    "stats clicks",
    "stats clickstats",
    "stats tictactoe",
    "stats ttt",
    "stats connectfour",
    "stats connect4",
    "stats connect-four",
    "stats c4",
    "stats higher-or-lower",
    "stats hol",
    "stats higherorlower",
    "stats highorlow",
    "stats higherlower",
    "stats highlow",
    "stats heads-or-tails",
    "stats headsortails",
    "stats headortail",
    "stats coinflip",
    "stats cf",
    "stats reactions",
    # Reaction history has its own opt-in switch for the person giving a
    # reaction.  Do not conflate that with the general saved-history consent.
    "reactions",
)

TRACKING_CONSENT_EXACT_EXCLUSIONS = frozenset(
    {
        "user",
        "user info",
        "user avatar",
        "user avatar get",
        "user banner",
    }
)


def command_requires_tracking_consent(
    command: Any | None,
) -> bool:
    """Return whether *command* may expose saved non-game history.

    Extensions can opt in explicitly with ``extras={"tracking_consent":
    True}``.  The qualified-name fallback keeps older commands covered and
    means aliases resolve to the canonical command name before this check.
    """

    if command is None:
        return False
    current: Any = command
    while current is not None:
        extras = getattr(current, "extras", None)
        if isinstance(extras, dict) and extras.get("tracking_consent") is True:
            return True
        current = getattr(current, "parent", None)

    qualified = str(getattr(command, "qualified_name", "")).casefold()
    if qualified == "commandstats" or qualified.startswith("commandstats "):
        qualified = "stats" + qualified[len("commandstats") :]
    # ``user`` is the fallback for the current user-info lookup. Its history
    # subcommands are covered by the root below, but the fallback and current
    # avatar/banner views are not tracking-history commands.
    if qualified in TRACKING_CONSENT_EXACT_EXCLUSIONS:
        return False
    if any(
        qualified == excluded or qualified.startswith(f"{excluded} ")
        for excluded in TRACKING_CONSENT_EXCLUDED_COMMANDS
    ):
        return False
    if qualified in TRACKING_CONSENT_EXACT_COMMANDS:
        return True
    if qualified in {"stats", "commandstats"}:
        return False
    root = qualified.split(" ", 1)[0]
    if root not in TRACKING_CONSENT_COMMANDS:
        return False
    return True


class TrackingConsentView(discord.ui.LayoutView):
    """Consent prompt shown before a user opens saved activity history."""

    def __init__(
        self,
        ctx: commands.Context[Any] | None = None,
        *,
        bot: Fishie | None = None,
        user_id: int | None = None,
    ) -> None:
        super().__init__(timeout=180)
        if ctx is None and (bot is None or user_id is None):
            raise TypeError("ctx or both bot and user_id are required")
        self.ctx = ctx
        self.bot = ctx.bot if ctx is not None else bot
        self.user_id = ctx.author.id if ctx is not None else user_id
        self.message: discord.Message | None = None
        self.status = discord.ui.TextDisplay(
            "## Tracking consent\n"
            "Some Fishie commands show saved history such as previous avatars, "
            "usernames, status, and joins. Please confirm that you want this history to "
            "be available to others. Accepting makes your saved history public. "
            "You can make it private or turn tracking off later in `fish settings`."
        )
        self.accept = discord.ui.Button(
            label="I agree", style=discord.ButtonStyle.success
        )
        self.decline = discord.ui.Button(
            label="Not now", style=discord.ButtonStyle.secondary
        )
        self.accept.callback = self._accept
        self.decline.callback = self._decline
        self._render()

    def _render(self) -> None:
        self.clear_items()
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(self.accept, self.decline),
                accent_color=self.bot.embedcolor if self.bot is not None else None,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This consent prompt belongs to the person who ran the command.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        self.accept.disabled = True
        self.decline.disabled = True
        self._render()
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def _accept(self, interaction: discord.Interaction) -> None:
        assert self.bot is not None and self.user_id is not None
        user_id = self.user_id
        await self.bot.pool.execute(
            """
            INSERT INTO user_settings (user_id, tracking_consent, history_public)
            VALUES ($1, TRUE, TRUE)
            ON CONFLICT (user_id) DO UPDATE
            SET tracking_consent = TRUE,
                history_public = TRUE
            """,
            user_id,
        )
        self.bot.db_cache.set_tracking_consent(user_id)
        self.bot.db_cache.set_history_public(user_id, True)
        self.accept.disabled = True
        self.decline.disabled = True
        self.status.content = (
            "## Tracking consent saved\n"
            "Your saved history is now public. You can change this any time in "
            "`fish settings`."
        )
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if self.ctx is not None and self.ctx.interaction is None:
            # Continue the text command that opened the prompt after Discord
            # has acknowledged the button interaction.
            await self.bot.invoke(self.ctx)
        else:
            self.status.content = (
                "## Tracking consent saved\n"
                "Your saved history is now public. Run the command again to "
                "view it. You can change this any time in `fish settings`."
            )
            self._render()
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _decline(self, interaction: discord.Interaction) -> None:
        self.accept.disabled = True
        self.decline.disabled = True
        self.status.content = (
            "## Tracking consent not given\n"
            "The tracking command was not run. You can accept later by using "
            "the command again."
        )
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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
        self._status_rotation_task: asyncio.Task[None] | None = None
        self._status_rotation_index = 0
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
        self.add_check(self._check_tracking_consent)

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

    async def _check_tracking_consent(self, ctx: commands.Context[Fishie]) -> bool:
        """Gate hybrid app commands that expose saved non-game history.

        Text commands are intercepted in :meth:`invoke`, while hybrid app
        commands run through ``Command.prepare`` directly.  This global check
        covers that second path without changing individual command callbacks.
        """
        if (
            ctx.interaction is None
            or ctx.command is None
            or ctx.author.bot
            or not command_requires_tracking_consent(ctx.command)
            or self.db_cache.tracking_consent_given(ctx.author.id)
            or ctx.author.id in self.db_cache.tracking_disabled_users
        ):
            return True

        view = TrackingConsentView(ctx)
        view.message = await ctx.send(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        raise TrackingConsentRequired()

    async def _check_app_tracking_consent(
        self, interaction: discord.Interaction[Fishie]
    ) -> bool:
        """Gate app-command views of saved non-game history.

        Text commands are gated in :meth:`invoke`. App commands bypass that
        method, so their leaf commands receive this check after extensions
        load. The consent prompt is ephemeral and does not expose the
        original command until the user explicitly accepts it.
        """
        command = interaction.command
        if (
            command is None
            or interaction.user.bot
            or not command_requires_tracking_consent(command)
            or self.db_cache.tracking_consent_given(interaction.user.id)
            or interaction.user.id in self.db_cache.tracking_disabled_users
        ):
            return True

        view = TrackingConsentView(bot=self, user_id=interaction.user.id)
        await interaction.response.send_message(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None
        # Pure app commands use the app-command check failure path. Hybrid
        # commands are stopped by ``_check_tracking_consent`` before this
        # leaf check is reached.
        return False

    def _register_tracking_consent_checks(self) -> None:
        """Attach the consent check to loaded app-command leaves."""

        def visit(command: Any) -> None:
            if isinstance(command, app_commands.Group):
                for child in command.commands:
                    visit(child)
                return
            # Hybrid app commands already pass through the global Context
            # check, which raises the ignored internal stop after responding.
            # Registering a second app check would turn that into a visible
            # ``HybridCommandError``.
            if getattr(command, "wrapped", None) is not None:
                return
            if not command_requires_tracking_consent(command):
                return
            if getattr(command, "_fishie_tracking_check", False):
                return
            command.add_check(self._check_app_tracking_consent)
            setattr(command, "_fishie_tracking_check", True)

        for cog in self.cogs.values():
            for command in cog.get_app_commands():
                visit(command)

    async def invoke(self, ctx: commands.Context[Fishie]) -> None:
        if (
            ctx.command is not None
            and not ctx.author.bot
            and command_requires_tracking_consent(ctx.command)
            and not self.db_cache.tracking_consent_given(ctx.author.id)
            and ctx.author.id not in self.db_cache.tracking_disabled_users
        ):
            view = TrackingConsentView(ctx)
            view.message = await ctx.send(
                view=view,
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
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

    def _error_invocation_details(
        self,
        *,
        context: Any | None = None,
        interaction: discord.Interaction | None = None,
        event: str | None = None,
        event_args: tuple[Any, ...] = (),
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        """Return source, author, subject, ID, and invocation text for an error."""
        interaction = interaction or getattr(context, "interaction", None)
        command = getattr(getattr(context, "command", None), "qualified_name", None)
        message = getattr(context, "message", None)
        author = getattr(context, "author", None)
        subject: object | None = None
        invocation: str | None = None

        if message is not None and getattr(message, "content", None):
            invocation = str(message.content)

        if interaction is not None:
            author = author or getattr(interaction, "user", None)
            app_invocation = _interaction_invocation(interaction, command)
            if invocation is None:
                invocation = app_invocation
            source = "Component interaction" if context is not None else "Interaction"
            if command:
                source += f" ({command})"
        elif context is not None:
            source = "Text command"
            if command:
                source += f" ({command})"
        elif event:
            source = f"Event: {event}"
        else:
            source = "Background task"

        for argument in event_args:
            if isinstance(argument, discord.Message):
                author = author or argument.author
                if invocation is None:
                    invocation = argument.content or None
                continue
            if isinstance(argument, discord.Interaction):
                author = author or argument.user
                if invocation is None:
                    invocation = _interaction_invocation(argument)
                continue
            if isinstance(argument, (discord.Member, discord.User)):
                subject = subject or argument

        if event and not source.startswith("Event:"):
            source = f"Event: {event}"

        author_name = _error_user_name(author)
        subject_name = _error_user_name(subject)
        author_id = str(getattr(author, "id", "")) if author is not None else None
        return source, author_name, subject_name, author_id, invocation

    async def log_error(
        self,
        error: Exception | commands.CommandError | BaseException,
        *,
        context: Any | None = None,
        interaction: discord.Interaction | None = None,
        event: str | None = None,
        event_args: tuple[Any, ...] = (),
    ) -> None:
        excinfo = "".join(
            traceback.format_exception(
                type(error), error, error.__traceback__, chain=True
            )
        )

        excinfo = _redact_error_text(excinfo, self.redact)
        source, author_name, subject_name, author_id, invocation = (
            self._error_invocation_details(
                context=context,
                interaction=interaction,
                event=event,
                event_args=event_args,
            )
        )
        source = _error_inline(source, self.redact)
        author_text = (
            f"{_error_inline(author_name, self.redact)} (`{author_id}`)"
            if author_name and author_id
            else "Unavailable"
        )
        subject_text = (
            _error_inline(subject_name, self.redact) if subject_name else None
        )
        invocation = discord.utils.escape_mentions(
            _redact_error_text(invocation or "Unavailable", self.redact)
        )
        if len(invocation) > ERROR_INVOCATION_LIMIT:
            invocation = invocation[: ERROR_INVOCATION_LIMIT - 1].rstrip() + "…"

        chunks = _error_chunks(excinfo)
        # Keep the inline report comfortably below Discord's aggregate text
        # limit. The complete traceback is attached whenever it does not fit.
        visible_chunk_count = 1
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Fishie error report"),
            discord.ui.TextDisplay(
                "\n".join(
                    [
                        f"**Source:** {source}",
                        f"**Author:** {author_text}",
                        *([f"**Subject:** {subject_text}"] if subject_text else []),
                    ]
                )
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### Invocation\n{_error_code_block(invocation)}"),
            discord.ui.Separator(),
        ]
        # The useful FFmpeg line and the subprocess note are at the end of a
        # chained traceback. Show that tail inline and send the complete report
        # as a separate webhook attachment below. Sending the file separately
        # avoids Discord treating it as part of an invalid Components V2 body.
        visible_chunks = chunks[:visible_chunk_count]
        if len(chunks) > visible_chunk_count:
            visible_chunks = [chunks[-1]]
        for index, chunk in enumerate(visible_chunks):
            heading = "### Full error\n" if index == 0 else ""
            children.append(discord.ui.TextDisplay(heading + _error_code_block(chunk)))
        include_traceback_file = len(chunks) > visible_chunk_count
        if include_traceback_file:
            children.append(
                discord.ui.TextDisplay(
                    "-# The complete traceback is attached as `large.txt`."
                )
            )

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(*children, accent_color=discord.Color.red().value)
        )

        if self.error_logs is None:
            self.logger.error("Error webhook is not initialized; cannot send report")
            return

        try:
            await self.error_logs.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if include_traceback_file:
                await self.error_logs.send(
                    content="Full Fishie traceback attached as `large.txt`.",
                    file=self.too_big(excinfo),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            # A malformed/oversized Components V2 payload must not hide the
            # original exception. Retry as a short plain webhook message and
            # recreate the attachment because the first request may consume
            # its file stream before Discord rejects the payload.
            self.logger.exception("Failed to send component error report")
            fallback = (
                "Fishie error report (plain fallback)\n"
                f"Source: {source}\n"
                f"Author: {author_text}\n"
                f"Invocation: {invocation}"
            )[:1900]
            try:
                await self.error_logs.send(
                    content=fallback,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if include_traceback_file:
                    await self.error_logs.send(
                        content="Full Fishie traceback attached as `large.txt`.",
                        file=self.too_big(excinfo),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception:
                self.logger.exception("Failed to send plain error report fallback")
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

        await self.log_error(
            error,
            event=event,
            event_args=(*args, *kwargs.values()),
        )

        return await super().on_error(event, *args, **kwargs)

    async def load_extensions(self):
        required = {
            "extensions.context",
            "extensions.events",
            "extensions.logging",
            "extensions.moderation",
            "extensions.search",
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
        self._register_tracking_consent_checks()

    async def setup_hook(self) -> None:
        async with self.pool.acquire() as connection:
            await check_migrations(connection)

        self._status_rotation_index = random.randrange(len(ROTATING_STATUSES))
        self.activity = discord.CustomActivity(
            name=ROTATING_STATUSES[self._status_rotation_index]
        )

        self.error_logs = discord.Webhook.from_url(
            self.config["webhooks"]["error_logs"], session=self.session
        )

        await self.load_extensions()
        self._register_tracking_consent_checks()
        for cog in self.cogs.values():
            for command in cog.get_app_commands():
                describe_missing_app_parameters(command)
        await self.populate_cache()
        await update_pokemon(self)
        self.logger.info(f"Added {len(self.pokemon):,} pokemon")
        self._status_rotation_task = asyncio.create_task(
            self._rotate_statuses(), name="fishie-status-rotation"
        )

    async def _rotate_statuses(self) -> None:
        """Rotate the public custom status without changing the mobile identify."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(STATUS_ROTATION_INTERVAL)
            if self.is_closed():
                return

            self._status_rotation_index = (self._status_rotation_index + 1) % len(
                ROTATING_STATUSES
            )
            activity = discord.CustomActivity(
                name=ROTATING_STATUSES[self._status_rotation_index]
            )
            self.activity = activity
            try:
                await self.change_presence(activity=activity)
            except discord.DiscordException:
                self.logger.warning(
                    "Could not update the rotating custom status", exc_info=True
                )

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
        status_task = self._status_rotation_task
        self._status_rotation_task = None
        if status_task is not None and not status_task.done():
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
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

    async def refresh_account_cache(self, user_id: int) -> None:
        """Refresh cached Last.fm and AniList names after an account change."""
        row = await self.pool.fetchrow(
            "SELECT lastfm, anilist FROM accounts WHERE user_id = $1", user_id
        )
        self.db_cache.update_accounts(
            user_id,
            last_fm=row["lastfm"] if row else None,
            anilist=row["anilist"] if row else None,
        )

    async def populate_cache(self):
        self.db_cache.prefixes.clear()
        self.db_cache.opted_out.clear()
        self.db_cache.auto_downloads.clear()
        self.db_cache.poketwo_guilds.clear()
        self.db_cache.auto_reaction_guilds.clear()
        self.db_cache.nsfw_covers.clear()
        self.db_cache.pinboard.clear()
        self.db_cache.lastfm.clear()
        self.db_cache.anilist.clear()
        self.db_cache.disabled_commands.clear()
        self.db_cache.tracking_disabled_users.clear()
        self.db_cache.private_history_users.clear()
        self.db_cache.public_history_users.clear()
        self.db_cache.game_tracking_disabled_users.clear()
        self.db_cache.private_game_history_users.clear()
        self.db_cache.public_game_history_users.clear()
        self.db_cache.guild_tracking_disabled.clear()
        self.db_cache.private_guild_history.clear()
        self.db_cache.public_guild_history.clear()
        self.db_cache.tracking_consent_users.clear()
        self.db_cache.reaction_tracking_users.clear()
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
                game_tracking_enabled,
                game_history_public,
                tracking_consent
            FROM user_settings
            """)
        for row in user_privacy_settings:
            user_id = row["user_id"]
            if not row["tracking_enabled"]:
                self.db_cache.tracking_disabled_users.add(user_id)
            self.db_cache.set_history_public(user_id, bool(row["history_public"]))
            if not row["game_tracking_enabled"]:
                self.db_cache.game_tracking_disabled_users.add(user_id)
            self.db_cache.set_game_history_public(
                user_id, bool(row["game_history_public"])
            )
            if row["tracking_consent"]:
                self.db_cache.set_tracking_consent(user_id)

        reaction_tracking = await self.pool.fetch(
            "SELECT user_id FROM reaction_tracking WHERE enabled = TRUE"
        )
        for row in reaction_tracking:
            self.db_cache.enable_reaction_tracking(row["user_id"])

        guild_settings = await self.pool.fetch("SELECT * FROM guild_settings")
        for row in guild_settings:
            guild_id = row["guild_id"]
            adl = row["auto_download"]
            poketwo = row["poketwo"]
            auto_reactions = row["auto_reactions"]
            pinboard = row["pinboard"]
            if not row["tracking_enabled"]:
                self.db_cache.guild_tracking_disabled.add(guild_id)
            self.db_cache.set_guild_history_public(
                guild_id, bool(row["history_public"])
            )

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

        accounts = await self.pool.fetch(
            "SELECT user_id, lastfm, anilist FROM accounts"
        )
        for row in accounts:
            user_id: int = row["user_id"]
            last_fm: Optional[str] = row["lastfm"]
            anilist: Optional[str] = row["anilist"]
            self.db_cache.update_accounts(user_id, last_fm=last_fm, anilist=anilist)
            if last_fm:
                self.logger.info(
                    f'Added last.fm account "{last_fm}" to user "{user_id}"'
                )
            if anilist:
                self.logger.info(
                    f'Added AniList account "{anilist}" to user "{user_id}"'
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
