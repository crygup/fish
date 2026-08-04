import pkgutil
from pathlib import Path

from discord.ext import commands

from extensions.fun.corn import Corn
from extensions.owner import Owner
from extensions.spotify import Spotify


def test_fishing_is_not_in_the_live_extension_tree() -> None:
    root = Path(__file__).resolve().parents[1]
    modules = {
        module.name
        for module in pkgutil.iter_modules(
            [str(root / "src" / "extensions")],
            prefix="extensions.",
        )
    }
    assert "extensions.fishing" not in modules


def test_owner_spotify_controls_are_staged_but_public_catalog_remains() -> None:
    owner_commands = {command.name for command in Owner.__cog_commands__}
    assert {
        "queue",
        "spotifyshuffle",
        "player",
        "skip",
        "pause",
        "play",
        "previous",
        "like",
        "unlike",
        "refresh",
    }.isdisjoint(owner_commands)
    spotify_commands = {command.name for command in Spotify.__cog_commands__}
    assert {"spotify", "album", "artist", "cover"} <= spotify_commands
    assert {
        "queue",
        "shuffle",
        "repeat",
        "player",
        "skip",
        "pause",
        "play",
        "restart",
        "rewind",
        "like",
        "unlike",
    }.isdisjoint(spotify_commands)


def test_corn_is_text_only_and_restart_aliases_shutdown() -> None:
    assert isinstance(Corn.corn, commands.Group)
    assert not isinstance(Corn.corn, commands.HybridGroup)
    shutdown = next(
        command for command in Owner.__cog_commands__ if command.name == "shutdown"
    )
    assert "restart" in shutdown.aliases
