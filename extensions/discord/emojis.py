from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import discord
from discord.ext import commands

from core import Cog
from utils import (
    TwemojiConverter,
    human_join,
    plural,
    to_image,
    EMOJI_RE,
)

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


class Emojis(Cog):
    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji_group(
        self,
        ctx: Context,
        emoji: Union[discord.Emoji, discord.PartialEmoji, TwemojiConverter],
    ):
        """Gets information about an emoji"""
        if isinstance(emoji, BytesIO):
            return await ctx.send(file=discord.File(emoji, "emoji.png"))

        if emoji is None:
            if not ctx.message.reference:
                raise commands.BadArgument("No emoji provided.")

            ref = ctx.message.reference.resolved

            if ref is None or isinstance(ref, discord.DeletedReferencedMessage):
                raise commands.BadArgument("No emoji found.")

            try:
                emoji = await commands.PartialEmojiConverter().convert(ctx, ref.content)
            except (commands.CommandError, commands.BadArgument):
                raise commands.BadArgument("No emoji found.")

        if isinstance(emoji, TwemojiConverter):
            raise commands.BadArgument("No emoji found.")

        embed = discord.Embed(
            timestamp=emoji.created_at,
            title=emoji.name,
            color=self.bot.embedcolor,
            description="**Raw text**: `<:{emoji.name}\u200b:{emoji.id}>`",
        )
        embed.description = f"**Raw text**: `<:{emoji.name}\u200b:{emoji.id}>`"

        if isinstance(emoji, discord.Emoji):
            if emoji.guild:
                assert embed.title

                embed.title += f" - {emoji.guild} ({emoji.guild_id})"

                try:
                    femoji = await emoji.guild.fetch_emoji(emoji.id)
                except:
                    femoji = None

                if femoji:
                    if femoji.user:
                        embed.description += (
                            f"\n**Author**: `{femoji.user} (ID: {femoji.user.id})`"
                        )

        embed.set_footer(text=f"ID: {emoji.id}\nCreated at")
        file = await emoji.to_file()
        embed.set_image(url=f"attachment://{file.filename}")

        await ctx.send(embed=embed, file=file)

    @emoji_group.command(name="create")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_create(self, ctx: GuildContext, name: str, url: str):
        try:
            image = await to_image(ctx.session, url, bytes=True)
            if isinstance(image, BytesIO):
                raise commands.BadArgument("Invalid image.")

            emoji = await ctx.guild.create_custom_emoji(
                name=name,
                image=image,
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as e:
            raise commands.BadArgument(f"Couldn't create emoji. \n{e}")

        await ctx.send(f"Successfully created {emoji}")

    @emoji_group.command(name="delete")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: GuildContext, *emojis: discord.Emoji):
        value = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(emojis)):emoji}?"
        )

        if not value:
            return

        message = await ctx.send(f"Deleting {plural(len(emojis)):emoji}...")

        deleted_emojis = []

        for emoji in emojis:
            deleted_emojis.append(f"`{emoji}`")
            await emoji.delete()
            await message.edit(
                content=f"Successfully deleted {human_join(deleted_emojis, final='and')} *({len(deleted_emojis)}/{len(emojis)})*."
            )

    @emoji_group.command(name="rename")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_rename(self, ctx: GuildContext, emoji: discord.Emoji, *, name: str):
        try:
            emoji = await emoji.edit(name=name)
            await ctx.send(f"Renamed {emoji}")
        except Exception as e:
            raise commands.BadArgument(f"Failed to rename emoji\n{e}")

    @commands.command(name="steal", aliases=("copy", "clone"))
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def steal(self, ctx: GuildContext, *, emojis: Optional[str]):
        """Clones emojis to the current server"""
        if ctx.ref:
            emojis = ctx.ref.content

        if not emojis:
            raise commands.BadArgument("No emojis found.")

        emoji_results = EMOJI_RE.findall(emojis)

        if not bool(emoji_results):
            raise commands.BadArgument("No emojis found.")

        message = await ctx.send("Stealing emojis...")

        completed_emojis = []
        for result in emoji_results:
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
                content=f'Successfully stole {human_join(completed_emojis, final="and")} *({len(completed_emojis)}/{len(emoji_results)})*.'
            )