from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import SimpleNamespace
from typing import cast

from discord.ext import commands

import extensions
from extensions.context import Context
from extensions.fun import Fun
from extensions.help import (
    HelpBackButton,
    HelpLayoutView,
    _command_description,
    _command_flag_lines,
    command_usage,
    make_command_embed,
)
from extensions.media_effects.commands import Images
from extensions.search import Search
from extensions.search.roblox import Roblox
from extensions.tools import Tools
from extensions.tools.purge import ReactionPurgeFlags


def registered_commands() -> list[commands.Command]:
    found: list[commands.Command] = []
    seen: set[int] = set()

    for module_info in pkgutil.walk_packages(extensions.__path__, "extensions."):
        module = importlib.import_module(module_info.name)
        for _, cog in inspect.getmembers(module, inspect.isclass):
            if cog.__module__ != module.__name__ or not issubclass(cog, commands.Cog):
                continue

            for command in getattr(cog, "__cog_commands__", ()):
                if id(command) in seen:
                    continue
                seen.add(id(command))
                found.append(command)

    return found


def test_every_registered_command_has_a_description() -> None:
    missing = [
        command.qualified_name
        for command in registered_commands()
        if not (command.help or command.description)
    ]
    assert missing == []


def test_generic_parser_arguments_have_custom_help_usage() -> None:
    generic = [
        command.qualified_name
        for command in registered_commands()
        if any(name in command.signature for name in ("argument", "input", "flags"))
        and not command.extras.get("usage")
    ]
    assert generic == []


def test_help_uses_custom_usage_and_qualified_command_names() -> None:
    ctx = cast(Context, SimpleNamespace(get_prefix="fish "))

    assert command_usage(ctx, Images.cube) == "fish cube <media> [flags...]"
    assert command_usage(ctx, Images.overlay_group) == (
        "fish overlay <media> <user|media|emoji|image|video|flag> [name|id|random] [flags...]"
    )


def test_large_group_help_splits_subcommands_across_valid_fields() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            bot=SimpleNamespace(embedcolor=0),
            get_prefix="fish ",
        ),
    )
    embed = make_command_embed(ctx, Images.image_effect)
    subcommand_fields = [
        field for field in embed.fields if (field.name or "").startswith("Subcommands")
    ]

    assert len(subcommand_fields) > 1
    assert all(len(field.value or "") <= 1024 for field in subcommand_fields)


def test_every_command_help_embed_respects_discord_limits() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            bot=SimpleNamespace(embedcolor=0),
            get_prefix="fish ",
        ),
    )
    failures: list[str] = []

    for command in registered_commands():
        embed = make_command_embed(ctx, command)
        if (
            len(embed.description or "") > 4096
            or len(embed.fields) > 25
            or any(
                len(field.name or "") > 256 or len(field.value or "") > 1024
                for field in embed.fields
            )
        ):
            failures.append(command.qualified_name)

    assert failures == []


def test_flag_commands_document_their_flags() -> None:
    assert "-skip" in (Tools.purge.help or "")
    assert "-format" in (Tools.download.help or "")
    assert "-delay" in (Search.screenshot.help or "")
    assert "-onlyme" in (Fun.phone.help or "")
    assert "-clockwise" in (Images.cube.help or "")
    assert "-audio" in (Images.audio_group_replace.help or "")


def test_detailed_help_renders_flags_and_excludes_effect_groups() -> None:
    assert "-skip" in "\n".join(_command_flag_lines(Tools.purge))
    assert "-reaction_id" in "\n".join(_command_flag_lines(Tools.purge_reactions))
    assert "-speed" in "\n".join(_command_flag_lines(Images.cube))
    assert _command_flag_lines(Images.effect_2) == []

    ctx = cast(
        Context,
        SimpleNamespace(
            bot=SimpleNamespace(embedcolor=0),
            get_prefix="fish ",
        ),
    )
    purge_embed = make_command_embed(ctx, Tools.purge)
    flags = [
        field.value or ""
        for field in purge_embed.fields
        if (field.name or "").startswith("Flags")
    ]
    assert flags
    assert all(value.startswith("```") and value.endswith("```") for value in flags)
    assert "-skip" in "\n".join(flags)
    assert command_usage(ctx, Tools.purge) == "fish purge [amount] [flags...]"
    assert command_usage(ctx, Tools.purge_reactions) == (
        "fish purge reactions [amount] [flags...]"
    )


