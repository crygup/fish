import pytest
from discord.ext import commands

from extensions.fun.library_uploads import (
    post_media_condition,
    split_library_filters,
    video_media_condition,
)


@pytest.mark.parametrize(
    ("value", "target", "all_users", "media"),
    (
        ("images @user", "@user", False, "image"),
        ("@user gifs", "@user", False, "gif"),
        ("video", None, False, "video"),
        ("all animated", None, True, "gif"),
        ("@user -static", "@user", False, "image"),
    ),
)
def test_library_filters_are_order_independent(
    value: str, target: str | None, all_users: bool, media: str
) -> None:
    assert split_library_filters(value) == (target, all_users, media)


def test_library_filters_keep_user_names_with_spaces() -> None:
    assert split_library_filters("Jane Doe png") == ("Jane Doe", False, "image")


def test_library_filters_reject_conflicting_media_types() -> None:
    with pytest.raises(commands.BadArgument, match="one media type"):
        split_library_filters("images gifs")


def test_library_filters_reject_all_and_user() -> None:
    with pytest.raises(commands.BadArgument, match="`all` or a Discord user"):
        split_library_filters("all @user")


def test_media_conditions_reject_incompatible_types() -> None:
    assert "%.gif" in post_media_condition("gif")
    assert "NOT LIKE" in post_media_condition("image")
    assert "NOT LIKE" in video_media_condition("video")
    with pytest.raises(commands.BadArgument, match="Posts only"):
        post_media_condition("video")
    with pytest.raises(commands.BadArgument, match="Videos only"):
        video_media_condition("gif")
