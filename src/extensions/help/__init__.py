from __future__ import annotations

import random
import re
import types
from textwrap import dedent
from typing import (
    TYPE_CHECKING,
    Any,
    List,
    Mapping,
    Optional,
    TypeAlias,
    Union,
    cast,
)

import discord
from discord.ext import commands

from core import Cog
from extensions.context import Context
from utils import (
    AuthorView,
    LayoutPageModal,
    build_layout_pagination_row,
    human_join,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

CogMapping: TypeAlias = Mapping[Optional[Cog], List[commands.Command[Any, ..., Any]]]


def command_usage(ctx: Context, command: commands.Command[Any, ..., Any]) -> str:
    """Build the prefix usage shown in command help."""
    custom_usage = command.extras.get("usage")
    signature = custom_usage if isinstance(custom_usage, str) else command.signature
    if isinstance(custom_usage, str):
        signature = _collapse_custom_usage_flags(command, custom_usage)
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


def _is_effect_help_command(
    command: commands.Command[Any, ..., Any],
) -> bool:
    """Keep the large effect command groups out of the flags section."""
    root_name = command.qualified_name.casefold().split(" ", 1)[0]
    return root_name == "effect" or root_name.startswith("effect-")


def _flag_converter_for(
    command: commands.Command[Any, ..., Any],
) -> type[commands.FlagConverter] | None:
    """Return the FlagConverter annotation used by a command, if any."""
    for parameter in command.clean_params.values():
        annotation = getattr(parameter, "annotation", None)
        if isinstance(annotation, type) and issubclass(
            annotation, commands.FlagConverter
        ):
            return annotation
    return None


def _command_flag_lines(command: commands.Command[Any, ..., Any]) -> list[str]:
    """Return readable flag lines for a command's detailed help page.

    Most commands keep their flag descriptions in the help text, while newer
    FlagConverter commands may only have metadata on the converter itself.
    Prefer the author's help text when it exists and fall back to that
    metadata so both styles stay documented automatically.
    """
    if _is_effect_help_command(command):
        return []

    configured = command.extras.get("flags")
    if isinstance(configured, str):
        configured_lines = [line.strip() for line in configured.splitlines()]
        if configured_lines:
            return configured_lines
    if isinstance(configured, (list, tuple)):
        configured_lines = [
            str(line).strip() for line in configured if str(line).strip()
        ]
        if configured_lines:
            return configured_lines

    help_lines: list[str] = []
    numeric_lines: list[str] = []
    for raw_line in (command.help or "").splitlines():
        line = raw_line.strip()
        if line.startswith("-# Numeric limits:"):
            numeric_lines.extend(
                item.strip().rstrip(".")
                for item in line.removeprefix("-# Numeric limits:").strip().split(";")
                if item.strip().startswith("-")
            )
            continue
        if line.startswith("-#"):
            line = line[2:].strip()
            if line.startswith("-"):
                help_lines.append(line)
    configured_ranges = command.extras.get("flag_ranges")
    if isinstance(configured_ranges, Mapping):
        ranges = {
            str(name).casefold(): str(value).strip()
            for name, value in configured_ranges.items()
        }
        merged: list[str] = []
        for line in help_lines:
            parts = line.split(None, 1)
            name = parts[0].lstrip("-").casefold()
            detail = ranges.pop(name, None)
            if detail:
                remainder = parts[1] if len(parts) > 1 else ""
                limit_text = f"(limits: {detail})"
                if remainder.strip() == detail:
                    line = f"{parts[0]} {limit_text}"
                elif limit_text not in remainder:
                    line = (
                        f"{parts[0]} {remainder.strip()} {limit_text}"
                        if remainder.strip()
                        else f"{parts[0]} {limit_text}"
                    )
            merged.append(line)
        help_lines = merged
        for name, detail in ranges.items():
            help_lines.append(f"-{name.lstrip('-')} (limits: {detail})")

    if help_lines or numeric_lines:
        documented_names = {
            line.split(None, 1)[0].lstrip("-").casefold() for line in help_lines
        }
        help_lines.extend(
            line
            for line in numeric_lines
            if line.split(None, 1)[0].lstrip("-").casefold() not in documented_names
        )
        return help_lines

    converter = _flag_converter_for(command)
    if converter is None:
        return []

    prefix = str(getattr(converter, "__commands_flag_prefix__", "-"))
    lines: list[str] = []
    for name, flag in getattr(converter, "__commands_flags__", {}).items():
        flag_name = str(getattr(flag, "name", name))
        aliases = [str(alias) for alias in getattr(flag, "aliases", ())]
        annotation = getattr(flag, "annotation", None)
        syntax = f"{prefix}{flag_name}"
        if annotation is not bool:
            syntax += " <value>"
        if aliases:
            syntax += (
                f" (aliases: {', '.join(f'{prefix}{alias}' for alias in aliases)})"
            )
        description = str(getattr(flag, "description", "") or "").strip()
        lines.append(f"{syntax}  {description}".rstrip())
    return lines


def _collapse_custom_usage_flags(
    command: commands.Command[Any, ..., Any], usage: str
) -> str:
    """Replace detailed custom flag syntax with one compact placeholder."""
    if (
        not usage
        or _is_effect_help_command(command)
        or not _command_flag_lines(command)
    ):
        return usage

    flag_token = r"(?<!\w)-[A-Za-z][\w-]*"
    without_placeholder = re.sub(r"\s*\[flags\.\.\.\]", "", usage)
    if not re.search(flag_token, without_placeholder):
        return usage
    usage = without_placeholder
    bracket_pattern = re.compile(rf"\[[^\]]*{flag_token}[^\]]*\]")
    replaced = False

    def replace_bracket(_: re.Match[str]) -> str:
        nonlocal replaced
        if replaced:
            return ""
        replaced = True
        return "[flags...]"

    collapsed, replacements = bracket_pattern.subn(replace_bracket, usage)
    if replacements:
        usage = collapsed

    # Required flags such as ``-text`` and ``-second`` are not wrapped in
    # brackets. Collapse those too, while preserving the positional arguments
    # that appear before the first flag.
    match = re.search(flag_token, usage)
    if match:
        prefix = usage[: match.start()].rstrip()
        if prefix.endswith("["):
            prefix = prefix[:-1].rstrip()
        return f"{prefix} [flags...]" if prefix else "[flags...]"

    if replacements:
        return re.sub(r"\s{2,}", " ", usage).strip()

    # A few usages include a required flag outside brackets, such as
    # ``<media> -audio <media>``. Keep the positional arguments and collapse
    # the flag portion without touching another alternative separated by ``|``.
    parts = re.split(r"(\s+\|\s+)", usage)
    for index, part in enumerate(parts):
        if re.fullmatch(r"\s+\|\s+", part or ""):
            continue
        match = re.search(flag_token, part)
        if match:
            prefix = part[: match.start()].rstrip()
            parts[index] = f"{prefix} [flags...]" if prefix else "[flags...]"
    return "".join(parts)


def _command_description(command: commands.Command[Any, ..., Any]) -> str:
    """Return command prose without flag documentation lines.

    Flag details are rendered in their own code block on detailed help pages.
    Effect groups are intentionally excluded from that block, so their existing
    inline flag documentation remains in the description.
    """
    description = command.description or command.help or ""
    if not description or not _command_flag_lines(command):
        return description

    lines: list[str] = []
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if line.startswith("-# Numeric limits:"):
            continue
        if line.startswith("-#") and line[2:].strip().startswith("-"):
            continue
        lines.append(raw_line.rstrip())
    return "\n".join(lines).strip()


def _text_display_codeblock_limit(title: str) -> int:
    """Return the payload budget for a code block inside a TextDisplay."""
    # The title, newline, and opening/closing triple-backtick fences count
    # toward TextDisplay's 4,000-character limit.
    overhead = len(f"**{title}**\n``````")
    return 4_000 - overhead


def _flag_codeblocks(
    command: commands.Command[Any, ..., Any], *, limit: int = 1_016
) -> list[str]:
    """Build Discord-safe code-block contents for a command's flags."""
    raw_lines = _command_flag_lines(command)
    parsed_lines: list[tuple[str, str]] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        parts = re.split(r"\s{2,}", stripped, maxsplit=1)
        if len(parts) == 2:
            syntax, description = parts
        else:
            match = re.match(r"^(?P<flag>\S+)(?:\s+(?P<description>.*))?$", stripped)
            if match is None:
                continue
            syntax = match.group("flag")
            description = match.group("description") or ""
        parsed_lines.append((syntax, description))

    if parsed_lines:
        flag_width = max(21, max(len(flag) + 2 for flag, _ in parsed_lines))
        lines = [
            f"{flag:<{flag_width}}{description}".rstrip()
            for flag, description in parsed_lines
        ]
    else:
        lines = raw_lines
    lines = [line.replace("```", "'''") for line in lines]
    return chunk_usage_lines(lines, limit=limit)


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
        description=_command_description(command),
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

    for index, text in enumerate(_flag_codeblocks(command)):
        name = "Flags" if index == 0 else "Flags (continued)"
        embed.add_field(name=name, value=f"```{text}```", inline=False)

    if isinstance(command, commands.Group):
        lines = [command_usage(ctx, child) for child in command.commands]
        for index, text in enumerate(chunk_usage_lines(lines)):
            name = "Subcommands" if index == 0 else "Subcommands (continued)"
            embed.add_field(name=name, value=f"```{text}```", inline=False)

    embed.set_footer(text="\u2800" * 47)

    return embed


class LegacyHelpCommand(commands.HelpCommand):
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


HELP_PAGE_SIZE = 5


def _help_text(value: object, limit: int = 700) -> str:
    """Keep help text readable without allowing accidental mentions."""
    text = discord.utils.escape_mentions(str(value or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text or "No description available."


def _short_command_help(command: commands.Command[Any, ..., Any]) -> str:
    """Return the first useful line of a command's help text."""
    description = command.description or command.help or ""
    return _help_text(str(description).splitlines()[0] if description else "", 400)


def _required_permissions(command: commands.Command[Any, ..., Any]) -> list[str]:
    """Extract user permissions declared by the command's permission checks."""
    permission_flags = set(discord.Permissions.VALID_FLAGS)
    permissions: list[str] = []
    checks = list(command.checks)
    app_command = getattr(command, "app_command", None)
    checks.extend(getattr(app_command, "checks", ()))

    for check in checks:
        qualified_name = getattr(check, "__qualname__", "")
        if "bot_has_" in qualified_name:
            continue
        for cell in getattr(check, "__closure__", ()) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, dict):
                values = value.items()
            elif isinstance(value, str) and value in permission_flags:
                values = ((value, True),)
            else:
                continue
            for name, required in values:
                if required and name in permission_flags and name not in permissions:
                    permissions.append(name)
    return permissions


def _permission_label(permission: str) -> str:
    """Return the friendly Discord label for a permission flag."""
    labels = {
        "manage_guild": "Manage Server",
        "manage_emojis": "Manage Expressions",
        "manage_emojis_and_stickers": "Manage Expressions",
        "moderate_members": "Timeout Members",
        "view_audit_log": "View Audit Log",
    }
    return labels.get(permission, permission.replace("_", " ").title())


class HelpCategorySelect(discord.ui.Select):
    """Category-only selector shared by every Components V2 help page."""

    def __init__(self, view: "HelpLayoutView") -> None:
        self.help_view = view
        options = [
            discord.SelectOption(
                label="Index",
                value="__index__",
                description="Return to the help overview.",
                emoji="🏠",
                default=view.selected_cog is None,
            )
        ]
        for cog in view.categories:
            options.append(
                discord.SelectOption(
                    label=_t(cog.qualified_name),
                    value=_t(cog.qualified_name),
                    emoji=cog.emoji,
                    description=_t(
                        _help_text(str(cog.description or "").splitlines()[0], 100)
                    ),
                    default=cog is view.selected_cog,
                )
            )
        super().__init__(
            placeholder="Choose a category",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.select_category(interaction, self.values[0])


class HelpCommandSelect(discord.ui.Select):
    """Select one of the commands shown on the current category page."""

    def __init__(self, view: "HelpLayoutView") -> None:
        self.help_view = view
        # While a command is open, ``page`` belongs to that group's
        # subcommands. Keep the category command selector on the page that
        # opened the detail view instead of making it disappear or pointing
        # at the wrong slice.
        category_page = view._category_page if view.detail is not None else view.page
        start = category_page * HELP_PAGE_SIZE
        page_commands = view.commands_list[start : start + HELP_PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=_t(discord.utils.escape_markdown(command.qualified_name)),
                value=str(index),
                description=_t(_short_command_help(command)),
            )
            for index, command in enumerate(page_commands)
        ]
        super().__init__(
            placeholder="Choose a command",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.select_command(interaction, int(self.values[0]))


class HelpSubcommandSelect(discord.ui.Select):
    """Select one of the subcommands shown on the current group page."""

    def __init__(self, view: "HelpLayoutView") -> None:
        self.help_view = view
        subcommands = view._detail_commands()
        start = view.page * HELP_PAGE_SIZE
        page_commands = subcommands[start : start + HELP_PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=_t(discord.utils.escape_markdown(command.qualified_name)),
                value=str(index),
                description=_t(_short_command_help(command)),
            )
            for index, command in enumerate(page_commands)
        ]
        super().__init__(
            placeholder="Choose a subcommand",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.select_subcommand(interaction, int(self.values[0]))


class HelpBackButton(discord.ui.Button):
    """Return from command details to the category command list."""

    def __init__(self, view: "HelpLayoutView") -> None:
        self.help_view = view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="\u21a9",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.back_to_category(interaction)


class HelpLayoutView(discord.ui.LayoutView):
    """Components V2 help overview and five-command category paginator."""

    def __init__(
        self,
        ctx: Context,
        categories: list[Cog],
        *,
        selected_cog: Cog | None = None,
        commands_list: list[commands.Command[Any, ..., Any]] | None = None,
        detail: commands.Command[Any, ..., Any] | None = None,
        help_command: "ComponentsHelpCommand | None" = None,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.categories = categories
        self.selected_cog = selected_cog
        self.commands_list = commands_list or []
        self.detail = detail
        self.help_command = help_command
        self.page = 0
        # Keep the category page that opened a command so the back button
        # returns to the same list instead of resetting to page one.
        self._category_page = 0
        self.message: discord.Message | None = None
        self._navigation_buttons: tuple[discord.ui.Button, ...] = ()
        self._render()

    @property
    def page_count(self) -> int:
        if self.detail is not None:
            detail_commands = self._detail_commands()
            if not detail_commands:
                return 1
            return max(
                1,
                (len(detail_commands) + HELP_PAGE_SIZE - 1) // HELP_PAGE_SIZE,
            )
        if self.selected_cog is None:
            return 1
        return max(1, (len(self.commands_list) + HELP_PAGE_SIZE - 1) // HELP_PAGE_SIZE)

    def _detail_commands(self) -> list[commands.Command[Any, ..., Any]]:
        if not isinstance(self.detail, commands.Group):
            return []
        return [child for child in self.detail.commands if not child.hidden]

    def _index_items(self) -> list[discord.ui.Item[Any]]:
        command_usage_text = dedent("""
            My default prefix is `fish` however mentioning me also works.

            Commands help will be displayed as `fish avatars [user]`.

            You will see a few symbols around an argument while looking for help.

            **[]** means that it is optional.
            **<>** means that it is required.

            However, do not actually include those symbols in the command.

            For reference this is how you would run the avatars command:
            `fish avatars` or
            `fish avatars @fishie#3245`.
            """).strip()
        return [
            discord.ui.TextDisplay("## Fishie Help"),
            discord.ui.TextDisplay(f"### Command usage\n{command_usage_text}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "[Support Server](https://discord.com/invite/rM9u4MRFBE) · "
                "[Terms of Service](https://github.com/fishie-bot/fishie-bot/blob/main/Terms%20of%20Service.md) · "
                "[Privacy Policy](https://github.com/fishie-bot/fishie-bot/blob/main/Privacy%20Policy.md) · "
                "[Donate](https://ko-fi.com/crygup)"
            ),
        ]

    def _category_items(self) -> list[discord.ui.Item[Any]]:
        assert self.selected_cog is not None
        title = (
            f"## {self.selected_cog.emoji} "
            f"{discord.utils.escape_markdown(self.selected_cog.qualified_name)}"
        )
        description = _help_text(self.selected_cog.description, 600)
        start = self.page * HELP_PAGE_SIZE
        page_commands = self.commands_list[start : start + HELP_PAGE_SIZE]
        items: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(title),
            discord.ui.TextDisplay(description),
            discord.ui.Separator(),
        ]
        if page_commands:
            for index, command in enumerate(page_commands):
                name = discord.utils.escape_markdown(command.qualified_name)
                items.append(
                    discord.ui.TextDisplay(
                        f"### `{name}`\n{_short_command_help(command)}"
                    )
                )
                if index < len(page_commands) - 1:
                    items.append(discord.ui.Separator())
            items.append(discord.ui.Separator())
            items.append(
                discord.ui.TextDisplay(
                    f"-# Page {self.page + 1}/{self.page_count} · "
                    f"Use `{self.ctx.clean_prefix}help <command>` for detailed help."
                )
            )
        else:
            items.append(
                discord.ui.TextDisplay("No commands are available in this category.")
            )
        return items

    def _detail_items(self) -> list[discord.ui.Item[Any]]:
        assert self.detail is not None
        command = self.detail
        name = discord.utils.escape_markdown(command.qualified_name)
        items: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(f"## {name}"),
            discord.ui.TextDisplay(_help_text(_command_description(command), 1_500)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**Usage**\n`{command_usage(self.ctx, command)}`"),
        ]
        permissions = _required_permissions(command)
        if permissions:
            items.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        "**Required permissions**\n"
                        + "\n".join(
                            f"`{_permission_label(permission)}`"
                            for permission in permissions
                        )
                    ),
                ]
            )
        if command.aliases:
            items.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        f"**Aliases**\n{human_join([f'`{a}`' for a in command.aliases])}"
                    ),
                ]
            )
        for index, text in enumerate(
            _flag_codeblocks(
                command,
                limit=_text_display_codeblock_limit("Flags (continued)"),
            )
        ):
            items.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        f"**{'Flags' if index == 0 else 'Flags (continued)'}**\n"
                        f"```{text}```"
                    ),
                ]
            )
        if isinstance(command, commands.Group):
            children = self._detail_commands()
            if children:
                start = self.page * HELP_PAGE_SIZE
                page_children = children[start : start + HELP_PAGE_SIZE]
                lines = [command_usage(self.ctx, child) for child in page_children]
                subcommand_blocks = chunk_usage_lines(
                    lines,
                    limit=_text_display_codeblock_limit("Subcommands (continued)"),
                )
                for index, text in enumerate(subcommand_blocks):
                    items.extend(
                        [
                            discord.ui.Separator(),
                            discord.ui.TextDisplay(
                                f"**{'Subcommands' if index == 0 else 'Subcommands (continued)'}**\n"
                                f"```{text}```"
                            ),
                        ]
                    )
                items.extend(
                    [
                        discord.ui.Separator(),
                        discord.ui.TextDisplay(
                            f"-# Page {self.page + 1}/{self.page_count}"
                        ),
                    ]
                )
        return items

    def _render(self) -> None:
        self.clear_items()
        if self.detail is not None:
            items = self._detail_items()
        elif self.selected_cog is None:
            items = self._index_items()
        else:
            items = self._category_items()
        self.add_item(
            discord.ui.Container(*items, accent_color=self.ctx.bot.embedcolor)
        )
        self.add_item(discord.ui.ActionRow(HelpCategorySelect(self)))
        if self.detail is not None:
            if isinstance(self.detail, commands.Group) and self._detail_commands():
                self.add_item(discord.ui.ActionRow(HelpSubcommandSelect(self)))
            elif self.selected_cog is not None and self.commands_list:
                self.add_item(discord.ui.ActionRow(HelpCommandSelect(self)))
        elif self.selected_cog is not None:
            start = self.page * HELP_PAGE_SIZE
            page_commands = self.commands_list[start : start + HELP_PAGE_SIZE]
            if page_commands:
                self.add_item(discord.ui.ActionRow(HelpCommandSelect(self)))
        should_paginate = self.page_count > 1 and (
            self.detail is not None or self.selected_cog is not None
        )
        if should_paginate:
            row, buttons = build_layout_pagination_row(
                page=self.page,
                page_count=self.page_count,
                previous=self._previous_page,
                next_page=self._next_page,
                shuffle=self._shuffle_page,
                go_to_page=self._open_page_modal,
                trash=self._delete,
            )
            self._navigation_buttons = buttons
            self.add_item(row)
        else:
            self._navigation_buttons = ()
        if self.detail is not None:
            self.add_item(discord.ui.ActionRow(HelpBackButton(self)))

    async def start(self) -> None:
        self.message = await self.ctx.send(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def back_to_category(self, interaction: discord.Interaction) -> None:
        """Return from command details to its category or the help index."""
        self.detail = None
        if self.selected_cog is None:
            self.commands_list = []
            self.page = 0
        else:
            self.page = min(self._category_page, self.page_count - 1)
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def select_category(
        self, interaction: discord.Interaction, value: str
    ) -> None:
        if value == "__index__":
            self.selected_cog = None
            self.commands_list = []
            self.detail = None
        else:
            cog = self.ctx.bot.get_cog(value)
            if cog is None or cog.hidden:
                raise commands.BadArgument("That help category is not available.")
            self.selected_cog = cog
            self.detail = None
            if self.help_command is not None:
                self.commands_list = await self.help_command.filter_commands(
                    cog.get_commands(), sort=True
                )
            else:
                self.commands_list = [
                    command for command in cog.get_commands() if not command.hidden
                ]
        self.page = 0
        self._category_page = 0
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def select_command(
        self, interaction: discord.Interaction, index: int
    ) -> None:
        category_page = self._category_page if self.detail is not None else self.page
        start = category_page * HELP_PAGE_SIZE
        page_commands = self.commands_list[start : start + HELP_PAGE_SIZE]
        if index < 0 or index >= len(page_commands):
            await interaction.response.send_message(
                "That command is no longer available on this page.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self._category_page = category_page
        self.detail = page_commands[index]
        self.page = 0
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def select_subcommand(
        self, interaction: discord.Interaction, index: int
    ) -> None:
        subcommands = self._detail_commands()
        start = self.page * HELP_PAGE_SIZE
        page_commands = subcommands[start : start + HELP_PAGE_SIZE]
        if index < 0 or index >= len(page_commands):
            await interaction.response.send_message(
                "That subcommand is no longer available on this page.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.detail = page_commands[index]
        self.page = 0
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_layout_page(
        self, interaction: discord.Interaction, page: int
    ) -> None:
        if page < 0 or page >= self.page_count:
            await interaction.response.send_message(
                "That page does not exist.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.page = page
        self._render()
        if interaction.response.is_done():
            if self.message:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        if self.page_count:
            await self._set_layout_page(interaction, (self.page - 1) % self.page_count)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if self.page_count:
            await self._set_layout_page(interaction, (self.page + 1) % self.page_count)

    async def _shuffle_page(self, interaction: discord.Interaction) -> None:
        choices = [page for page in range(self.page_count) if page != self.page]
        if not choices:
            await interaction.response.send_message(
                "There are no other pages to choose from.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._set_layout_page(interaction, random.choice(choices))

    async def _open_page_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LayoutPageModal(self, self.page_count))

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this help menu can use its controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[Any] | None = None,
    ) -> None:
        self.ctx.bot.logger.error(
            "Help menu interaction failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            await self.ctx.bot.log_error(
                error,
                context=self.ctx,
                interaction=interaction,
            )
        except Exception:
            self.ctx.bot.logger.exception("Could not send help menu error report")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "The help menu could not be updated.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    "The help menu could not be updated.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.DiscordException:
            self.ctx.bot.logger.exception("Could not acknowledge help menu error")

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.ActionRow):
                for item in child.children:
                    if hasattr(item, "disabled"):
                        cast(Any, item).disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass


class ComponentsHelpCommand(commands.HelpCommand):
    """Components V2 help command used by ``fish help``."""

    @staticmethod
    def _categories(bot: Any) -> list[Cog]:
        return [
            cog for cog in bot.cogs.values() if isinstance(cog, Cog) and not cog.hidden
        ]

    async def send_bot_help(self, mapping: CogMapping) -> None:
        del mapping
        ctx = cast(Context, self.context)
        view = HelpLayoutView(
            ctx,
            self._categories(ctx.bot),
            help_command=self,
        )
        await view.start()

    async def send_cog_help(self, cog: Cog) -> None:
        ctx = cast(Context, self.context)
        commands_list = await self.filter_commands(cog.get_commands(), sort=True)
        view = HelpLayoutView(
            ctx,
            self._categories(ctx.bot),
            selected_cog=cog,
            commands_list=commands_list,
            help_command=self,
        )
        await view.start()

    async def send_command_help(self, command: commands.Command[Any, ..., Any]) -> None:
        ctx = cast(Context, self.context)
        selected_cog = command.cog if isinstance(command.cog, Cog) else None
        commands_list: list[commands.Command[Any, ..., Any]] = []
        if selected_cog is not None:
            commands_list = await self.filter_commands(
                selected_cog.get_commands(), sort=True
            )
        view = HelpLayoutView(
            ctx,
            self._categories(ctx.bot),
            selected_cog=selected_cog,
            commands_list=commands_list,
            detail=command,
            help_command=self,
        )
        if selected_cog is not None:
            try:
                command_index = commands_list.index(command)
            except ValueError:
                command_index = 0
            view._category_page = command_index // HELP_PAGE_SIZE
            view._render()
        await view.start()

    async def send_group_help(self, group: commands.Group[Any, ..., Any]) -> None:
        await self.send_command_help(group)

    async def send_error_message(self, error: commands.CommandError) -> None:
        match = re.search(r'No command called "(?P<name>[^"]+)" found', str(error))
        name = match.group("name").casefold() if match else ""
        if name in {"help", "h"}:
            await self.send_bot_help({})
            return
        if name:
            for cog in self._categories(self.context.bot):
                if name in {
                    cog.qualified_name.casefold(),
                    *(a.casefold() for a in cog.aliases),
                }:
                    await self.send_cog_help(cog)
                    return
        raise commands.BadArgument(str(error))


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
        self.old_help = LegacyHelpCommand()
        self.help_command = ComponentsHelpCommand()

    @commands.command(name="oldhelp", hidden=True)
    async def oldhelp(self, ctx: Context, *, command: Optional[str] = None) -> None:
        """Show the legacy help layout."""
        legacy = self.old_help.copy()
        legacy.context = ctx
        await legacy.command_callback(ctx, command=command)

    async def cog_unload(self):
        self.bot.help_command = self.old_help

    async def cog_load(self) -> None:
        self.bot.help_command = self.help_command


async def setup(bot: Fishie):
    await bot.add_cog(Help(bot))
