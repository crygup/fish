from __future__ import annotations

import re
from io import BytesIO
from typing import TYPE_CHECKING, Annotated, Optional, Union

import discord
from discord.ext import commands

from utils import (
    REPLIES,
    REPLY,
    BlankException,
    EmojiConverter,
    NotTenorUrl,
    TenorUrlConverter,
    TwemojiConverter,
    emoji_extras,
    human_join,
    plural,
    ImageConverter,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Emojis(CogBase):
    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji(
        self,
        ctx: Context,
        emoji: Optional[Union[discord.Emoji, discord.PartialEmoji, TwemojiConverter]],
    ):
        """Gets information about an emoji."""

        if ctx.message.reference is None and emoji is None:
            raise commands.BadArgument("No emoji provided.")

        if isinstance(emoji, BytesIO):
            return await ctx.send(file=discord.File(emoji, filename=f"emoji.png"))

        if emoji:
            emoji = emoji

        else:
            if not ctx.message.reference:
                raise commands.BadArgument("No emoji provided.")

            reference = ctx.message.reference.resolved

            if (
                isinstance(reference, discord.DeletedReferencedMessage)
                or reference is None
            ):
                raise commands.BadArgument("No emoji found.")

            emoji = (await EmojiConverter().from_message(ctx, reference.content))[0]

        if emoji is None or isinstance(
            emoji, TwemojiConverter
        ):  # having TwemojiConverter check here for typing reasons, will always be str
            raise BlankException("No emoji found.")

        embed = discord.Embed(timestamp=emoji.created_at, title=emoji.name)

        if isinstance(emoji, discord.Emoji) and emoji.guild:
            if not emoji.available:
                embed.title = f"~~{emoji.name}~~"

            embed.add_field(
                name="Guild",
                value=f"{REPLIES}{str(emoji.guild)}\n" f"{REPLY}{emoji.guild.id}",
            )

            femoji = await emoji.guild.fetch_emoji(emoji.id)
            if femoji.user:
                embed.add_field(
                    name="Created by",
                    value=f"{REPLIES}{str(femoji.user)}\n" f"{REPLY}{femoji.user.id}",
                )

        embed.add_field(
            name="Raw text", value=f"`<:{emoji.name}\u200b:{emoji.id}>`", inline=False
        )

        embed.set_footer(text=f"ID: {emoji.id} \nCreated at")
        embed.set_image(url=f'attachment://emoji.{"gif" if emoji.animated else "png"}')
        file = await emoji.to_file(
            filename=f'emoji.{"gif" if emoji.animated else "png"}'
        )
        await ctx.send(embed=embed, file=file)

    @emoji.command(name="create", extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_create(
        self,
        ctx: Context,
        name,
        *,
        image: Annotated[bytes, ImageConverter],
    ):
        try:
            emoji = await ctx.guild.create_custom_emoji(
                name=name,
                image=image,
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as e:
            return await ctx.send(str(e))

        await ctx.send(f"Successfully created {emoji}")

    @emoji.command(name="rename", extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_rename(self, ctx: Context, emoji: discord.Emoji, *, name: str):
        pattern = re.compile(r"[a-zA-Z0-9_ ]")
        if not pattern.match(name):
            raise commands.BadArgument(
                "Name can only contain letters, numbers, and underscores."
            )

        await emoji.edit(name=name)
        await ctx.send(f"Renamed {emoji} to **`{name}`**.")

    @emoji.command(name="delete", extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: Context, *emojis: discord.Emoji):
        value = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(emojis)):emoji}?"
        )
        if not value:
            await ctx.send(
                f"Well I didn't want to delete {'them' if len(emojis) > 1 else 'it'} anyway."
            )
            return

        message = await ctx.send(f"Deleting {plural(len(emojis)):emoji}...")

        if message is None:
            return

        deleted_emojis = []

        for emoji in emojis:
            deleted_emojis.append(f"`{emoji}`")
            await emoji.delete()
            await message.edit(
                content=f"Successfully deleted {human_join(deleted_emojis, final='and')} *({len(deleted_emojis)}/{len(emojis)})*."
            )