def test_flag_codeblocks_align_syntax_and_description() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            bot=SimpleNamespace(embedcolor=0),
            get_prefix="fish ",
        ),
    )
    blur_embed = make_command_embed(ctx, Images.blur)
    flags = next(
        (field.value or "") for field in blur_embed.fields if field.name == "Flags"
    )

    assert "-radius              Change the blur strength." in flags
    assert "-type                Choose gaussian, box, or motion blur." in flags


def test_detailed_help_keeps_flags_out_of_command_description() -> None:
    command = next(
        command
        for command in Roblox.__cog_commands__
        if command.qualified_name == "roblox item"
    )
    description = _command_description(command)

    assert description == (
        "Look up a Roblox item with optional creator, sale, limited, and type filters."
    )
    assert "-creator" not in description
    assert "-creator" in "\n".join(_command_flag_lines(command))


def test_help_detail_has_back_button_and_restores_category_commands() -> None:
    category = SimpleNamespace(
        qualified_name="Media Effects",
        emoji="🌐",
        description="Media effects",
        hidden=False,
    )
    ctx = cast(
        Context,
        SimpleNamespace(
            bot=SimpleNamespace(embedcolor=0),
            author=SimpleNamespace(id=1),
            clean_prefix="fish",
            get_prefix="fish ",
        ),
    )
    view = HelpLayoutView(
        ctx,
        [category],
        selected_cog=category,
        commands_list=[Images.cube],
        detail=Images.cube,
    )

    rows = [item for item in view.children if hasattr(item, "children")]
    assert any(
        isinstance(child, HelpBackButton) for row in rows for child in row.children
    )

    class Response:
        async def edit_message(self, **kwargs):
            self.kwargs = kwargs

    interaction = SimpleNamespace(response=Response())
    import asyncio

    asyncio.run(view.back_to_category(interaction))
    assert view.detail is None
    assert any(
        type(child).__name__ == "HelpCommandSelect"
        for row in view.children
        for child in row.children
    )
    assert interaction.response.kwargs["allowed_mentions"].replied_user is False


def test_reaction_purge_documents_and_parses_reaction_filters() -> None:
    assert "-reaction" in (Tools.purge_reactions.help or "")
    assert "-reaction_id" in (Tools.purge_reactions.help or "")
    parsed = ReactionPurgeFlags.parse_flags(
        "-reaction_name sparkle -emoji_id 123 -skip"
    )
    assert parsed["reaction"] == ["sparkle"]
    assert parsed["reaction_id"] == ["123"]
    assert parsed["skip"] == ["true"]


def test_flag_converter_commands_have_help_and_custom_usage() -> None:
    failures: list[str] = []
    for command in registered_commands():
        uses_flags = any(
            inspect.isclass(parameter.annotation)
            and issubclass(parameter.annotation, commands.FlagConverter)
            for parameter in command.clean_params.values()
        )
        if uses_flags and (
            "-# -" not in (command.help or "") or not command.extras.get("usage")
        ):
            failures.append(command.qualified_name)

    assert failures == []


def test_parsed_media_flags_have_help_and_custom_usage() -> None:
    failures: list[str] = []
    for command in registered_commands():
        try:
            source = inspect.getsource(command.callback)
        except OSError:
            continue

        if "_parse_effect_flags(" not in source:
            continue
        if "-# -" not in (command.help or "") or not command.extras.get("usage"):
            failures.append(command.qualified_name)

    assert failures == []


def test_command_help_uses_single_dash_flags() -> None:
    failures: list[str] = []
    for command in registered_commands():
        usage = command.extras.get("usage", "")
        if "--" in (command.help or "") or (isinstance(usage, str) and "--" in usage):
            failures.append(command.qualified_name)

    assert failures == []


def test_media_effect_help_identifies_supported_media_inputs() -> None:
    failures: list[str] = []
    for root in Images.__cog_commands__:
        command_set = (
            tuple(root.walk_commands()) if isinstance(root, commands.Group) else (root,)
        )
        for command in command_set:
            usage = str(command.extras.get("usage", ""))
            if "<media>" in usage and "User/Emoji/Media URL" not in (
                command.help or ""
            ):
                failures.append(command.qualified_name)

    assert failures == []


def test_standalone_media_commands_remain_text_only() -> None:
    for command in (
        Images.caption,
        Images.globe,
        Images.speed,
        Images.spin3d,
        Images.cube,
        Images.crop_group_circle,
        Images.crop_group_triangle,
        Images.fade_group_in,
        Images.fade_group_out,
        Images.overlay_group_flag,
        Images.overlay_group,
        Images.reverse,
        Images.convert_command,
        Images.audio_group_volume,
    ):
        assert isinstance(command, commands.Command)
        assert not isinstance(command, commands.HybridCommand)
