from __future__ import annotations

import re
import textwrap
from typing import Dict, List, Optional, TypeAlias

import discord
from bot import Bot, Context
from discord.ext import commands
from utils import FrontHelpPageSource, Pager, human_join, AuthorView


async def setup(bot: Bot):
    await bot.add_cog(HelpCog(bot))


class CommandView(AuthorView):
    def __init__(self, ctx: Context, cog: commands.Cog):
        super().__init__(ctx)
        self.add_item(CommandDropdown(ctx, cog))


class CogView(AuthorView):
    def __init__(self, ctx: Context, cogs: List[commands.Cog]):
        super().__init__(ctx)
        self.add_item(HelpDropdown(ctx, cogs, main_help=False))


class CommandDropdown(discord.ui.Select):
    def __init__(self, ctx: Context, cog: commands.Cog):
        self.ctx = ctx
        self.cog = cog

        options = []

        for cmd in cog.get_commands():
            options.append(
                discord.SelectOption(
                    label=cmd.name.title(),
                    value=cmd.name.lower().replace(" ", "_"),
                    description=f'{cmd.help.splitlines()[0].capitalize()}{"." if not cmd.help.splitlines()[0].endswith(".") else ""}'
                    if cmd.help
                    else None
                    if cmd.description != ""
                    else None,
                    emoji="\U0001f536"
                    if isinstance(cmd, commands.Group)
                    else "\U0001f537",
                )
            )

        super().__init__(placeholder="Select a command", options=options)

    async def make_embed(
        self, embed: discord.Embed, command: commands.Command | commands.Group
    ) -> discord.Embed:
        ctx = self.ctx
        bot = ctx.bot

        if bot.user is None:
            raise RuntimeError("Bot user is None")

        embed.set_author(
            name=f"{command.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = (
            f"```{command.help}```" if command.help else "No help yet..."
        )

        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in command.aliases], final="and"
                ),
                inline=False,
            )

        if isinstance(command, commands.Group) and command.commands:
            embed.add_field(
                name="Subcommands",
                value=human_join(
                    [f"**`{subcmd}`**" for subcmd in command.commands], final="and"
                ),
                inline=False,
            )

        if command.cooldown:
            cd = command.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        return embed

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        bot = ctx.bot
        embed = discord.Embed(color=bot.embedcolor)
        if bot.user is None:
            return

        cmd = bot.get_command(self.values[0])

        if cmd is None:
            await interaction.response.send_message(
                "Unable to find that command?", ephemeral=True
            )
            return

        embed = await self.make_embed(embed, cmd)
        self.placeholder = cmd.name.title()

        if interaction.message is None:
            return

        await interaction.message.edit(embed=embed, view=self.view)
        await interaction.response.defer()


class HelpDropdown(discord.ui.Select):
    view: HelpView

    def __init__(
        self,
        ctx: Context,
        cogs: List[commands.Cog],
        embed: Optional[discord.Embed] = None,
        main_help: bool = True,
    ):
        self.ctx = ctx
        self.cogs = cogs
        self.defaut_embed = embed
        self.view_added = False
        self.main_help = main_help

        options = []
        if main_help:
            options.append(
                discord.SelectOption(
                    label="Index",
                    value="index",
                    description="Goes back to the home page.",
                    emoji="\U0001f3e0",
                )
            )
        for cog in cogs:
            if cog.qualified_name == "":
                continue

            options.append(
                discord.SelectOption(
                    label=cog.qualified_name.title(),
                    value=cog.qualified_name.lower().replace(" ", "_"),
                    description=f'{cog.description.capitalize()}{"." if not cog.description.endswith(".") else ""}'
                    if cog.description != ""
                    else None,
                    emoji=getattr(cog, "display_emoji", None),
                )
            )

        super().__init__(placeholder="Select a category", options=options)

    async def make_embed(
        self, embed: discord.Embed, cog: commands.Cog
    ) -> discord.Embed:
        ctx = self.ctx
        bot = ctx.bot

        if bot.user is None:
            raise RuntimeError("Bot user is None")

        embed.set_author(
            name=f"{cog.qualified_name.title()} Help",
            icon_url=bot.user.display_avatar.url,
        )
        embed.description = cog.description
        embed.color = bot.embedcolor

        cmds = cog.get_commands()

        if len(cmds) == 0:
            embed.description += "\n\nNo commands found."
            return embed

        embed.add_field(
            name="Commands",
            value=human_join([f"`{c.name}`" for c in cmds], final="and"),
            inline=False,
        )

        return embed

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        bot = ctx.bot
        embed = discord.Embed(color=bot.embedcolor)
        if bot.user is None:
            return

        if self.values[0] == "index":
            embed = self.defaut_embed
            self.view.remove_item(self.view.children[-1])
            self.view_added = False
            self.placeholder = "Select a category"
        else:
            if self.view_added:
                self.view.remove_item(self.view.children[-1])

            cog = bot.get_cog(self.values[0])

            if cog is None:
                await interaction.response.send_message(
                    "Unable to find that cog?", ephemeral=True
                )
                return

            embed = await self.make_embed(embed, cog)
            self.view.add_item(CommandDropdown(ctx, cog))
            self.view_added = True
            self.placeholder = cog.qualified_name.title()

        if interaction.message is None:
            return

        await interaction.message.edit(embed=embed, view=self.view)
        await interaction.response.defer()


