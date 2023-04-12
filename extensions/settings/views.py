from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from core import AuthorView

if TYPE_CHECKING:
    from extensions.context import Context


class AccountsSelect(discord.ui.Select[Any]):
    ...


class AccountsView(AuthorView):
    def __init__(self, *, ctx: Context):
        super().__init__(ctx=ctx)
