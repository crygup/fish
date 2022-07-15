from shutil import get_unpack_formats
from typing import List
import discord
from discord.ext import commands
from bot import Bot
import re

async def setup(bot: Bot):
    await bot.add_cog(Pokemon(bot))


class Pokemon(commands.Cog, name="pokemon"):
    def __init__(self, bot: Bot):
        self.bot = bot


    def get_pokemon(self, guess: str) -> List:
        found = []
        for p in self.bot.pokemon:
            if len(p) != len(guess):
                continue

            new_pattern = re.compile(guess.replace(r'_', r'[a-z]{1}'))
            results = new_pattern.match(p)

            if results is None:
                continue

            found.append(results.group())

        return found

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.author.id != 716390085896962058:
            return

        if r"\_" not in message.content:
            return

        to_search = re.match(r'the pokémon is (?P<pokemon>[^"]+).', message.content.lower())

        if to_search is None:
            return

        to_search = to_search.groups()[0].replace('\\', '')
        found = self.get_pokemon(to_search)

        if found == []:
            await message.channel.send('Couldn\'t find anything matching that, sorry.')
            return

        joined = '\n'.join(found)
        await message.channel.send(f'Possible answers: \n{joined}')


    @commands.command(name='wtp')
    async def wtp(self, ctx: commands.Context, guess: str):

        to_search = guess.lower().replace('\\', '')
        found = self.get_pokemon(to_search)

        if found == []:
            await ctx.send('Couldn\'t find anything matching that, sorry.')
            return

        joined = '\n'.join(found)
        await ctx.send(f'Possible answers: \n{joined}')
