from __future__ import annotations

from typing import cast

import discord
import pytest
from test_support import not_none, require_type

# Test doubles supply only the Discord/service fields exercised by each test.
from extensions.context import Context
from extensions.mudae.sphere import (
    OQ_SPHERE_MAP,
    OT_SIM_EMOJIS,
    OT_TEXT,
    SPHERE_MAP,
    SimOTView,
    SphereCog,
    SphereOTView,
    SphereView,
    UnsupportedSphereEmoji,
    _ot_color_count,
    _ot_random_layout,
    _ot_recommendations,
)
from utils.emojis import spR2, spW2


def _component(emoji: dict[str, object]) -> list[dict[str, object]]:
    return [{"components": [{"emoji": emoji}]}]


def test_sphere_parser_accepts_suffix_two_name_alias_without_known_id() -> None:
    cog = SphereCog.__new__(SphereCog)

    revealed = cog._parse_sphere_components(
        _component({"id": "999999", "name": "spB2"})
    )

    assert revealed == {0: "blue"}


def test_oq_parser_accepts_suffix_two_name_alias_without_known_id() -> None:
    cog = SphereCog.__new__(SphereCog)

    revealed = cog._parse_oq_components(_component({"id": "999999", "name": "SpP2"}))

    assert revealed == {0: "purple"}


def test_parsers_ignore_suffix_two_hidden_sphere() -> None:
    cog = SphereCog.__new__(SphereCog)

    assert (
        cog._parse_sphere_components(_component({"id": "999999", "name": "spU2"})) == {}
    )
    assert cog._parse_oq_components(_component({"id": "999999", "name": "spU2"})) == {}


def test_unknown_sphere_emoji_is_reported_instead_of_dropped() -> None:
    cog = SphereCog.__new__(SphereCog)

    with pytest.raises(UnsupportedSphereEmoji, match="spX:999999"):
        cog._parse_sphere_components(_component({"id": "999999", "name": "spX"}))


def test_known_maps_continue_to_use_custom_ids() -> None:
    cog = SphereCog.__new__(SphereCog)
    sphere_id = next(iter(SPHERE_MAP))
    oq_id = next(iter(OQ_SPHERE_MAP))

    assert cog._parse_sphere_components(
        _component({"id": str(sphere_id), "name": "different-name"})
    ) == {0: SPHERE_MAP[sphere_id]}
    assert cog._parse_oq_components(
        _component({"id": str(oq_id), "name": "different-name"})
    ) == {0: OQ_SPHERE_MAP[oq_id]}


def test_alternate_red_sphere_is_supported_by_id_and_name() -> None:
    cog = SphereCog.__new__(SphereCog)

    assert cog._parse_sphere_components(
        _component({"id": str(spR2.id), "name": "spR2"})
    ) == {0: "red"}
    assert cog._parse_oq_components(_component({"id": "999999", "name": "spR2"})) == {
        0: "red"
    }


def test_oq_alternate_reward_sphere_is_supported_by_id_and_name() -> None:
    cog = SphereCog.__new__(SphereCog)

    assert cog._parse_oq_components(
        _component({"id": str(spW2.id), "name": "spW2"})
    ) == {0: "red"}
    # Emoji IDs can differ between Mudae's guild-specific packs; the semantic
    # name should still identify this OQ reward sphere.
    assert cog._parse_oq_components(_component({"id": "999999", "name": "spW2"})) == {
        0: "red"
    }


def test_alternate_standard_spheres_are_supported_by_id_without_names() -> None:
    cog = SphereCog.__new__(SphereCog)

    assert cog._parse_sphere_components(_component({"id": "1437149292530765825"})) == {
        0: "blue"
    }
    assert cog._parse_oq_components(_component({"id": "1437149276160397404"})) == {
        0: "purple"
    }


def test_ot_parser_accepts_alternate_light_and_other_rare_sphere_names() -> None:
    cog = SphereCog.__new__(SphereCog)

    assert cog._parse_ot_components(
        _component({"id": str(1437149400685084783), "name": "spL2"})
    ) == {0: "light"}
    assert cog._parse_ot_components(_component({"id": "999999", "name": "spW2"})) == {
        0: "rainbow"
    }
    assert cog._parse_ot_components(_component({"id": "999999", "name": "spD3"})) == {
        0: "dark"
    }
    assert cog._parse_ot_components(_component({"id": "999999", "name": "spM4"})) == {
        0: "chaos"
    }


