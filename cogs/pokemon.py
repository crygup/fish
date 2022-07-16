import re
from typing import List

import discord
import pandas as pd
from bot import Bot
from discord.ext import commands
from utils import SimplePages


async def setup(bot: Bot):
    await bot.add_cog(Pokemon(bot))


class Pokemon(commands.Cog, name="pokemon"):
    def __init__(self, bot: Bot):
        self.bot = bot

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
        if message.author.id != 716390085896962058:
            return

        if r"\_" not in message.content:
            return

        to_search = re.match(
            r'the pokémon is (?P<pokemon>[^"]+).', message.content.lower()
        )

        if to_search is None:
            return

        to_search = to_search.groups()[0].replace("\\", "")
        found = self.get_pokemon(to_search)

        if found == []:
            await message.channel.send("Couldn't find anything matching that, sorry.")
            return

        joined = "\n".join(found)
        await message.channel.send(joined)

    @commands.command(name="wtp", aliases=('hint',))
    async def wtp(self, ctx: commands.Context, guess: str):
        to_search = guess.lower().replace("\\", "")
        found = self.get_pokemon(to_search)

        if found == []:
            await ctx.send("Couldn't find anything matching that, sorry.")
            return

        joined = "\n".join(found)
        await ctx.send(joined)

    @commands.command(name="update_pokemon")
    @commands.is_owner()
    async def update_pokemon(self, ctx: commands.Context):
        url = "https://raw.githubusercontent.com/poketwo/data/master/csv/pokemon.csv"
        data = pd.read_csv(url)
        pokemon = [str(p).lower() for p in data["name.en"]]

        for p in pokemon:
            if re.search(r"[\U00002640\U0000fe0f|\U00002642\U0000fe0f]", p):
                pokemon[pokemon.index(p)] = re.sub("[\U00002640\U0000fe0f|\U00002642\U0000fe0f]", "", p)
            if re.search(r"[\U000000e9]", p):
                pokemon[pokemon.index(p)] = re.sub("[\U000000e9]", "e", p)

        self.bot.pokemon = pokemon

        await ctx.send("updated")

    async def poke_pages(self, ctx: commands.Context, name, title):
        data = [p.title() for p in self.bot.pokemon if p.startswith(name)]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = title
        await pages.start(ctx)

    @commands.command(name='megas')
    async def megas(self, ctx: commands.Context):
        """Pokémon with a mega evolution"""
        await self.poke_pages(ctx, 'mega ', 'Pokémon with a mega evolution')

    @commands.command(name='gigantamax', aliases=('gigas',))
    async def gigantamax(self, ctx: commands.Context):
        """Pokémon with a gigantamax evolution"""
        await self.poke_pages(ctx, 'gigantamax ', 'Pokémon with a gigantamax evolution')

    @commands.command(name='festive')
    async def festive(self, ctx: commands.Context):
        """Pokémon with a festive alternative"""
        await self.poke_pages(ctx, 'festive ', 'Pokémon with a festive alternative')

    @commands.command(name='shadow')
    async def shadow(self, ctx: commands.Context):
        """Pokémon with a shadow alternative"""
        await self.poke_pages(ctx, 'shadow ', 'Pokémon with a shadow alternative')

    @commands.command(name='hisuian')
    async def hisuian(self, ctx: commands.Context):
        """Pokémon with a hisuian region alternative"""
        await self.poke_pages(ctx, 'hisuian ', 'Pokémon with a hisuian region alternative')

    @commands.command(name='galarian')
    async def galarian(self, ctx: commands.Context):
        """Pokémon with a galarian region alternative"""
        await self.poke_pages(ctx, 'galarian ', 'Pokémon with a galarian region alternative')

    @commands.command(name='alolan')
    async def alolan(self, ctx: commands.Context):
        """Pokémon with an alolan region alternative"""
        await self.poke_pages(ctx, 'alolan ', 'Pokémon with a alolan region alternative')