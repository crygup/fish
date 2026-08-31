"""History views stay text-only while current profile views remain available."""

from extensions.discord_ext.info import HISTORY_APP_COMMANDS_ENABLED, Info


def test_saved_history_app_commands_are_disabled() -> None:
    assert HISTORY_APP_COMMANDS_ENABLED is False

    # These are HybridCommands so their text handlers remain available, but
    # ``with_app_command=False`` keeps them out of every app-command tree.
    assert Info.user_avatar_history.app_command is None
    assert Info.user_avatar_list.app_command is None
    assert Info.user_usernames.app_command is None
    assert Info.user_display_names.app_command is None

    # Their parent still exposes the current-avatar lookup as an app command;
    # only the saved-history children are intentionally text-only.
    assert Info.user_avatar_group.app_command is not None
    assert all(
        child.app_command is None
        for child in (Info.user_avatar_history, Info.user_avatar_list)
    )


def test_avatar_and_icon_app_commands_do_not_expose_history_options() -> None:
    assert [parameter.name for parameter in Info.avatar_app.parameters] == ["user"]
    assert list(Info.icon_app.parameters) == []
