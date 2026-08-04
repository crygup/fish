from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from utils.paths import FILES_ROOT

VIDEO_ASSETS_ROOT = FILES_ROOT / "videos"
VIDEO_ASSETS_CATALOG = VIDEO_ASSETS_ROOT / "catalog.json"
VIDEO_ASSET_EXTENSIONS = frozenset({".m4v", ".mov", ".mp4", ".webm"})


@dataclass(frozen=True, slots=True)
class VideoAsset:
    id: int
    name: str
    display_name: str
    category: str
    path: Path
    format: str
    duration: float
    width: int
    height: int
    has_audio: bool

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.category})"


@lru_cache(maxsize=1)
def video_asset_catalog() -> tuple[VideoAsset, ...]:
    try:
        payload: Any = json.loads(VIDEO_ASSETS_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "The bundled video-asset catalog could not be loaded."
        ) from error

    entries = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("The bundled video-asset catalog is invalid.")

    root = VIDEO_ASSETS_ROOT.resolve()
    catalog: list[VideoAsset] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative = Path(str(entry.get("path") or ""))
        path = (VIDEO_ASSETS_ROOT / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file() or path.suffix.casefold() not in VIDEO_ASSET_EXTENSIONS:
            continue
        try:
            catalog.append(
                VideoAsset(
                    id=int(entry["id"]),
                    name=str(entry["name"]),
                    display_name=str(entry["display_name"]),
                    category=str(entry["category"]),
                    path=path,
                    format=str(entry["format"]),
                    duration=float(entry["duration"]),
                    width=int(entry["width"]),
                    height=int(entry["height"]),
                    has_audio=bool(entry["has_audio"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    if not catalog:
        raise RuntimeError("No bundled video assets are available.")
    return tuple(sorted(catalog, key=lambda asset: asset.id))
