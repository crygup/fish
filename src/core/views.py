from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

import discord

if TYPE_CHECKING:
    from extensions.context import Context


@runtime_checkable
class Disableable(Protocol):
    disabled: bool


class AuthorView(discord.ui.View):
    message: Optional[discord.Message]

    def __init__(self, *, ctx: Context, timeout: Optional[float] = 180):
        self.ctx = ctx
        super().__init__(timeout=timeout)

    def disable_all(self) -> None:
        for button in self.children:
            if isinstance(button, Disableable):
                button.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message:
            await self.message.edit(view=self)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if (
            interaction.user.id == self.ctx.bot.config["ids"]["owner_id"]
            or interaction.user.id == self.ctx.author.id
        ):
            return True

        await interaction.response.send_message(
            f"Sorry, this is not for you. If you would like to use this however you can run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ''}`"
        )
        return False
