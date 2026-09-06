"""Saved history views are available to app commands behind one switch."""

from extensions.discord_ext.info import HISTORY_APP_COMMANDS_ENABLED, Info


def test_saved_history_app_commands_are_enabled() -> None:
    assert HISTORY_APP_COMMANDS_ENABLED is True

    # These are HybridCommands, so their text handlers remain available while
    # the shared switch exposes their app-command variants as well.
    assert Info.user_avatar_history.app_command is not None
    assert Info.user_avatar_list.app_command is not None
    assert Info.user_usernames.app_command is not None
    assert Info.user_display_names.app_command is not None

    # Their parent still exposes the current-avatar lookup as an app command,
    # and the saved-history children remain available in the same group.
    assert Info.user_avatar_group.app_command is not None
    assert all(
        child.app_command is not None
        for child in (Info.user_avatar_history, Info.user_avatar_list)
    )


def test_avatar_and_icon_app_commands_expose_history_options() -> None:
    assert [parameter.name for parameter in Info.avatar_app.parameters] == [
        "user",
        "history",
        "list_mode",
        "server",
        "size",
        "edit",
    ]
    assert [parameter.name for parameter in Info.icon_app.parameters] == [
        "history",
        "list_mode",
        "size",
        "edit",
    ]
