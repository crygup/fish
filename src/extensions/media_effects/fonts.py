from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from utils.paths import FILES_ROOT

FONT_ROOT = Path(
    os.getenv(
        "FISHIE_EFFECT_FONT_ROOT",
        str(FILES_ROOT / "fonts" / "text"),
    )
)


@dataclass(frozen=True, slots=True)
class EffectFont:
    name: str
    filename: str
    aliases: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return FONT_ROOT / self.filename


# Every bundled family is distributed by Google Fonts under the SIL Open Font
# License 1.1. Source URLs and license attribution live beside the assets.
EFFECT_FONTS = (
    EffectFont("Roboto", "Roboto.ttf"),
    EffectFont("Open Sans", "OpenSans.ttf", ("opensans",)),
    EffectFont("Lato", "Lato.ttf"),
    EffectFont("Montserrat", "Montserrat.ttf"),
    EffectFont("Oswald", "Oswald.ttf"),
    EffectFont("Raleway", "Raleway.ttf"),
    EffectFont("Poppins", "Poppins.ttf"),
    EffectFont("Bebas Neue", "BebasNeue.ttf", ("bebas",)),
    EffectFont("Anton", "Anton.ttf"),
    EffectFont("Bangers", "Bangers.ttf"),
    EffectFont("Comic Neue", "ComicNeue.ttf", ("comic",)),
    EffectFont("Lobster", "Lobster.ttf"),
    EffectFont("Pacifico", "Pacifico.ttf"),
    EffectFont("Playfair Display", "PlayfairDisplay.ttf", ("playfair",)),
    EffectFont("Noto Sans", "NotoSans.ttf", ("noto",)),
)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


_FONT_LOOKUP = {
    _normalize(alias): font
    for font in EFFECT_FONTS
    for alias in (font.name, Path(font.filename).stem, *font.aliases)
}


def font_names() -> tuple[str, ...]:
    return tuple(font.name for font in EFFECT_FONTS)


def find_effect_font(value: str) -> EffectFont:
    selected = _FONT_LOOKUP.get(_normalize(value or "roboto"))
    if selected is None:
        raise ValueError(
            "Unknown font. Choose one of: " + ", ".join(font_names()) + "."
        )
    return selected


def load_effect_font(
    value: str,
    size: int,
    *,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    selected = find_effect_font(value)
    candidates = (
        selected.path,
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        try:
            font = ImageFont.truetype(str(path), size)
            if bold and hasattr(font, "set_variation_by_name"):
                try:
                    font.set_variation_by_name("Bold")
                except OSError:
                    pass
            return font
        except OSError:
            continue
    return ImageFont.load_default()
