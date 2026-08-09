from types import SimpleNamespace
from typing import Any, cast

from PIL import Image, ImageDraw

from extensions.lastfm.topster import (
    LASTFM_PLACEHOLDER,
    _album_artist,
    _album_cover_url,
    _fit_topster_text,
    _topster_font,
    _topster_target_user,
)


def test_topster_uses_the_largest_available_cover() -> None:
    item = {
        "image": [
            {"#text": ""},
            {"#text": ("https://lastfm.freetls.fastly.net/" "i/u/300x300/example.jpg")},
        ]
    }

    assert _album_cover_url(item) == (
        "https://lastfm.freetls.fastly.net/i/u/example.jpg"
    )


def test_topster_skips_lastfm_placeholder_covers() -> None:
    item = {
        "image": [
            {
                "#text": (
                    "https://lastfm.freetls.fastly.net/"
                    f"i/u/300x300/{LASTFM_PLACEHOLDER}"
                )
            }
        ]
    }

    assert _album_cover_url(item) is None


def test_topster_reads_album_artist_name() -> None:
    assert _album_artist({"artist": {"name": "Portishead"}}) == "Portishead"


def test_topster_uses_a_sole_prefix_mention_as_the_target_user() -> None:
    author = SimpleNamespace(id=1)
    target = SimpleNamespace(id=2)
    ctx = SimpleNamespace(
        interaction=None,
        author=author,
        message=SimpleNamespace(mentions=[target]),
    )

    assert _topster_target_user(cast(Any, ctx), cast(Any, author)) is target


def test_topster_truncates_labels_to_the_text_column() -> None:
    image = Image.new("RGB", (500, 100), "black")
    draw = ImageDraw.Draw(image)
    font = _topster_font(15)
    label = _fit_topster_text(draw, "A" * 200, font, 200)

    assert label.endswith("…")
    assert draw.textlength(label, font=font) <= 200


def test_topster_base_font_stays_monospace_for_mixed_scripts() -> None:
    font = _topster_font(15)

    assert "mono" in str(getattr(font, "path", "")).lower()
