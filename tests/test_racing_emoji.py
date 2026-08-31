import pytest

from utils.racing_emoji import (
    classify_racing_emoji,
    parse_custom_racing_emoji,
    racing_emoji_category,
    racing_emoji_price,
)


@pytest.mark.parametrize(
    "value",
    ["🐟", "🐙", "🦈", "🦭", "🐬"],
)
def test_default_race_animals_use_sea_animal_price(value: str) -> None:
    assert racing_emoji_category(value) == "sea_animal"
    assert racing_emoji_price(value) == 50_000


def test_category_prices_cover_faces_hearts_animals_food_and_misc() -> None:
    expected = {
        "😀": ("human_face", 15_000),
        "🤡": ("human_face", 15_000),
        "❤️": ("heart", 20_000),
        "🐶": ("animal", 25_000),
        "🍕": ("food", 15_000),
        "⭐": ("misc", 10_000),
    }
    for emoji, (category, price) in expected.items():
        info = classify_racing_emoji(emoji)
        assert (info.category, info.price) == (category, price)


def test_variation_selector_is_accepted() -> None:
    assert racing_emoji_category("⭐️") == "misc"


@pytest.mark.parametrize("value", ["🟥", "🟦", "🔴", "⚫", "🔶"])
def test_solid_colour_shapes_are_not_for_sale(value: str) -> None:
    with pytest.raises(ValueError, match="Solid colour"):
        classify_racing_emoji(value)


def test_custom_emoji_is_always_custom_price() -> None:
    info = classify_racing_emoji("<a:party_blob:123456789012345678>")
    assert info.category == "custom"
    assert info.price == 100_000
    assert info.custom is not None
    assert info.custom.animated is True
    assert info.custom.rendered == "<a:party_blob:123456789012345678>"


def test_custom_emoji_parser_rejects_malformed_markup() -> None:
    assert parse_custom_racing_emoji("<:missing-id:>") is None
    with pytest.raises(ValueError, match="one Unicode or custom"):
        classify_racing_emoji("<:missing-id:>")


@pytest.mark.parametrize("value", ["", "not emoji", "🐟🐙", "<:a:1> trailing"])
def test_emoji_input_must_be_one_supported_emoji(value: str) -> None:
    with pytest.raises(ValueError):
        classify_racing_emoji(value)
