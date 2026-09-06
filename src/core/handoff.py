"""Helpers for retiring the legacy Fishie application.

The project runs the legacy and replacement applications from the same source
tree.  Keeping the handoff notice and invite construction in one small module
prevents text commands, application commands, and automatic downloads from
drifting apart while the two instances are online during the transition.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import discord

# The replacement application's client ID is intentionally a constant here:
# it is public information used in install links, unlike its bot token.
NEW_BOT_ID = 1537535633038381190
DEADLINE_LABEL = "September 9th"


def _instance_value(bot: Any, name: str) -> Any:
    """Read an instance attribute, supporting both properties and callables."""

    try:
        value = getattr(bot, name, None)
    except Exception:
        # A runtime identity property may briefly be unavailable while a bot
        # object is being constructed.  Handoff checks must never turn that
        # startup race into a command error.
        return None
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def is_legacy_instance(bot: Any) -> bool:
    """Return whether *bot* is running the retiring application.

    ``runtime_architecture`` exposes ``is_legacy_bot``.  The fallback keeps
    this helper usable in tests and during a rolling deployment before that
    property has been initialized; testing bots are never blocked.
    """

    explicit = _instance_value(bot, "is_legacy_bot")
    if explicit is not None:
        return bool(explicit)
    explicit = _instance_value(bot, "legacy_instance")
    if explicit is not None:
        return bool(explicit)
    instance = _instance_value(bot, "instance")
    if instance is None:
        instance = _instance_value(bot, "bot_instance")
    if instance is None:
        return False
    return str(instance).casefold() in {"legacy", "old", "primary"}


def is_handoff_exempt_command(command: Any) -> bool:
    """Return whether a command must remain available on the old bot.

    Owner/recovery controls are deliberately kept usable so the old process
    can be shut down or repaired after the replacement is installed.  These
    names are matched against the complete qualified name and root command.
    """

    qualified = str(getattr(command, "qualified_name", "")).casefold().strip()
    root = qualified.split(" ", 1)[0] if qualified else ""
    if not qualified:
        return True
    if root in {"dev", "shutdown", "restart"}:
        return True
    # The owner cog is excluded by module as well, because nested owner
    # command names are intentionally free to evolve.
    binding = getattr(command, "binding", None) or getattr(command, "cog", None)
    module = getattr(getattr(binding, "__class__", None), "__module__", "")
    return str(module).startswith("extensions.owner")


def replacement_client_id(bot: Any | None = None) -> int:
    """Resolve the replacement app ID from runtime/config, with a safe fallback."""

    if bot is not None:
        for attr in ("new_bot_id", "replacement_bot_id", "handoff_bot_id"):
            value = _instance_value(bot, attr)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        config = getattr(bot, "config", None)
        ids = config.get("ids") if isinstance(config, Mapping) else None
        if isinstance(ids, Mapping):
            for key in ("new_bot_id", "replacement_bot_id"):
                value = ids.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        pass
    return NEW_BOT_ID


def _bot_permissions(bot: Any | None) -> discord.Permissions:
    permissions = getattr(bot, "bot_permissions", None)
    if callable(permissions):
        permissions = permissions()
    if isinstance(permissions, discord.Permissions):
        # Copy the value so callers cannot mutate the bot's shared object.
        return discord.Permissions(permissions.value)
    return discord.Permissions.none()


def profile_install_url(bot: Any | None = None) -> str:
    """Build a user-install link (applications.commands only)."""

    app_id = replacement_client_id(bot)
    return (
        "https://discord.com/oauth2/authorize?"
        f"client_id={app_id}&scope=applications.commands&integration_type=1"
    )


def guild_install_url(
    bot: Any | None,
    guild: discord.abc.Snowflake | None,
    *,
    permissions: discord.Permissions | None = None,
) -> str:
    """Build a guild-install link with application-command support."""

    app_id = replacement_client_id(bot)
    requested = _bot_permissions(bot) if permissions is None else permissions
    kwargs: dict[str, Any] = {
        "permissions": requested,
        "scopes": ("bot", "applications.commands"),
    }
    if guild is not None:
        kwargs.update(guild=guild, disable_guild_select=True)
    return discord.utils.oauth_url(app_id, **kwargs)


def handoff_notice(bot: Any | None = None, guild: Any | None = None) -> str:
    """Return the complete migration notice and all replacement links."""

    profile = profile_install_url(bot)
    with_permissions = guild_install_url(bot, guild)
    without_permissions = guild_install_url(
        bot, guild, permissions=discord.Permissions.none()
    )
    guild_suffix = "" if guild is not None else " (choose a server during install)"
    return (
        "## Fishie is moving to a new bot\n"
        f"This bot will go offline on **{DEADLINE_LABEL}** because Discord is "
        "changing how bot verification works. Your user and server settings "
        "will transfer seamlessly.\n\n"
        f"[Add Fishie to your profile]({profile})\n"
        f"[Add Fishie to this server{guild_suffix}]({with_permissions})\n"
        f"[Add Fishie with no permissions{guild_suffix}]({without_permissions})"
    )


def notice_expiry(seconds: float = 3600.0) -> datetime.datetime:
    """Return a UTC expiry for the in-memory notice throttle."""

    return discord.utils.utcnow() + datetime.timedelta(seconds=max(0.0, seconds))


async def _send_interaction_notice(
    bot: Any, interaction: discord.Interaction[Any], content: str
) -> bool:
    if interaction.response.is_done():
        await interaction.followup.send(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    return True


async def send_handoff_notice(
    bot: Any,
    destination: Any,
    *,
    guild: Any | None = None,
    ephemeral: bool | None = None,
) -> bool:
    """Send a replacement notice to a context, interaction, or channel.

    Returns ``True`` when a send was attempted.  The function intentionally
    suppresses mention parsing in every path and avoids raising if a channel
    disappears while the old bot is being retired.
    """

    if guild is None:
        guild = getattr(destination, "guild", None)
        if guild is None:
            interaction = getattr(destination, "interaction", None)
            guild = getattr(interaction, "guild", None)
    content = handoff_notice(bot, guild)
    interaction = destination if isinstance(destination, discord.Interaction) else None
    if interaction is None:
        interaction = getattr(destination, "interaction", None)
    try:
        if isinstance(interaction, discord.Interaction):
            return await _send_interaction_notice(bot, interaction, content)

        send = getattr(destination, "send", None)
        if not callable(send):
            return False
        send_message = cast(Callable[..., Awaitable[Any]], send)
        kwargs: dict[str, Any] = {
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        # Context.send accepts ephemeral for app commands.  A regular
        # Messageable does not, so only include it when explicitly requested.
        if (
            ephemeral is not None
            and getattr(destination, "interaction", None) is not None
        ):
            kwargs["ephemeral"] = bool(ephemeral)
        await send_message(content, **kwargs)
        return True
    except (discord.HTTPException, discord.Forbidden, TypeError, ValueError):
        logger = getattr(bot, "logger", None)
        log_exception = getattr(logger, "debug", None)
        if callable(log_exception):
            log_exception(
                "Could not send replacement-bot handoff notice", exc_info=True
            )
        return False


class HandoffNoticeThrottle:
    """Small per-user/channel throttle for automatic-download warnings."""

    def __init__(self, *, ttl: float = 3600.0) -> None:
        self.ttl = max(1.0, float(ttl))
        self._seen: dict[tuple[int, int], datetime.datetime] = {}
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int, channel_id: int) -> bool:
        now = discord.utils.utcnow()
        key = (int(user_id), int(channel_id))
        async with self._lock:
            for stale_key, expires in tuple(self._seen.items()):
                if expires <= now:
                    self._seen.pop(stale_key, None)
            if key in self._seen:
                return False
            self._seen[key] = now + datetime.timedelta(seconds=self.ttl)
            return True
