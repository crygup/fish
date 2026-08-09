from __future__ import annotations

import re
import types
from textwrap import dedent
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, TypeAlias, Union

import discord
from discord.ext import commands

from core import Cog
from extensions.context import Context
from utils import AuthorView, human_join

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

CogMapping: TypeAlias = Mapping[Optional[Cog], List[commands.Command[Any, ..., Any]]]


def command_usage(ctx: Context, command: commands.Command[Any, ..., Any]) -> str:
    """Build the prefix usage shown in command help."""
    custom_usage = command.extras.get("usage")
    signature = custom_usage if isinstance(custom_usage, str) else command.signature
    suffix = f" {signature}" if signature else ""
    return f"{ctx.get_prefix}{command.qualified_name}{suffix}"


def chunk_usage_lines(lines: list[str], limit: int = 1016) -> list[str]:
    """Split usage lines so each code block fits in a Discord embed field."""
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for line in lines:
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0

        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)

    if current:
        chunks.append("\n".join(current))

    return chunks


def make_command_embed(
    ctx: Context,
    command: Union[
        commands.Command[Cog, ..., Any], commands.Group[Cog, ..., commands.Command]
    ],
):
    bot = ctx.bot
    embed = discord.Embed(
        color=bot.embedcolor,
        title=command.name.capitalize(),
        description=command.description or command.help or "",
    )
    embed.add_field(
        name="Usage",
        value=f"`{command_usage(ctx, command)}`",
        inline=False,
    )
    if command.aliases:
        embed.add_field(
            name="Aliases",
            value=human_join([f"`{a}`" for a in command.aliases]),
            inline=False,
        )

    if isinstance(command, commands.Group):
        lines = [command_usage(ctx, child) for child in command.commands]
        for index, text in enumerate(chunk_usage_lines(lines)):
            name = "Subcommands" if index == 0 else "Subcommands (continued)"
            embed.add_field(name=name, value=f"```{text}```", inline=False)

    embed.set_footer(text="\u2800" * 47)

    return embed


class HelpCommand(commands.HelpCommand):
    context: Context

    # fish help
    async def send_bot_help(self, mapping: CogMapping):
        ctx = self.context
        bot = ctx.bot
        embed = discord.Embed(color=bot.embedcolor)

        field_1 = """
        My default prefix is `fish` however mentioning me also works.
        
        Commands help will be displayed as `fish avatars [user]`.
        
        You will see a few symbols around an argument while looking for help.

        **[]** means that it is optional.
        **<>** means that it is required.
        
        However, do not actually include those symbols in the command.

        For reference this is how you would run the avatars command:
        `fish avatars` or
        `fish avatars @fishie#3245`."""

        field_2 = """
        To get help with a command or category you can run the command: `fish help <command/category>`.
        
        Or optionally you can use the select menu down below to browse through them all."""

        field_3 = """
        [Support Server](https://discord.com/invite/rM9u4MRFBE)
        [Terms of Service](https://github.com/fishie-bot/fishie-bot/blob/main/Terms%20of%20Service.md)
        [Privacy Policy](https://github.com/fishie-bot/fishie-bot/blob/main/Privacy%20Policy.md)
        [Donate](https://ko-fi.com/crygup)
        """

        embed.add_field(name="Command usage", value=dedent(field_1))
        embed.add_field(name="Getting started", value=dedent(field_2))
        embed.add_field(name="Links", value=dedent(field_3), inline=False)
        await ctx.send(
            embed=embed,
            view=CogHelpView(self.context, [cog for cog, _ in mapping.items() if cog]),
        )

    # fish help <command>
    async def send_command_help(self, command: commands.Command[Cog, ..., Any]):
        ctx = self.context
        embed = make_command_embed(ctx, command)
        if command.cog is None:
            await ctx.send(embed=embed)
            return

        cmds = await self.filter_commands(command.cog.get_commands(), sort=True)
        all_cogs = [c for _, c in ctx.bot.cogs.items() if c]
        view = CogHelpView(ctx, all_cogs, selected_cog=command.cog)
        _add_command_help_dropdowns(view, ctx, cmds)
        await ctx.send(embed=embed, view=view)

    async def send_group_help(self, group: commands.Group[Cog, ..., commands.Command]):
        ctx = self.context
        embed = make_command_embed(ctx, group)
        cmds = await self.filter_commands(group.commands, sort=True)
        if group.cog:
            all_cogs = [c for _, c in ctx.bot.cogs.items() if c]
            view = CogHelpView(ctx, all_cogs, selected_cog=group.cog)
            _add_command_help_dropdowns(view, ctx, cmds)
        else:
            view = CommandHelpView(ctx, cmds)
        await ctx.send(embed=embed, view=view)

    # fish help <cog>
    async def send_cog_help(self, cog: Cog):
        ctx = self.context
        bot = ctx.bot
        cmds = await self.filter_commands(cog.get_commands(), sort=True)

        embed = discord.Embed(
            title=f"{cog.emoji} {cog.qualified_name}",
            color=bot.embedcolor,
            description=cog.description,
        )
        if cog.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join([f"`{a}`" for a in cog.aliases], final="and"),
                inline=False,
            )

        embed.add_field(
            name="Commands",
            value=(
                human_join([f"`{c.name}`" for c in cmds], final="and")
                if cmds
                else "No commands"
            ),
        )

        filtered = await self.filter_commands(
            [c for c in cog.get_commands()], sort=True
        )
        view = CogHelpView(self.context, [c for _, c in bot.cogs.items()])
        if filtered:
            _add_command_help_dropdowns(view, ctx, filtered)
        await ctx.send(embed=embed, view=view)

    async def send_error_message(self, error: commands.CommandError):
        ctx = self.context
        bot = ctx.bot

        pattern = re.compile(r'No command called "(?P<name>[a-zA-Z0-9]{1,25})" found.')
        results = pattern.match(str(error))

        if not bool(results):
            raise commands.BadArgument(str(error))

        match = results.group("name").lower()

        for _, cog in bot.cogs.items():
            if match == cog.qualified_name.lower() or match in [
                a.lower() for a in cog.aliases
            ]:
                return await self.send_cog_help(cog)


