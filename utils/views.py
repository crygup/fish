from __future__ import annotations

import sys
import traceback
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.interactions import Interaction
from discord.ui.item import Item

if TYPE_CHECKING:
    from extensions.context import Context


class AuthorView(discord.ui.View):
    def __init__(self, ctx: Context, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)

        self.message: Optional[discord.Message] = None
        self.ctx = ctx

    def disable_all(self) -> None:
        for button in self.children:
            if isinstance(button, discord.ui.Button):
                button.disabled = True
            if isinstance(button, discord.ui.Select):
                button.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message:
            await self.message.edit(view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user and interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message(
            f'You can\'t use this, sorry. \nIf you\'d like to use this then run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ""}`',
            ephemeral=True,
        )
        return False

    async def on_error(self, interaction: Interaction, error: Exception, _):
        self.ctx.bot.logger.info(
            f'View {self} errored by {self.ctx.author}. Full content: "{self.ctx.message.content}"'
        )
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )

        try:
            await interaction.response.send_message(str(error), ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(content=str(error), ephemeral=True)
