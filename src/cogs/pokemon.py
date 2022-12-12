import re
from typing import Dict, List, Any

import asyncpg
import discord
import pandas as pd
from discord.ext import commands

from bot import Bot
from utils import (
    AuthorView,
    BoolConverter,
    Context,
    SimplePages,
    setup_pokemon,
    to_bytesio,
)


async def setup(bot: Bot):
    await bot.add_cog(Pokemon(bot))


class Pokemon(commands.Cog, name="pokemon"):
    """Pokemon related commands


    Enable auto-solving with `fish auto_solve`"""

    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="pokeball", id=1006847357381521428)

    def get_pokemon(self, guess: str) -> List:
        found = []
        guess = re.sub("[\U00002640\U0000fe0f|\U00002642\U0000fe0f]", "", guess)
        guess = re.sub("[\U000000e9]", "e", guess)

        sorted_guesses = [p for p in self.bot.pokemon if len(p) == len(guess)]
        for p in sorted_guesses:
            new_pattern = re.compile(guess.replace(r"_", r"[a-z]{1}"))
            results = new_pattern.match(p)

            if results is None:
                continue

            answer = results.group()

            found.append(answer)

        return found

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if (
            message.guild is None
            or message.author.id != 716390085896962058
            or str(message.guild.id)
            not in await self.bot.redis.smembers("poketwo_guilds")
            or str(message.guild.owner_id)
            in await self.bot.redis.smembers("block_list")
            or str(message.guild.id) in await self.bot.redis.smembers("block_list")
            or r"\_" not in message.content
        ):
            return

        to_search = re.match(
            r'the pokémon is (?P<pokemon>[^"]+).', message.content.lower()
        )

        if to_search is None:
            return

        to_search = re.sub(r"\\", "", to_search.groups()[0])
        found = self.get_pokemon(to_search)

        if found == []:
            await message.channel.send("Couldn't find anything matching that, sorry.")
            return

        joined = "\n".join(found)
        await message.channel.send(joined)

    @commands.command(name="hint")
    async def hint(self, ctx: Context):
        """Auto solve a pokétwo hint message."""
        if ctx.message.reference is None:
            await ctx.send("Please reply to a message contain a pokétwo hint.")
            return

        to_search = re.match(
            r'the pokémon is (?P<pokemon>[^"]+).', ctx.message.reference.resolved.content.lower()  # type: ignore
        )

        if to_search is None:
            return

        to_search = re.sub(r"\\", "", to_search.groups()[0])

        found = self.get_pokemon(to_search)

        if found == []:
            await ctx.send("Couldn't find anything matching that, sorry.")
            return

        joined = "\n".join(found)
        await ctx.send(joined)

    @commands.command(name="update_pokemon", extras={"UPerms": ["Bot Owner"]})
    @commands.is_owner()
    async def update_pokemon(self, ctx: Context):
        await setup_pokemon(self.bot)

        await ctx.send("updated olk")

    async def poke_pages(self, ctx: Context, name, title):
        data = [p.title() for p in self.bot.pokemon if p.startswith(name)]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = title
        await pages.start(ctx)

    @commands.command(
        name="auto_solve", aliases=("as",), extras={"UPerms": ["Manage Server"]}
    )
    @commands.has_permissions(manage_guild=True)
    async def auto_solve(self, ctx: Context):
        """Toggles automatic solving of pokétwo's pokémon hints"""

        try:
            sql = "INSERT INTO guild_settings(guild_id, poketwo) VALUES($1, $2)"
            await self.bot.pool.execute(sql, ctx.guild.id, True)
        except asyncpg.UniqueViolationError:
            if str(ctx.guild.id) in await self.bot.redis.smembers("poketwo_guilds"):
                sql = "UPDATE guild_settings SET poketwo = NULL WHERE guild_id = $1"
                await self.bot.pool.execute(sql, ctx.guild.id)
                await self.bot.redis.srem("poketwo_guilds", ctx.guild.id)
                await ctx.send("Disabled auto solving for this server.")
                return

            sql = "UPDATE guild_settings SET poketwo = $1 WHERE guild_id = $2"
            await self.bot.pool.execute(sql, True, ctx.guild.id)

        await self.bot.redis.sadd("poketwo_guilds", ctx.guild.id)
        await ctx.send("Enabled auto solving for this server.")

    @commands.command(name="megas")
    async def megas(self, ctx: Context):
        """Pokémon with a mega evolution"""
        await self.poke_pages(ctx, "mega ", "Pokémon with a mega evolution")

    @commands.command(name="gigantamax", aliases=("gigas",))
    async def gigantamax(self, ctx: Context):
        """Pokémon with a gigantamax evolution"""
        await self.poke_pages(ctx, "gigantamax ", "Pokémon with a gigantamax evolution")

    @commands.command(name="festive")
    async def festive(self, ctx: Context):
        """Pokémon with a festive alternative"""
        await self.poke_pages(ctx, "festive ", "Pokémon with a festive alternative")

    @commands.command(name="shadow")
    async def shadow(self, ctx: Context):
        """Pokémon with a shadow alternative"""
        await self.poke_pages(ctx, "shadow ", "Pokémon with a shadow alternative")

    @commands.command(name="hisuian")
    async def hisuian(self, ctx: Context):
        """Pokémon with a hisuian region alternative"""
        await self.poke_pages(
            ctx, "hisuian ", "Pokémon with a hisuian region alternative"
        )

    @commands.command(name="galarian")
    async def galarian(self, ctx: Context):
        """Pokémon with a galarian region alternative"""
        await self.poke_pages(
            ctx, "galarian ", "Pokémon with a galarian region alternative"
        )

    @commands.command(name="alolan")
    async def alolan(self, ctx: Context):
        """Pokémon with an alolan region alternative"""
        await self.poke_pages(
            ctx, "alolan ", "Pokémon with a alolan region alternative"
        )

    @commands.command(name="wtp", hidden=True)
    @commands.is_owner()
    async def wtp(self, ctx: Context):
        await ctx.trigger_typing()
        data = await ctx.dagpi("https://api.dagpi.xyz/data/wtp")
        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_author(name="Who's that pokemon?")
        image = await to_bytesio(ctx.session, data["question"])
        file = discord.File(fp=image, filename="pokemon.png")
        embed.set_image(url="attachment://pokemon.png")
        await ctx.send(embed=embed, file=file, view=WTPView(ctx, data))


