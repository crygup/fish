from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from discord.ext import commands

# Test doubles supply only the Discord/service fields exercised by each test.
from extensions.context import GuildContext
from extensions.moderation import Moderation
from extensions.moderation import mass as mass_module
from extensions.moderation.mass import Mass, _can_manage_mass


class _Member:
    def __init__(
        self,
        member_id: int,
        guild: SimpleNamespace,
        *,
        administrator: bool = False,
        top_role: int = 1,
        protection_role: int | None = None,
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.top_role = top_role
        self._protection_role = protection_role

    def get_role(self, role_id: int) -> object | None:
        return object() if role_id == self._protection_role else None


def _ctx(member: _Member, config: object | None) -> SimpleNamespace:
    configs = {member.guild.id: config} if config is not None else {}
    return SimpleNamespace(
        author=member,
        guild=member.guild,
        cog=SimpleNamespace(_protection_configs=configs),
    )


def test_mass_permission_boundary_matches_protection_managers(monkeypatch) -> None:
    monkeypatch.setattr(mass_module.discord, "Member", _Member)
    guild = SimpleNamespace(
        id=50,
        owner_id=1,
        me=SimpleNamespace(top_role=10),
    )
    config = SimpleNamespace(protection_role_id=99)

    assert _can_manage_mass(cast("GuildContext", _ctx(_Member(1, guild), config)))
    assert _can_manage_mass(
        cast("GuildContext", _ctx(_Member(2, guild, protection_role=99), config))
    )
    assert _can_manage_mass(
        cast(
            "GuildContext",
            _ctx(_Member(3, guild, administrator=True, top_role=11), config),
        )
    )
    assert not _can_manage_mass(
        cast(
            "GuildContext",
            _ctx(_Member(4, guild, administrator=True, top_role=10), config),
        )
    )
    assert not _can_manage_mass(cast("GuildContext", _ctx(_Member(5, guild), config)))


def test_mass_runtime_check_allows_protection_role_without_native_permissions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(mass_module.discord, "Member", _Member)
    guild = SimpleNamespace(
        id=50,
        owner_id=1,
        me=SimpleNamespace(
            top_role=10,
            guild_permissions=SimpleNamespace(ban_members=True),
        ),
    )
    config = SimpleNamespace(protection_role_id=99)
    ctx = _ctx(_Member(2, guild, protection_role=99), config)

    Mass._ensure_mass_permissions(cast("GuildContext", ctx), "ban")


def test_every_mass_app_command_has_the_protection_manager_check() -> None:
    command = next(item for item in Moderation.__cog_commands__ if item.name == "mass")
    assert isinstance(command, commands.Group)
    for subcommand in command.walk_commands():
        assert isinstance(subcommand, (commands.HybridCommand, commands.HybridGroup))
        checks = getattr(subcommand.app_command, "checks", ())
        assert any(
            getattr(check, "__name__", "") == "_mass_app_manager_check"
            for check in checks
        ), subcommand.qualified_name