def test_ot_colour_count_and_random_layout_follow_ship_sizes() -> None:
    assert _ot_color_count(OT_TEXT + "\nNumber of different colors: **6**") == 6
    assert _ot_color_count(OT_TEXT + "\n**Number of different colors: 6**") == 6
    layout = _ot_random_layout(6)
    assert len(layout) == 25
    assert list(layout.values()).count("teal") == 4
    assert list(layout.values()).count("green") == 3
    assert list(layout.values()).count("yellow") == 3
    assert list(layout.values()).count("blue") == 11


def test_ot_empty_board_uses_bounded_initial_ranking() -> None:
    safe, danger, recommendations, probabilities, complete = _ot_recommendations({}, 9)

    assert complete is False
    assert not safe and not danger
    assert len(recommendations) == 4
    assert set(probabilities) == set(range(25))


def test_ot_recommendations_mark_forced_blue_cell_as_danger() -> None:
    layout = _ot_random_layout(6)
    blue_position = next(
        position for position, colour in layout.items() if colour == "blue"
    )
    revealed = {
        position: colour
        for position, colour in layout.items()
        if position != blue_position
    }
    safe, danger, recommendations, probabilities, complete = _ot_recommendations(
        not_none(revealed), 6
    )

    assert complete
    assert blue_position in danger
    assert probabilities[blue_position] == 1
    assert blue_position not in recommendations
    assert not safe


def test_ot_view_keeps_a_five_by_five_grid() -> None:
    view = SphereOTView(cast("Context", object()), {}, {1}, {0}, [1, 2, 3])

    assert len(view.children) == 25
    assert (
        require_type(view.children[0], discord.ui.Button).style.value == 4
    )  # danger/red button
    assert (
        require_type(view.children[1], discord.ui.Button).style.value == 3
    )  # green recommendation
    assert require_type(view.children[1], discord.ui.Button).disabled is False
    assert require_type(view.children[2], discord.ui.Button).disabled is False
    assert require_type(view.children[3], discord.ui.Button).disabled is False
    assert SphereOTView._emoji_for_colour("light") == OT_SIM_EMOJIS["light"]
    assert SphereOTView._emoji_for_colour("rainbow") is None


def test_simot_uses_only_mudae_custom_sphere_emojis() -> None:
    layout = {
        position: colour
        for position, colour in enumerate(
            [
                "teal",
                "green",
                "yellow",
                "orange",
                "light",
                "blue",
            ]
            * 4
            + ["blue"],
        )
    }
    view = SimOTView(cast("Context", object()), layout)

    # Reveal one of every simulated semantic colour.  A Unicode fallback must
    # never be emitted for OT cells.
    for position in range(6):
        view.revealed[position] = layout[position]
    view._build()
    for position in range(6):
        emoji = require_type(view.children[position], discord.ui.Button).emoji
        assert emoji is not None
        assert getattr(emoji, "id", None) is not None


def test_ot_revealed_source_emoji_is_preserved() -> None:
    cog = SphereCog.__new__(SphereCog)
    raw = _component({"id": "123456", "name": "spW2"})
    revealed, unknown, overrides = cog._parse_ot_components_with_emojis(raw)

    assert revealed == {0: "rainbow"}
    assert not unknown
    assert revealed is not None
    assert overrides[0].id == 123456
    view = SphereOTView(
        cast("Context", object()), revealed, set(), set(), [], emoji_overrides=overrides
    )
    assert require_type(view.children[0], discord.ui.Button).emoji == overrides[0]


def test_unknown_cell_is_retained_for_a_disabled_blank_grid() -> None:
    cog = SphereCog.__new__(SphereCog)
    revealed, unknown = cog._parse_oq_components_tolerant(
        _component({"id": "999999", "name": "spX"})
    )

    assert revealed == {}
    assert set(unknown) == {0}

    view = SphereView(
        cast("Context", object()),
        not_none(revealed),
        None,
        unknown_positions=set(unknown),
        disabled=True,
    )
    assert len(view.children) == 25
    assert require_type(view.children[0], discord.ui.Button).emoji is None
    assert require_type(view.children[0], discord.ui.Button).label == "\u200b"
    assert all(
        require_type(button, discord.ui.Button).disabled for button in view.children
    )