class WTPModal(discord.ui.Modal, title="Who's that Pokémon?"):
    def __init__(self, ctx: Context, data: Dict[Any, Any]):
        super().__init__()
        self.ctx = ctx
        self.data = data

    pokemon = discord.ui.TextInput(
        label="Who's that Pokémon?",
        style=discord.TextStyle.short,
        required=True,
    )

    def do_we_add_s(self, number: int):
        return True if number > 1 or number == 0 else False

    async def update_data(self) -> discord.Embed:
        correct_name = str(self.data["Data"]["name"]).lower()
        given_name = self.pokemon.value.lower()
        correct: bool = correct_name == given_name
        embed = discord.Embed(color=0x008341 if correct else 0xFF0000)
        embed.set_author(name="Correct!" if correct else "Incorrect!")

        # we really shouldn't be making 2 calls to the database for one command but for UX reasons, we have to.
        data = await self.ctx.bot.pool.fetchrow(
            "SELECT * FROM pokemon_guesses WHERE pokemon_name = $1 AND author_id = $2",
            correct_name,
            self.ctx.author.id,
        )

        if data:
            sql = f"""
            UPDATE pokemon_guesses SET {'correct' if correct else 'incorrect'} + 1 WHERE pokemon_name = $1 AND author_id = $2 RETURNING *
            """

            new_data = await self.ctx.bot.pool.fetchrow(
                sql, correct_name, self.ctx.author.id
            )
        else:
            sql = f"""
            INSERT INTO pokemon_guesses (pokemon_name, author_id, {'correct' if correct else 'incorrect'}) VALUES($1, $2, $3) RETURNING *
            """
            new_data = await self.ctx.bot.pool.fetchrow(
                sql, correct_name, self.ctx.author.id, 1
            )

        embed.set_footer(
            text=f"You've got {correct_name.title()} correct {new_data['correct']:,} time{self.do_we_add_s(new_data['correct'])} and incorrect {new_data['incorrect']:,} time{self.do_we_add_s(new_data['incorrect'])}"  # type: ignore # i think pyright is just broken or something
        )

        return embed

    async def on_submit(self, interaction: discord.Interaction):
        embed = await self.update_data()

        file = discord.File(
            await to_bytesio(self.ctx.session, self.data["answer"]),
            filename="pokemon.png",
        )

        embed.set_image(url="attachment://pokemon.png")

        if interaction.message:
            await interaction.message.edit(attachments=[file], embed=embed, view=None)
            await interaction.response.defer()
            return

        await interaction.response.send_message("Something went wrong!", ephemeral=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:

        await self.ctx.bot.send_error(self.ctx, error)


class WTPView(AuthorView):
    def __init__(self, ctx: Context, data: dict[str, str]):
        super().__init__(ctx)
        self.ctx = ctx
        self.data = data

    @discord.ui.button(label="click me")
    async def modal(self, interaction: discord.Interaction, __):
        await interaction.response.send_modal(WTPModal(self.ctx, self.data))
