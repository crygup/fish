from __future__ import annotations

import pytest

from extensions.mudae.sphere import (
    OQ_SPHERE_MAP,
    SPHERE_MAP,
    SphereCog,
    SphereView,
    UnsupportedSphereEmoji,
)
from utils.emojis import spR2


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


def test_unknown_cell_is_retained_for_a_disabled_blank_grid() -> None:
    cog = SphereCog.__new__(SphereCog)
    revealed, unknown = cog._parse_oq_components_tolerant(
        _component({"id": "999999", "name": "spX"})
    )

    assert revealed == {}
    assert set(unknown) == {0}

    view = SphereView(
        object(),
        revealed,
        None,
        unknown_positions=set(unknown),
        disabled=True,
    )
    assert len(view.children) == 25
    assert view.children[0].emoji is None
    assert all(button.disabled for button in view.children)
