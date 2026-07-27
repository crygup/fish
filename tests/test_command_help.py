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
from extensions.help import command_usage, make_command_embed
from extensions.media_effects.commands import Images
from extensions.tools import Tools


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

    assert command_usage(ctx, Images.cube) == (
        "fish cube <input> [-speed 1 -clockwise]"
    )
    assert command_usage(ctx, Images.overlay_group_image) == (
        "fish overlay image <input> -overlay <input> [-opacity 0.7 -scale 1]"
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
    assert "-delay" in (Tools.screenshot.help or "")
    assert "-onlyme" in (Fun.phone.help or "")
    assert "-clockwise" in (Images.cube.help or "")
    assert "-audio" in (Images.audio_group_replace.help or "")


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


def test_standalone_media_commands_remain_text_only() -> None:
    for command in (
        Images.caption,
        Images.globe,
        Images.speed,
        Images.spin3d,
        Images.cube,
        Images.overlay_video,
        Images.reverse,
        Images.convert_command,
        Images.volume,
    ):
        assert isinstance(command, commands.Command)
        assert not isinstance(command, commands.HybridCommand)
