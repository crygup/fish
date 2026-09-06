from __future__ import annotations

from types import SimpleNamespace

# Test doubles supply only the Discord/service fields exercised by each test.
from typing import cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from core import Fishie
from extensions.moderation import Moderation
from extensions.moderation.protection import (
    INVITE_RE,
    TRIGGER_LABELS,
    Protection,
    ProtectionConfig,
    _normalize_mode,
    _normalize_trigger,
)


def _entry(
    action: discord.AuditLogAction,
    *,
    before: object | None = None,
    after: object | None = None,
    target: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        action=action,
        before=before or SimpleNamespace(),
        after=after or SimpleNamespace(),
        target=target or SimpleNamespace(),
    )


def test_protection_group_is_hybrid_with_requested_aliases_and_commands() -> None:
    command = next(
        item for item in Moderation.__cog_commands__ if item.name == "protection"
    )
    assert isinstance(command, commands.HybridGroup)
    assert command.fallback == "info"
    assert {
        "antinuke",
        "serverproection",
        "serverprotection",
        "protect",
        "antiraid",
    }.issubset(command.aliases)
    assert {
        "protection enable",
        "protection disable",
        "protection action",
        "protection channel",
        "protection lock",
        "protection unlock",
        "protection role",
        "protection role set",
        "protection role remove",
    }.issubset({item.qualified_name for item in command.walk_commands()})


def test_protection_trigger_and_mode_aliases_are_user_friendly() -> None:
    assert _normalize_trigger("mass bans") == "mass_bans"
    assert _normalize_trigger("channel") == "channel_changes"
    assert _normalize_trigger("admin-role") == "administrator_role"
    assert _normalize_mode("warning") == "warn"
    assert _normalize_mode("timeout") == "lock"


def test_protection_classifies_requested_audit_events() -> None:
    assert Protection._classify_protection_entry(
        cast("discord.AuditLogEntry", _entry(discord.AuditLogAction.ban))
    ) == ("mass_bans",)
    assert Protection._classify_protection_entry(
        cast("discord.AuditLogEntry", _entry(discord.AuditLogAction.kick))
    ) == ("mass_kicks",)
    assert Protection._classify_protection_entry(
        cast("discord.AuditLogEntry", _entry(discord.AuditLogAction.channel_delete))
    ) == ("channel_changes",)
    assert Protection._classify_protection_entry(
        cast(
            "discord.AuditLogEntry",
            _entry(
                discord.AuditLogAction.channel_update,
                before=SimpleNamespace(name="old"),
                after=SimpleNamespace(name="new"),
            ),
        )
    ) == ("channel_changes",)


def test_protection_detects_admin_role_creation_and_elevation() -> None:
    regular = discord.Permissions.none()
    administrator = discord.Permissions(administrator=True)
    assert Protection._classify_protection_entry(
        cast(
            "discord.AuditLogEntry",
            _entry(
                discord.AuditLogAction.role_create,
                target=SimpleNamespace(permissions=administrator),
            ),
        )
    ) == ("administrator_role",)
    assert Protection._classify_protection_entry(
        cast(
            "discord.AuditLogEntry",
            _entry(
                discord.AuditLogAction.role_update,
                before=SimpleNamespace(name="role", permissions=regular),
                after=SimpleNamespace(name="role", permissions=administrator),
            ),
        )
    ) == ("administrator_role",)


def test_protection_detects_timeouts_security_and_vanity_changes() -> None:
    timeout = discord.utils.utcnow()
    assert Protection._classify_protection_entry(
        cast(
            "discord.AuditLogEntry",
            _entry(
                discord.AuditLogAction.member_update,
                before=SimpleNamespace(timed_out_until=None),
                after=SimpleNamespace(timed_out_until=timeout),
            ),
        )
    ) == ("member_timeouts",)
    assert set(
        Protection._classify_protection_entry(
            cast(
                "discord.AuditLogEntry",
                _entry(
                    discord.AuditLogAction.guild_update,
                    before=SimpleNamespace(
                        verification_level=discord.VerificationLevel.low,
                        vanity_url_code="old",
                    ),
                    after=SimpleNamespace(
                        verification_level=discord.VerificationLevel.high,
                        vanity_url_code="new",
                    ),
                ),
            )
        )
    ) == {"security_level", "vanity_url"}


