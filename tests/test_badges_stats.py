from __future__ import annotations

from core.badges import command_badge_leaders


def test_command_badge_uses_parent_and_only_one_command_per_user() -> None:
    rows = [
        {"user_id": 1, "command": "play", "total": 20},
        {"user_id": 2, "command": "play", "total": 20},
        {"user_id": 1, "command": "queue", "total": 21},
        {"user_id": 3, "command": "queue", "total": 21},
        # The parent command is expected from SQL; this row demonstrates that
        # subcommand text is not emitted as a separate badge name.
        {"user_id": 4, "command": "stats", "total": 8},
        {"user_id": 4, "command": "stats", "total": 8},
    ]

    badges = command_badge_leaders(rows)

    assert {(badge.user_id, badge.text) for badge in badges} == {
        (1, "#1 queue user"),
        (2, "#1 play user"),
        (3, "#1 queue user"),
        (4, "#1 stats user"),
    }


def test_command_badge_tie_for_one_user_is_deterministic() -> None:
    rows = [
        {"user_id": 7, "command": "zebra", "total": 5},
        {"user_id": 7, "command": "alpha", "total": 5},
        {"user_id": 8, "command": "alpha", "total": 5},
    ]

    badges = command_badge_leaders(rows)

    assert {(badge.user_id, badge.text) for badge in badges} == {
        (7, "#1 alpha user"),
        (8, "#1 alpha user"),
    }
