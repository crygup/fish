from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord.interactions import Interaction

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
            "View %s errored for user_id=%s command=%s",
            type(self).__name__,
            self.ctx.author.id,
            getattr(self.ctx.command, "qualified_name", None),
        )
        await self.ctx.bot.log_error(
            error,
            context=self.ctx,
            interaction=interaction,
        )

        try:
            await interaction.response.send_message(str(error), ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(content=str(error), ephemeral=True)


class AuthorLayoutView(discord.ui.LayoutView):
    """Components V2 counterpart to :class:`AuthorView`.

    Settings panels use this base so their status text, controls, and
    selectors are always sent as a Components V2 layout while retaining the
    same author-only interaction and timeout behavior as the older views.
    """

    def __init__(self, ctx: Context, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None
        self.ctx = ctx

    def disable_all(self) -> None:
        for item in self.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this setting can use it.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_error(self, interaction: Interaction, error: Exception, _item) -> None:
        self.ctx.bot.logger.info(
            "View %s errored for user_id=%s command=%s",
            type(self).__name__,
            self.ctx.author.id,
            getattr(self.ctx.command, "qualified_name", None),
        )
        await self.ctx.bot.log_error(
            error,
            context=self.ctx,
            interaction=interaction,
        )
        try:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                content=str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
