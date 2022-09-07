import re
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot, Context
from utils import TenorUrlConverter, human_join

from ._base import CogBase

emoji_extras = {"BPerms": ["Manage Emojis"], "UPerms": ["Manage Emojis"]}


class OtherCommands(CogBase):
    @commands.command(name="steal", aliases=("clone",), extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def steal(self, ctx: Context, *, emojis: Optional[str]):
        ref = ctx.message.reference
        content = ctx.message.content

        if emojis is None:
            if ref is None:
                await ctx.send(
                    "You need to provide some emojis to steal, either reply to a message or give them as an argument."
                )
                return

            resolved = ref.resolved
            if (
                isinstance(resolved, discord.DeletedReferencedMessage)
                or resolved is None
            ):
                return

            content = resolved.content

        pattern = re.compile(r"<a?:[a-zA-Z0-9\_]{1,}:[0-9]{1,}>")
        results = pattern.findall(content)

        if len(results) == 0:
            await ctx.send("No emojis found.")
            return

        message = await ctx.send("Stealing emojis...")

        if message is None:
            return

        completed_emojis = []
        for result in results:
            emoji = await commands.PartialEmojiConverter().convert(ctx, result)

            if emoji is None:
                continue

            try:
                e = await ctx.guild.create_custom_emoji(
                    name=emoji.name, image=await emoji.read()
                )
                completed_emojis.append(str(e))
            except discord.HTTPException:
                pass

            await message.edit(
                content=f'Successfully stole {human_join(completed_emojis, final="and")} *({len(completed_emojis)}/{len(results)})*.'
            )

    @commands.command(name="tenor")
    async def tenor(self, ctx: commands.Context, url: str):
        """Gets the actual gif URL from a tenor link"""

        real_url = await TenorUrlConverter().convert(ctx, url)
        await ctx.send(f"Here is the real url: {real_url}")

    # @commands.command(name="wordle", aliases=("word",), hidden=True)
    # @commands.is_owner()
    # async def wordle(self, ctx: Context, *, flags: str):
    #    """This uses flags to solve a wordle problem.
    #
    #    Use underscores to spcify blanks.
    #    Example: h_r___
    #
    #    Flags:
    #        -jeyy    - Uses jeyybot wordle list (this is default true for the jeyybot wordle command)
    #        -correct - letters that are correct
    #        -invalid - letters that are not used
    #    """
    #    words = self.bot.words
    #
    #    parser = Arguments(add_help=False, allow_abbrev=False)
    #    parser.add_argument("-j", "-jeyy", action="store_true", default=True)
    #    parser.add_argument("-c", "-correct", type=str)
    #    parser.add_argument("-i", "-invalid", type=str)
    #
    #    args = parser.parse_args(shlex.split(flags))
    #
    #    if args.j:
    #        words = self.bot.jeyy_words
    #
    #    word = ["_", "_", "_", "_", "_"]
    #    letters_to_skip = [letter for letter in args.i] if args.i else []
    #
    #    letters_to_use = "abcdefghijklmnopqrstuvwxyz"
    #
    #    if letters_to_skip:
    #        letters_to_use = re.sub(f"[{''.join(letters_to_skip)}]", "", letters_to_use)
    #
    #    if args.c:
    #        if len(args.c) != 5:
    #            await ctx.send("The word must be 5 letters long.")
    #            return
    #
    #        word = [letter for letter in args.c]
    #        for i, letter in enumerate(word):
    #            if letter == "_":
    #                word[i] = f"[{letters_to_use}]{{1}}"
    #            else:
    #                word[i] = f"[{letter}]{{1}}"
    #
    #    pattern = re.compile("".join(word))
    #
    #    guessed_words = [word for word in words if pattern.match(word)]
    #
    #    if guessed_words == []:
    #        await ctx.send("No words found.")
    #        return
    #
    #    formatted = guessed_words[:10]
    #    embed = discord.Embed(color=self.bot.embedcolor)
    #    embed.title = f"Possible words found ({len(formatted)}/{len(guessed_words):,})"
    #    embed.description = human_join(
    #        [f"**`{word}`**" for word in formatted], final="and"
    #    )
    #
    #    await ctx.send(embed=embed)
