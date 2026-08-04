from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from utils.paths import FILES_ROOT

IMAGE_ASSETS_ROOT = FILES_ROOT / "images"
IMAGE_ASSETS_CATALOG = IMAGE_ASSETS_ROOT / "catalog.json"
IMAGE_ASSET_EXTENSIONS = frozenset({".apng", ".gif", ".jpeg", ".jpg", ".png", ".webp"})


@dataclass(frozen=True, slots=True)
class ImageAsset:
    id: int
    name: str
    display_name: str
    category: str
    path: Path
    format: str
    animated: bool
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.category})"


@lru_cache(maxsize=1)
def image_asset_catalog() -> tuple[ImageAsset, ...]:
    try:
        payload: Any = json.loads(IMAGE_ASSETS_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "The bundled image-asset catalog could not be loaded."
        ) from error

    entries = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("The bundled image-asset catalog is invalid.")

    root = IMAGE_ASSETS_ROOT.resolve()
    catalog: list[ImageAsset] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative = Path(str(entry.get("path") or ""))
        path = (IMAGE_ASSETS_ROOT / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file() or path.suffix.casefold() not in IMAGE_ASSET_EXTENSIONS:
            continue
        try:
            catalog.append(
                ImageAsset(
                    id=int(entry["id"]),
                    name=str(entry["name"]),
                    display_name=str(entry["display_name"]),
                    category=str(entry["category"]),
                    path=path,
                    format=str(entry["format"]),
                    animated=bool(entry["animated"]),
                    width=int(entry["width"]),
                    height=int(entry["height"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    if not catalog:
        raise RuntimeError("No bundled image assets are available.")
    return tuple(sorted(catalog, key=lambda asset: asset.id))
