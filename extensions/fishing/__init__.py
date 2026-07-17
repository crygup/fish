from __future__ import annotations

import json
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


CATALOG_PATH = Path(__file__).resolve().parents[2] / "files" / "data" / "fishing.json"


def _key(value: str) -> str:
    """Normalize a user-provided catalog name to its JSON key."""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


class Fishing(Cog):
    """Fishing, rods, bait, catches, and the fishing coin balance."""

    emoji = discord.PartialEmoji(name="\U0001f3a3")
    hidden = True

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        with CATALOG_PATH.open(encoding="utf-8") as file:
            self.catalog: dict[str, Any] = json.load(file)

        self.rods = {item["key"]: item for item in self.catalog["rods"]}
        self.bait = {item["key"]: item for item in self.catalog["bait"]}
        self.rarities = {item["key"]: item for item in self.catalog["rarities"]}
        self.tiers = {item["key"]: item for item in self.catalog["tiers"]}
        self.rarity_order = [item["key"] for item in self.catalog["rarities"]]
        self.creatures = {item["key"]: item for item in self.catalog["creatures"]}

        if (
            len(self.rods) != 10
            or len(self.bait) != 10
            or len(self.rarities) != 10
            or len(self.tiers) != 10
        ):
            raise ValueError(
                "Fishing catalog must contain ten rods, bait, and rarities"
            )
        if not self.creatures or "kraken" not in self.creatures:
            raise ValueError("Fishing catalog must contain creatures and a Kraken")
        for tier in self.tiers.values():
            if tier["required_rod"] not in self.rods:
                raise ValueError(f"Unknown required rod for tier {tier['key']}")
        for creature in self.creatures.values():
            tier = self.tiers.get(creature["tier"])
            if tier is None:
                raise ValueError(f"Unknown tier for creature {creature['key']}")
            required_power = self.rods[tier["required_rod"]]["power"]
            if creature["min_rod"] != required_power:
                raise ValueError(f"Creature tier/tool mismatch for {creature['key']}")

    async def _ensure_account(self, user_id: int) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO fishing_accounts (user_id, coins)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
            self.catalog["starting_coins"],
        )

    async def _account(self, user_id: int):
        await self._ensure_account(user_id)
        account = await self.bot.pool.fetchrow(
            """
            SELECT user_id, coins, equipped_rod_key, equipped_rod_rarity_key,
                   equipped_bait_key, total_catches
            FROM fishing_accounts
            WHERE user_id = $1
            """,
            user_id,
        )
        if account is None:
            raise RuntimeError("Fishing account was not created.")
        return account

    async def _locked_account(self, connection: Any, user_id: int):
        account = await connection.fetchrow(
            "SELECT * FROM fishing_accounts WHERE user_id = $1 FOR UPDATE",
            user_id,
        )
        if account is None:
            raise RuntimeError("Fishing account was not created.")
        return account

    @staticmethod
    def _weighted(items: list[dict[str, Any]], weight_key: str) -> dict[str, Any]:
        return random.choices(
            items,
            weights=[max(float(item[weight_key]), 0.0) for item in items],
            k=1,
        )[0]

    def _pick_creature(self, rod: dict[str, Any]) -> dict[str, Any]:
        eligible = [
            creature
            for creature in self.catalog["creatures"]
            if creature["min_rod"] <= rod["power"]
        ]
        # Apply the starter boost without mutating the JSON-backed catalog.
        weights = [
            creature["weight"] * float(self.tiers[creature["tier"]]["weight"])
            for creature in eligible
        ]
        return random.choices(eligible, weights=weights, k=1)[0]

    def _pick_rarity(self, rod: dict[str, Any], luck: float = 1.0) -> dict[str, Any]:
        allowed = self.catalog["rarities"][: int(rod["rarity_cap"]) + 1]
        weights = [
            float(item["weight"]) * (luck ** (index / max(len(allowed) - 1, 1)))
            for index, item in enumerate(allowed)
        ]
        return random.choices(allowed, weights=weights, k=1)[0]

    @staticmethod
    def _money(value: int | float) -> str:
        return f"{value:,.0f} coin" + ("s" if value != 1 else "")

    def _rod_label(self, key: str | None, rarity_key: str | None) -> str:
        if not key or key not in self.rods:
            return "None"
        rod = self.rods[key]
        rarity = self.rarities.get(rarity_key or "common", self.rarities["common"])
        return f"{rarity['name']} {rod['name']} Fishing Rod"

    @commands.command(name="balance", aliases=("coins", "wallet"))
    async def balance(self, ctx: Context):
        """Show your fishing coin balance."""
        account = await self._account(ctx.author.id)
        await ctx.send(
            f"{ctx.author.mention}, you have **{self._money(account['coins'])}**.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @commands.command(name="shop", aliases=("fishing_shop",))
    async def shop(self, ctx: Context):
        """Show available fishing rods and bait."""
        embed = discord.Embed(title="Fishing Shop", color=self.bot.embedcolor)
        embed.add_field(
            name="Fishing Rods",
            value="\n".join(
                f"**{rod['name']}** — {self._money(rod['price'])}"
                for rod in self.catalog["rods"]
            ),
            inline=False,
        )
        embed.add_field(
            name="Bait",
            value="\n".join(
                f"**{item['name']}** — {self._money(item['price'])}"
                for item in self.catalog["bait"]
            ),
            inline=False,
        )
        embed.set_footer(text="Buy with: fish buy <rod or bait> [quantity]")
        await ctx.send(embed=embed)

    @commands.command(name="buy", aliases=("purchase",))
    @app_commands.describe(
        item="A rod or bait name from the fishing shop",
        quantity="How many bait items to buy",
    )
    async def buy(self, ctx: Context, item: str, quantity: int = 1):
        """Buy a fishing rod upgrade or bait."""
        if quantity < 1 or quantity > 100:
            raise commands.BadArgument("Quantity must be between 1 and 100.")
        item_key = _key(item)
        rod = self.rods.get(item_key)
        bait = self.bait.get(item_key)
        if rod is None:
            if bait is None:
                raise commands.BadArgument("That item is not in the fishing shop.")
            price = int(bait["price"]) * quantity
        else:
            price = int(rod["price"]) * quantity
        if rod is not None and quantity != 1:
            raise commands.BadArgument(
                "Fishing rods can only be purchased one at a time."
            )

        await self._ensure_account(ctx.author.id)
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                account = await self._locked_account(connection, ctx.author.id)
                if account["coins"] < price:
                    raise commands.BadArgument(
                        f"You need **{self._money(price)}**, but only have **{self._money(account['coins'])}**."
                    )

                if rod is not None:
                    current_key = account["equipped_rod_key"]
                    if current_key and self.rods[current_key]["power"] >= rod["power"]:
                        raise commands.BadArgument(
                            "You can only buy a fishing rod upgrade."
                        )
                    rarity = self._weighted(
                        self.catalog["rarities"][: int(rod["rarity_cap"]) + 1], "weight"
                    )
                    await connection.execute(
                        """
                        INSERT INTO fishing_rods (user_id, rod_key, rarity_key, quantity)
                        VALUES ($1, $2, $3, 1)
                        ON CONFLICT (user_id, rod_key, rarity_key)
                        DO UPDATE SET quantity = fishing_rods.quantity + 1
                        """,
                        ctx.author.id,
                        item_key,
                        rarity["key"],
                    )
                    await connection.execute(
                        """
                        UPDATE fishing_accounts
                        SET coins = coins - $2, equipped_rod_key = $3,
                            equipped_rod_rarity_key = $4
                        WHERE user_id = $1
                        """,
                        ctx.author.id,
                        price,
                        item_key,
                        rarity["key"],
                    )
                    result = f"Bought a **{rarity['name']} {rod['name']} Fishing Rod**"
                else:
                    if bait is None:
                        raise RuntimeError("Fishing bait was not found.")
                    await connection.execute(
                        """
                        INSERT INTO fishing_bait (user_id, bait_key, quantity)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (user_id, bait_key)
                        DO UPDATE SET quantity = fishing_bait.quantity + EXCLUDED.quantity
                        """,
                        ctx.author.id,
                        item_key,
                        quantity,
                    )
                    await connection.execute(
                        """
                        UPDATE fishing_accounts
                        SET coins = coins - $2,
                            equipped_bait_key = COALESCE(equipped_bait_key, $3)
                        WHERE user_id = $1
                        """,
                        ctx.author.id,
                        price,
                        item_key,
                    )
                    result = f"Bought **{quantity} {bait['name']}**"

        await ctx.send(f"{result} for **{self._money(price)}**.")

    @commands.command(name="equip", aliases=("use",))
    @app_commands.describe(item="The bait to equip")
    async def equip(self, ctx: Context, item: str):
        """Equip bait you already own."""
        item_key = _key(item)
        bait = self.bait.get(item_key)
        if bait is None:
            raise commands.BadArgument("That is not a valid bait type.")
        await self._ensure_account(ctx.author.id)
        owned = await self.bot.pool.fetchval(
            "SELECT quantity FROM fishing_bait WHERE user_id = $1 AND bait_key = $2",
            ctx.author.id,
            item_key,
        )
        if not owned:
            raise commands.BadArgument(f"You do not own any **{bait['name']}**.")
        await self.bot.pool.execute(
            "UPDATE fishing_accounts SET equipped_bait_key = $2 WHERE user_id = $1",
            ctx.author.id,
            item_key,
        )
        await ctx.send(f"Equipped **{bait['name']}**.")

    @commands.command(name="inventory", aliases=("inv", "bag"))
    async def inventory(self, ctx: Context):
        """Show your fishing equipment, bait, and notable catches."""
        account = await self._account(ctx.author.id)
        bait_rows = await self.bot.pool.fetch(
            """
            SELECT bait_key, quantity FROM fishing_bait
            WHERE user_id = $1 AND quantity > 0 ORDER BY bait_key
            """,
            ctx.author.id,
        )
        catches = await self.bot.pool.fetch(
            """
            SELECT creature_key, rarity_key, quantity FROM fishing_catches
            WHERE user_id = $1 AND quantity > 0
            ORDER BY quantity DESC, creature_key LIMIT 12
            """,
            ctx.author.id,
        )
        bait_text = (
            ", ".join(
                f"{self.bait[row['bait_key']]['name']} ×{row['quantity']}"
                for row in bait_rows
            )
            or "None"
        )
        catch_text = (
            "\n".join(
                f"{self.rarities[row['rarity_key']]['name']} {self.creatures[row['creature_key']]['name']} ×{row['quantity']}"
                for row in catches
            )
            or "No catches yet"
        )
        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Fishing Inventory",
            color=self.bot.embedcolor,
        )
        embed.add_field(name="Coins", value=self._money(account["coins"]))
        embed.add_field(
            name="Rod",
            value=self._rod_label(
                account["equipped_rod_key"], account["equipped_rod_rarity_key"]
            ),
        )
        embed.add_field(
            name="Equipped Bait",
            value=self.bait.get(account["equipped_bait_key"], {}).get("name", "None"),
        )
        embed.add_field(name="Bait Owned", value=bait_text, inline=False)
        embed.add_field(name="Catches", value=catch_text, inline=False)
        embed.set_footer(text=f"Total catches: {account['total_catches']:,}")
        await ctx.send(embed=embed)

    @commands.command(name="cast", aliases=("catch", "fish"))
    async def cast(self, ctx: Context):
        """Cast your equipped rod and catch a sea creature."""
        await self._ensure_account(ctx.author.id)
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                account = await self._locked_account(connection, ctx.author.id)
                if not account["equipped_rod_key"]:
                    raise commands.BadArgument(
                        "Buy a fishing rod first with `fish buy plastic`."
                    )
                if not account["equipped_bait_key"]:
                    raise commands.BadArgument("Buy bait first with `fish buy bread`.")
                rod = self.rods[account["equipped_rod_key"]]
                bait = self.bait[account["equipped_bait_key"]]
                consumed = await connection.execute(
                    """
                    UPDATE fishing_bait SET quantity = quantity - 1
                    WHERE user_id = $1 AND bait_key = $2 AND quantity > 0
                    """,
                    ctx.author.id,
                    bait["key"],
                )
                if consumed != "UPDATE 1":
                    raise commands.BadArgument(
                        f"You are out of **{bait['name']}**. Buy more bait or equip another type."
                    )
                creature = self._pick_creature(rod)
                rarity = self._pick_rarity(rod, float(bait["luck"]))
                rarity_data = self.rarities[rarity["key"]]
                payout = max(
                    1,
                    round(
                        float(creature["value"])
                        * float(rarity_data["multiplier"])
                        * float(rod["multiplier"])
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO fishing_catches (user_id, creature_key, rarity_key, quantity)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (user_id, creature_key, rarity_key)
                    DO UPDATE SET quantity = fishing_catches.quantity + 1
                    """,
                    ctx.author.id,
                    creature["key"],
                    rarity["key"],
                )
                await connection.execute(
                    """
                    UPDATE fishing_accounts
                    SET coins = coins + $2, total_catches = total_catches + 1
                    WHERE user_id = $1
                    """,
                    ctx.author.id,
                    payout,
                )

        await ctx.send(
            f"You caught a **{rarity['name']} {creature['name']}** and earned **{self._money(payout)}**!"
        )

    async def cog_check(self, ctx: commands.Context[Fishie]) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        raise commands.BadArgument("You are not allowed to use this command.")


async def setup(bot: Fishie):
    await bot.add_cog(Fishing(bot))