TRUNC = 100


def _t(s: str) -> str:
    """Truncate to Discord's 100-char limit for SelectOption fields."""
    return s[:TRUNC]


def _add_command_help_dropdowns(
    view: discord.ui.View, ctx: Context, cmds: List[commands.Command[Cog, ..., Any]]
):
    for cmd_list in (cmds[i : i + 25] for i in range(0, len(cmds), 25)):
        view.add_item(CommandHelpDropdown(ctx, cmd_list))


class CogHelpDropdown(discord.ui.Select):
    view: CogHelpView

    def __init__(self, ctx: Context, cogs: List[Cog], selected_cog: Cog | None = None):
        self.cogs = cogs
        self.ctx = ctx

        options = []

        for cog in cogs:
            if cog is None or cog and cog.hidden:
                continue

            options.append(
                discord.SelectOption(
                    label=_t(cog.qualified_name),
                    emoji=cog.emoji,
                    description=_t((cog.description or "").split("\n")[0]),
                    default=cog is selected_cog,
                )
            )

        super().__init__(
            placeholder="Choose a category", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        bot = ctx.bot
        cog = bot.get_cog(self.values[0])

        if cog is None:
            raise commands.BadArgument("Somehow I could not find that category.")

        for option in self.options:
            option.default = option.value == _t(cog.qualified_name)

        cmds = cog.get_commands()

        embed = discord.Embed(
            title=f"{cog.emoji} {cog.qualified_name}",
            color=bot.embedcolor,
            description=cog.description,
        )
        embed.add_field(
            name="Commands",
            value=human_join([f"`{c.name}`" for c in cmds], final="and"),
        )

        if not interaction.message:
            raise commands.BadArgument("Somehow no message was found.")

        while len(self.view.children) > 1:
            self.view.remove_item(self.view.children[-1])

        _add_command_help_dropdowns(self.view, ctx, cmds)

        await interaction.message.edit(embed=embed, view=self.view)
        await interaction.response.defer()


class CogHelpView(AuthorView):
    def __init__(self, ctx: Context, cogs: List[Cog], selected_cog: Cog | None = None):
        super().__init__(ctx)
        self.add_item(CogHelpDropdown(ctx, cogs, selected_cog=selected_cog))


class CommandHelpDropdown(discord.ui.Select):
    view: CommandHelpView

    def __init__(self, ctx: Context, cmds: List[commands.Command[Cog, ..., Any]]):
        self.cmds = cmds
        self.ctx = ctx

        options = []

        for cmd in cmds:
            if cmd.hidden:
                continue
            desc = _t((cmd.help or cmd.description or "").split("\n")[0])
            options.append(
                discord.SelectOption(
                    label=_t(cmd.name.capitalize()),
                    value=_t(cmd.name.lower()),
                    description=desc,
                )
            )

        if not options:
            options.append(
                discord.SelectOption(label="No commands to show.", value="none")
            )
        super().__init__(
            placeholder="Choose a command", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = self.ctx
        command: commands.Command[Cog, ..., Any] | None = None
        for cmd in self.cmds:
            if cmd.name.lower() == self.values[0]:
                command = cmd
                break

        if command is None:
            raise commands.BadArgument("Could not find that command somehow.")

        embed = make_command_embed(ctx, command)

        if not interaction.message:
            raise commands.BadArgument("Somehow no message was found.")
        view_ref = self.view
        if isinstance(view_ref, CogHelpView):
            if isinstance(command, commands.Group):
                try:
                    raw = list(command.commands)
                except Exception:
                    raw = []
                subcmds = [c for c in raw if not c.hidden]
                if subcmds and view_ref is not None:
                    while len(view_ref.children) > 1:
                        view_ref.remove_item(view_ref.children[-1])
                    _add_command_help_dropdowns(view_ref, ctx, subcmds)
            await interaction.message.edit(embed=embed, view=view_ref)
            return

        if isinstance(command, commands.Group):
            try:
                raw = list(command.commands)
            except Exception:
                raw = []
            subcmds = [c for c in raw if not c.hidden]
            if subcmds and command.cog:
                all_cogs = [c for _, c in ctx.bot.cogs.items() if c]
                view_ref = CogHelpView(ctx, all_cogs)
                _add_command_help_dropdowns(view_ref, ctx, subcmds)
        await interaction.message.edit(embed=embed, view=view_ref)


class CommandHelpView(AuthorView):
    def __init__(self, ctx: Context, cmds: List[commands.Command[Cog, ..., Any]]):
        super().__init__(ctx)
        if not cmds:
            return
        _add_command_help_dropdowns(self, ctx, cmds)


class Help(Cog):
    emoji = discord.PartialEmoji(name="\U00002753")
    hidden = True

    def __init__(self, bot: Fishie) -> None:
        super().__init__()
        self.bot = bot
        self.old_help = commands.DefaultHelpCommand()
        self.help_command = HelpCommand()

    async def cog_unload(self):
        self.bot.help_command = self.old_help

    async def cog_load(self) -> None:
        self.bot.help_command = HelpCommand()


async def setup(bot: Fishie):
    await bot.add_cog(Help(bot))
