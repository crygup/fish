"""Sync built-in catalog rows without modifying user ownership or wallets."""

from typing import Any

import discord

from utils.shop_catalog import load_shop_catalog


async def sync_shop_catalog(connection: Any) -> None:
    # A transaction also prevents a partial catalog update if validation/SQL fails.
    document = load_shop_catalog()
    async with connection.transaction():
        for item in document["titles"]:
            await connection.execute(
                """INSERT INTO title_catalog(title_key, display_name, price, category, enabled)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (title_key) DO UPDATE SET display_name = EXCLUDED.display_name,
                    price = EXCLUDED.price, category = EXCLUDED.category""",
                item["key"],
                item["description"],
                item["price"],
                item.get("category", "Misc titles"),
                item.get("enabled", True),
            )
        for section, category in (("badges", "purchase"), ("stat_badges", "stat")):
            for item in document[section]:
                custom = item.get("kind") == "custom_emoji"
                emoji = discord.PartialEmoji.from_str(item.get("emoji", ""))
                await connection.execute(
                    """INSERT INTO badge_catalog(badge_key, category, display_name,
                        emoji_name, emoji_id, is_custom, unicode, animated, price, enabled)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (badge_key) DO UPDATE SET category = EXCLUDED.category,
                        display_name = EXCLUDED.display_name, emoji_name = EXCLUDED.emoji_name,
                        emoji_id = EXCLUDED.emoji_id, is_custom = EXCLUDED.is_custom,
                        unicode = EXCLUDED.unicode, animated = EXCLUDED.animated,
                        price = EXCLUDED.price""",
                    f"{category}:{item['key']}",
                    category,
                    item["description"],
                    "" if custom else emoji.name,
                    None if custom else emoji.id,
                    custom or emoji.id is not None,
                    not custom and emoji.id is None,
                    emoji.animated,
                    item.get("price"),
                    item.get("enabled", True),
                )
        for item in document["colors"]:
            await connection.execute(
                """INSERT INTO color_catalog(color_key, display_name, hex_value, price, enabled)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (color_key) DO UPDATE SET display_name = EXCLUDED.display_name,
                    hex_value = EXCLUDED.hex_value, price = EXCLUDED.price""",
                item["key"],
                item["description"],
                item.get("hex_value") or "#000000",
                item["price"],
                item.get("enabled", True),
            )
        for item in document["rings"]:
            emoji = discord.PartialEmoji.from_str(item["emoji"])
            await connection.execute(
                """INSERT INTO ring_catalog(ring_key, display_name, emoji_name, emoji_id,
                    unicode, animated, display, price, enabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (ring_key) DO UPDATE SET display_name = EXCLUDED.display_name,
                    emoji_name = EXCLUDED.emoji_name, emoji_id = EXCLUDED.emoji_id,
                    unicode = EXCLUDED.unicode, animated = EXCLUDED.animated,
                    display = EXCLUDED.display, price = EXCLUDED.price""",
                item["key"],
                item["name"],
                emoji.name,
                emoji.id,
                emoji.id is None,
                emoji.animated,
                item["emoji"],
                item["price"],
                item.get("enabled", True),
            )
