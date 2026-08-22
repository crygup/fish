from types import SimpleNamespace

import pytest

from extensions.fun.post import PostCommands
from extensions.fun.video import VideoCommands

REVIEW_GUILD_ID = 939497177821110272
REVIEW_ROLE_ID = 1538971877563703427


def _interaction(*, guild_id: int = REVIEW_GUILD_ID, role_id: int | None = None):
    roles = (SimpleNamespace(id=role_id),) if role_id is not None else ()
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_guild=False),
        roles=roles,
    )
    user = SimpleNamespace(id=123)
    guild = SimpleNamespace(id=guild_id, get_member=lambda _user_id: member)
    return SimpleNamespace(user=user, guild=guild)


@pytest.mark.asyncio
@pytest.mark.parametrize("cog_type", (VideoCommands, PostCommands))
async def test_review_role_can_use_upload_controls(cog_type: type) -> None:
    cog = cog_type()
    cog.bot = SimpleNamespace(config={"ids": {"owner_id": "999"}})

    assert await cog._can_review(  # type: ignore[attr-defined]
        _interaction(role_id=REVIEW_ROLE_ID)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cog_type", (VideoCommands, PostCommands))
async def test_review_role_does_not_work_outside_review_guild(cog_type: type) -> None:
    cog = cog_type()
    cog.bot = SimpleNamespace(config={"ids": {"owner_id": "999"}})

    assert not await cog._can_review(  # type: ignore[attr-defined]
        _interaction(guild_id=1, role_id=REVIEW_ROLE_ID)
    )
