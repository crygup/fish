from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from utils.paths import FILES_ROOT

AUDIO_EFFECTS_ROOT = FILES_ROOT / "audio" / "effects"
AUDIO_EFFECTS_CATALOG = AUDIO_EFFECTS_ROOT / "catalog.json"


@dataclass(frozen=True, slots=True)
class AudioEffect:
    id: int
    name: str
    display_name: str
    category: str
    path: Path
    duration: float

    @property
    def label(self) -> str:
        return f"{self.id}. {self.display_name} ({self.category.title()})"


def _normalized_lookup(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


@lru_cache(maxsize=1)
def audio_effect_catalog() -> tuple[AudioEffect, ...]:
    try:
        payload: Any = json.loads(AUDIO_EFFECTS_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "The bundled audio-effect catalog could not be loaded."
        ) from error
    entries = payload.get("effects") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("The bundled audio-effect catalog is invalid.")

    catalog: list[AudioEffect] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative = Path(str(entry.get("path") or ""))
        path = (AUDIO_EFFECTS_ROOT / relative).resolve()
        try:
            path.relative_to(AUDIO_EFFECTS_ROOT.resolve())
        except ValueError:
            continue
        if not path.is_file() or path.suffix.casefold() != ".mp3":
            continue
        try:
            catalog.append(
                AudioEffect(
                    id=int(entry["id"]),
                    name=str(entry["name"]),
                    display_name=str(entry["display_name"]),
                    category=str(entry["category"]),
                    path=path,
                    duration=float(entry["duration"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not catalog:
        raise RuntimeError("No bundled audio effects are available.")
    return tuple(sorted(catalog, key=lambda effect: effect.id))


def find_audio_effect(value: str, *, allow_random: bool = True) -> AudioEffect:
    catalog = audio_effect_catalog()
    normalized = _normalized_lookup(value)
    if allow_random and normalized in {"", "random", "rand"}:
        return random.choice(catalog)
    if normalized.isdecimal():
        requested_id = int(normalized)
        for effect in catalog:
            if effect.id == requested_id:
                return effect
    exact = [
        effect
        for effect in catalog
        if normalized
        in {
            _normalized_lookup(effect.name),
            _normalized_lookup(effect.display_name),
            _normalized_lookup(f"{effect.category}_{effect.name}"),
        }
    ]
    if exact:
        return exact[0]
    partial = [
        effect
        for effect in catalog
        if normalized and normalized in _normalized_lookup(effect.name)
    ]
    if len(partial) == 1:
        return partial[0]
    raise ValueError("Unknown sound effect. Use its catalog ID, name, or `random`.")


@lru_cache(maxsize=32)
def audio_effect_data(effect_id: int) -> bytes:
    """Read a bundled effect through a small bounded in-memory cache."""

    effect = find_audio_effect(str(effect_id), allow_random=False)
    return effect.path.read_bytes()


def audio_effect_choices(query: str, *, limit: int = 25) -> list[AudioEffect]:
    normalized = _normalized_lookup(query)
    catalog = audio_effect_catalog()
    if not normalized:
        return list(catalog[:limit])
    ranked = [
        effect for effect in catalog if normalized in _normalized_lookup(effect.label)
    ]
    return ranked[:limit]