def test_protection_mass_threshold_is_per_actor_and_window(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(
        "extensions.moderation.protection.time.monotonic", lambda: clock[0]
    )
    protection = object.__new__(Protection)
    protection._protection_windows = {}
    protection._protection_cooldowns = {}
    assert protection._threshold_reached(1, 10, "mass_bans") == (False, 1)
    assert protection._threshold_reached(1, 10, "mass_bans") == (False, 2)
    assert protection._threshold_reached(1, 11, "mass_bans") == (False, 1)
    assert protection._threshold_reached(1, 10, "mass_bans") == (True, 3)
    clock[0] += 61
    assert protection._threshold_reached(1, 10, "mass_bans") == (False, 1)


def test_protection_permission_boundary_allows_only_requested_managers() -> None:
    bot_role = 10
    guild = SimpleNamespace(owner_id=1, me=SimpleNamespace(top_role=bot_role))
    config = ProtectionConfig(
        guild_id=50,
        channel_id=60,
        protection_role_id=99,
        enabled=True,
        response_mode="both",
        allowed_vanity_code=None,
        enabled_triggers=frozenset(TRIGGER_LABELS),
    )

    def member(
        member_id: int,
        *,
        administrator: bool = False,
        top_role: int = 1,
        protection_role: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=member_id,
            guild=guild,
            guild_permissions=SimpleNamespace(administrator=administrator),
            top_role=top_role,
            get_role=lambda role_id: (
                object() if protection_role and role_id == 99 else None
            ),
        )

    assert Protection._can_manage_protection(
        cast("discord.Member", member(1)), config, allow_role=True
    )
    assert Protection._can_manage_protection(
        cast("discord.Member", member(2, protection_role=True)), config, allow_role=True
    )
    assert Protection._can_manage_protection(
        cast("discord.Member", member(3, administrator=True, top_role=11)),
        config,
        allow_role=True,
    )
    assert not Protection._can_manage_protection(
        cast("discord.Member", member(4, administrator=True, top_role=10)),
        config,
        allow_role=True,
    )
    assert not Protection._can_manage_protection(
        cast("discord.Member", member(5)), config, allow_role=True
    )
    assert not Protection._can_manage_protection(
        cast("discord.Member", member(2, protection_role=True)),
        config,
        allow_role=False,
    )


def test_protection_invite_detection_matches_discord_invites_only() -> None:
    assert INVITE_RE.search("join https://discord.gg/example")
    assert INVITE_RE.search("https://discord.com/invite/example")
    assert INVITE_RE.search("discordapp.com/invite/example")
    assert not INVITE_RE.search("https://example.com/invite/test")


class _Role:
    def __init__(
        self,
        role_id: int,
        position: int,
        *,
        default: bool = False,
        managed: bool = False,
    ) -> None:
        self.id = role_id
        self.position = position
        self._default = default
        self.managed = managed

    def is_default(self) -> bool:
        return self._default

    def __lt__(self, other: object) -> bool:
        return self.position < getattr(other, "position", -1)

    def __ge__(self, other: object) -> bool:
        return self.position >= getattr(other, "position", -1)


def test_protection_only_strips_normal_roles_below_fishie() -> None:
    me = SimpleNamespace(top_role=_Role(500, 50))
    normal = _Role(1, 10)
    managed = _Role(2, 20, managed=True)
    default = _Role(3, 0, default=True)
    above = _Role(4, 60)
    member = SimpleNamespace(roles=[default, normal, managed, above])

    assert Protection._removable_protection_roles(
        cast("discord.Member", member), cast("discord.Member", me)
    ) == [normal]


@pytest.mark.asyncio
async def test_protection_lock_saves_and_strips_roles_before_timeout() -> None:
    normal = _Role(10, 10)
    default = _Role(1, 0, default=True)
    me = SimpleNamespace(
        id=900,
        top_role=_Role(900, 50),
        guild_permissions=SimpleNamespace(manage_roles=True, moderate_members=True),
    )
    member = SimpleNamespace(
        id=42,
        bot=False,
        top_role=normal,
        roles=[default, normal],
        remove_roles=AsyncMock(),
        timeout=AsyncMock(),
    )

    class Pool:
        def __init__(self) -> None:
            self.execute = AsyncMock()

        async def fetchrow(self, *_args: object) -> None:
            return None

    protection = object.__new__(Protection)
    pool = Pool()
    protection.bot = cast("Fishie", SimpleNamespace(pool=pool))
    guild = SimpleNamespace(id=5, owner_id=1, me=me)

    complete, details = await protection._lock_protection_member(
        cast("discord.Guild", guild), cast("discord.Member", member), "mass_bans"
    )

    assert complete
    assert "Stripped 1 role" in details
    member.remove_roles.assert_awaited_once()
    member.timeout.assert_awaited_once()
    assert pool.execute.await_args is not None
    saved_roles = pool.execute.await_args.args[3]
    assert saved_roles == [10]
