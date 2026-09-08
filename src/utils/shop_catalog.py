"""Built-in shop definitions, loaded once for command rendering.

Prices and labels sync at startup/migrate. Existing database ``enabled`` flags
and rows absent from JSON are preserved, so retiring an item never removes
ownership. Restart after editing this file's JSON source.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .paths import FILES_ROOT

CATALOG_PATH = FILES_ROOT / "data/shop_catalog.json"


def load_shop_catalog() -> dict[str, Any]:
    document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Shop catalog must be an object")
    for section in (
        "badges",
        "titles",
        "colors",
        "rings",
        "stat_badges",
        "racing_emoji",
    ):
        entries = document.get(section)
        if not isinstance(entries, list):
            raise ValueError(f"Shop catalog {section} must be a list")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid shop catalog entry in {section}")
            key = entry.get("key")
            if not isinstance(key, str) or not key.strip() or key in seen:
                raise ValueError(f"Missing or duplicate shop key in {section}: {key!r}")
            seen.add(key)
            if section != "stat_badges":
                price = entry.get("price")
                if type(price) is not int or not 0 < price <= 9_000_000_000_000_000_000:
                    raise ValueError(f"Invalid price for {section}/{key}")
            if type(entry.get("enabled", True)) is not bool:
                raise ValueError(f"Invalid enabled flag for {section}/{key}")
            if section == "badges" and entry.get("kind", "emoji") not in {
                "emoji",
                "flag",
                "custom_emoji",
            }:
                raise ValueError(f"Invalid badge kind for {key}")
            if section in {"badges", "stat_badges", "rings"} and not isinstance(
                entry.get("emoji"), str
            ):
                raise ValueError(f"Missing emoji for {section}/{key}")
            label = (
                "name"
                if section == "rings"
                else "label" if section == "racing_emoji" else "description"
            )
            if not isinstance(entry.get(label), str) or not entry[label].strip():
                raise ValueError(f"Missing label for {section}/{key}")
            if section == "colors" and key != "custom":
                if not re.fullmatch(
                    r"#[0-9A-Fa-f]{6}", str(entry.get("hex_value", ""))
                ):
                    raise ValueError(f"Invalid hex value for color {key}")
    return document


SHOP_CATALOG = load_shop_catalog()
