from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from utils import (
    UnknownAccount,
    SteamIDConverter,
    STEAM,
    ROBLOX,
    LASTFM,
    OSU,
    BlankException,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class LinkCog(CogBase):
    @commands.command(name="accounts")
    async def accounts(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows your linked accounts"""
        accounts = await ctx.bot.pool.fetchrow(
            "SELECT * FROM accounts WHERE user_id = $1", user.id
        )

        if not accounts:
            raise BlankException(f"{str(user)} has no linked accounts.")

        embed = discord.Embed()
        embed.set_author(
            name=f"{user.display_name} - Connected accounts",
            icon_url=user.display_avatar.url,
        )

        embed.add_field(name="Last.fm", value=accounts["lastfm"] or "Not set")
        embed.add_field(name="osu!", value=accounts["osu"] or "Not set")
        embed.add_field(name="Steam", value=accounts["steam"] or "Not set")
        embed.add_field(name="Roblox", value=accounts["roblox"] or "Not set")

        await ctx.send(
            embed=embed, view=DropdownView(ctx) if user.id == ctx.author.id else None
        )


class Dropdown(discord.ui.Select):
    def __init__(self, ctx: Context):
        self.ctx = ctx

        # Set the options that will be presented inside the dropdown
        options = [
            discord.SelectOption(
                label="Last.fm",
                description="Add or remove your last.fm account",
                emoji=LASTFM,
                value="lastfm",
            ),
            discord.SelectOption(
                label="osu!",
                description="Add or remove your osu account",
                emoji=OSU,
                value="osu",
            ),
            discord.SelectOption(
                label="Steam",
                description="Add or remove your steam account",
                emoji=STEAM,
                value="steam",
            ),
            discord.SelectOption(
                label="Roblox",
                description="Add or remove your roblox account",
                emoji=ROBLOX,
                value="roblox",
            ),
        ]

        super().__init__(
            placeholder="Add or remove one your accounts",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        dropdown = self
        account: Optional[str] = await self.ctx.bot.pool.fetchval(
            f"SELECT {value} FROM accounts WHERE user_id = $1", self.ctx.author.id
        )

        class Feedback(
            discord.ui.Modal, title=f"Add or remove your {value.capitalize()} account."
        ):
            name = discord.ui.TextInput(
                label=f"{value.capitalize()} username",
                placeholder=account if account else "Input your username",
                style=discord.TextStyle.short,
                required=False,
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                username = self.name.value
                user = dropdown.ctx.author
                if username == "":
                    pass
                elif value == "lastfm":
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,15}", username):
                        return await modal_interaction.response.send_message(
                            "Invalid username.", ephemeral=True
                        )
                elif value == "osu":
                    if not re.fullmatch(r"[a-zA-Z0-9_\s-]{2,16}", username):
                        return await modal_interaction.response.send_message(
                            "Invalid username.", ephemeral=True
                        )
                elif value == "steam":
                    username = SteamIDConverter(username)

                if username == "":
                    sql = f"""UPDATE accounts SET {value} = $1 WHERE user_id = $2 RETURNING *"""
                else:
                    sql = f"""
                   INSERT INTO accounts ({value}, user_id) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE
                   SET {value} = $1 WHERE accounts.user_id = $2
                   RETURNING *
                   """

                results = await dropdown.ctx.bot.pool.fetchrow(
                    sql,
                    None if username == "" else username,
                    dropdown.ctx.author.id,
                )

                await modal_interaction.response.defer()

                if modal_interaction.message is None or results is None:
                    return

                embed = discord.Embed(color=dropdown.ctx.bot.embedcolor)
                embed.set_author(
                    name=f"{user.display_name} - Connected accounts",
                    icon_url=user.display_avatar.url,
                )

                embed.add_field(name="Last.fm", value=results["lastfm"] or "Not set")
                embed.add_field(name="osu!", value=results["osu"] or "Not set")
                embed.add_field(name="Steam", value=results["steam"] or "Not set")
                embed.add_field(name="Roblox", value=results["roblox"] or "Not set")

                await modal_interaction.message.edit(embed=embed)

        await interaction.response.send_modal(Feedback())


class DropdownView(discord.ui.View):
    def __init__(self, ctx: Context):
        super().__init__()

        self.add_item(Dropdown(ctx))
