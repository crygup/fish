from __future__ import annotations

from typing import Any

import discord

import extensions.moderation.logger as logger_module
from extensions.moderation.logger import (
    _channel_permission_changes,
    _role_permission_changes,
)


def test_role_permission_changes_show_allowed_and_removed_permissions() -> None:
    before = discord.Permissions.none()
    before.update(send_messages=True)
    after = discord.Permissions.none()
    after.update(manage_messages=True)

    changes = _role_permission_changes(before, after)

    assert "**Removed:** Send Messages" in changes
    assert "**Allowed:** Manage Messages" in changes


def test_channel_permission_changes_identify_overall_roles_and_users(
    monkeypatch: Any,
) -> None:
    class FakeRole:
        def __init__(self, target_id: int, mention: str, *, default: bool = False):
            self.id = target_id
            self.mention = mention
            self._default = default

        def is_default(self) -> bool:
            return self._default

    class FakeMember:
        def __init__(self, target_id: int, mention: str):
            self.id = target_id
            self.mention = mention

    class FakeChannel:
        def __init__(self, overwrites: dict[Any, discord.PermissionOverwrite]):
            self.overwrites = overwrites

        def overwrites_for(self, target: Any) -> discord.PermissionOverwrite:
            return self.overwrites[target]

    monkeypatch.setattr(logger_module.discord, "Role", FakeRole)
    monkeypatch.setattr(logger_module.discord, "Member", FakeMember)

    everyone = FakeRole(1, "@everyone", default=True)
    role = FakeRole(2, "<@&2>")
    user = FakeMember(3, "<@3>")
    before = FakeChannel(
        {
            everyone: discord.PermissionOverwrite(view_channel=None),
            role: discord.PermissionOverwrite(send_messages=False),
        }
    )
    after = FakeChannel(
        {
            everyone: discord.PermissionOverwrite(view_channel=True),
            role: discord.PermissionOverwrite(send_messages=True),
            user: discord.PermissionOverwrite(manage_messages=False),
        }
    )

    changes, action = _channel_permission_changes(before, after)  # type: ignore[arg-type]
    text = "\n".join(changes)

    assert "Overall (`@everyone`)" in text
    assert "<@&2> (Role)" in text
    assert "<@3> (User)" in text
    assert "Allowed View Channel" in text
    assert "Denied Manage Messages" in text
    assert action is discord.AuditLogAction.overwrite_create