class HelpCommandDropdown(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Select a command", options=[])


class HelpView(AuthorView):
    def __init__(self, ctx: Context, cogs: List[commands.Cog], embed: discord.Embed):
        super().__init__(ctx)
        self.add_item(HelpDropdown(ctx, cogs, embed))


class MyHelp(commands.HelpCommand):
    context: Context

    async def send_bot_help(self, mapping: Dict[commands.Cog, List[commands.Command]]):
        bot = self.context.bot
        ctx = self.context
        if bot.user is None:
            return

        embed = discord.Embed(color=bot.embedcolor)
        embed.set_author(
            name=f"{bot.user} help",
            icon_url=bot.user.display_avatar.url,
        )

        usage = """
        My default prefix is `fish`, <@876391494485950504> also works.
        
        Command help will be shown like this
        `fish userinfo [user]`

        Arguments with **[]** around them are optional, while arguments with **<>** around them are required for the command to work.
        Don't actually include the symbols in the command.
        """

        embed.add_field(name="Command usage", value=textwrap.dedent(usage))

        getting_started = """
        To look at more commands/categories, use `fish help [command|category]`

        Optionally, you can use the select menu down below to browse.
        """

        embed.add_field(name="Getting started", value=textwrap.dedent(getting_started))

        filtered_cogs = [
            cog
            for _, cog in bot.cogs.items()
            if len(await self.filter_commands(cog.get_commands())) > 0
        ]

        await ctx.send(embed=embed, view=HelpView(ctx, filtered_cogs, embed))

    async def send_command_help(self, command: commands.Command):
        bot = self.context.bot
        ctx = self.context

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{command.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = (
            f"```{command.help}```" if command.help else "No help yet..."
        )

        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in command.aliases], final="and"
                ),
                inline=False,
            )

        if command.cooldown:
            cd = command.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        await ctx.send(embed=embed, view=CommandView(ctx, command.cog))

    async def send_group_help(self, group: commands.Group):
        bot = self.context.bot
        ctx = self.context

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{group.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{group.help}```" if group.help else "No help yet..."

        if group.commands:
            embed.add_field(
                name="Commands",
                value=human_join(
                    [f"**`{command.name}`**" for command in group.commands], final="and"
                ),
                inline=False,
            )

        if group.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in group.aliases], final="and"
                ),
                inline=False,
            )

        if group.cooldown:
            cd = group.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        await ctx.send(embed=embed, view=CommandView(ctx, group.cog))

    async def send_cog_help(self, cog: commands.Cog):
        ctx = self.context
        bot = ctx.bot

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{cog.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{cog.description}```" or "No help yet..."

        commands = await self.filter_commands(cog.get_commands())

        if commands:
            embed.add_field(
                name="Commands",
                value=human_join(
                    [f"**`{command.name}`**" for command in cog.get_commands()],
                    final="and",
                ),
                inline=False,
            )

        if hasattr(cog, "aliases"):
            embed.add_field(name="Aliases", value=human_join([f"**`{alias}`**" for alias in cog.aliases], final="and"), inline=False)  # type: ignore

        filtered_cogs = [
            cog
            for _, cog in bot.cogs.items()
            if len(await self.filter_commands(cog.get_commands())) > 0
        ]

        await ctx.send(embed=embed, view=CogView(ctx, filtered_cogs))

    async def send_error_message(self, error: commands.CommandError):
        ctx = self.context
        bot = ctx.bot

        if bot.user is None:
            return

        pattern = re.compile(r'No command called "(?P<name>[a-zA-Z0-9]{1,25})" found.')
        results = pattern.match(str(error))

        if results:
            error_command_name = results.group("name").lower()

            for name, cog in bot.cogs.items():
                if error_command_name == cog.qualified_name.lower():
                    await self.send_cog_help(cog)
                    return

                if hasattr(cog, "aliases"):
                    if error_command_name in cog.aliases:  # type: ignore
                        _cog = bot.get_cog(cog.qualified_name)

                        if _cog is None:
                            continue

                        await self.send_cog_help(_cog)
                        return

        else:
            await ctx.send(str(error))


class HelpCog(commands.Cog, name="help_cog"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._og_help = commands.DefaultHelpCommand()
        self.bot.help_command = MyHelp()

    async def cog_unload(self):
        self.bot.help_command = self._og_help

    async def cog_load(self) -> None:
        self.bot.help_command = MyHelp()
