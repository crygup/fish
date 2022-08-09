import argparse
import os
import re
import secrets
import shlex
import time
from io import BytesIO
from typing import Annotated, Dict, List, Optional, Union

import asyncpg
import discord
from bot import Bot, Context
from discord.ext import commands, tasks
from utils import (
    EmojiConverter,
    GuildContext,
    TenorUrlConverter,
    get_video,
    human_join,
    natural_size,
    to_thread,
    video_regexes,
    FieldPageSource,
    Pager,
)
from yt_dlp import YoutubeDL

from utils.paginator import SimplePages


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class TagName(commands.clean_content):
    def __init__(self, *, lower: bool = False):
        self.lower: bool = lower
        super().__init__()

    async def convert(self, ctx: Context, argument: str) -> str:
        converted = await super().convert(ctx, argument)
        lower = converted.lower().strip()

        if not lower:
            raise commands.BadArgument("Missing tag name.")

        if len(lower) > 100:
            raise commands.BadArgument("Tag name is a maximum of 100 characters.")

        first_word, _, _ = lower.partition(" ")

        # get tag command.
        root: commands.GroupMixin = ctx.bot.get_command("tag")  # type: ignore
        if first_word in root.all_commands:
            raise commands.BadArgument("This tag name starts with a reserved word.")

        return converted if not self.lower else lower


class Tools(commands.Cog, name="tools"):
    """Useful tools"""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.currently_downloading: list[str] = []

    async def cog_unload(self):
        self.delete_videos.cancel()

    async def cog_load(self) -> None:
        self.delete_videos.start()

    @commands.command(name="wordle", aliases=("word",))
    async def wordle(self, ctx: Context, *, flags: str):
        """This uses flags to solve a wordle problem.

        Use underscores to spcify blanks.
        Example: h_r___

        Flags:
            -jeyy    - Uses jeyybot wordle list (this is default true for the jeyybot wordle command)
            -correct - letters that are correct
            -invalid - letters that are not used
        """
        words = self.bot.words

        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument("-j", "-jeyy", action="store_true", default=True)
        parser.add_argument("-c", "-correct", type=str)
        parser.add_argument("-i", "-invalid", type=str)

        args = parser.parse_args(shlex.split(flags))

        if args.j:
            words = self.bot.jeyy_words

        word = ["_", "_", "_", "_", "_"]
        letters_to_skip = [letter for letter in args.i] if args.i else []

        letters_to_use = "abcdefghijklmnopqrstuvwxyz"

        if letters_to_skip:
            letters_to_use = re.sub(f"[{''.join(letters_to_skip)}]", "", letters_to_use)

        if args.c:
            if len(args.c) != 5:
                await ctx.send("The word must be 5 letters long.")
                return

            word = [letter for letter in args.c]
            for i, letter in enumerate(word):
                if letter == "_":
                    word[i] = f"[{letters_to_use}]{{1}}"
                else:
                    word[i] = f"[{letter}]{{1}}"

        pattern = re.compile("".join(word))

        guessed_words = [word for word in words if pattern.match(word)]

        if guessed_words == []:
            await ctx.send("No words found.")
            return

        formatted = guessed_words[:10]
        embed = discord.Embed(color=self.bot.embedcolor)
        embed.title = f"Possible words found {len(formatted)}/{len(guessed_words):,}"
        embed.description = human_join(
            [f"**`{word}`**" for word in formatted], final="and"
        )

        await ctx.send(embed=embed)

    @commands.command(name="steal", aliases=("clone",))
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def steal(self, ctx: GuildContext, *, emojis: Optional[str]):
        ref = ctx.message.reference
        content = ctx.message.content

        if emojis is None:
            if ref is None:
                await ctx.send(
                    "You need to provide some emojis to steal, either reply to a message or give them as an argument."
                )
                return

            resolved = ref.resolved
            if isinstance(resolved, discord.DeletedReferencedMessage):
                return

            if resolved is None:
                return

            content = resolved.content

        pattern = re.compile(r"<a?:[a-zA-Z0-9\_]{1,}:[0-9]{1,}>")
        results = pattern.findall(content)

        if len(results) == 0:
            await ctx.send("No emojis found.")
            return

        message = await ctx.send("Stealing emojis...")

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

    @to_thread
    def download_video(self, video: str, options: Dict):
        with YoutubeDL(options) as ydl:
            ydl.download(video)

    @commands.command(name="download", aliases=("dl",))
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def download(self, ctx: GuildContext, url: str, *, flags: Optional[str]):
        """Downloads a video from certain sites.

        Accepted sites are, Youtube, TikTok, Twitter, and reddit."""

        default_name = secrets.token_urlsafe(8)
        default_format = "mp4"
        skip_check = False
        audio = False

        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument("-format", type=str, default=default_format)
        parser.add_argument("-dev", action="store_true")

        if flags is not None:
            try:
                args = parser.parse_args(shlex.split(flags))
            except RuntimeError as e:
                await ctx.send(str(e))
                return

            if args.dev:
                check = await self.bot.is_owner(ctx.author)

                if not check:
                    raise commands.NotOwner

                skip_check = True

            if args.format is not None:
                if not re.match(r"(mp4|webm|mp3)", args.format):
                    await ctx.send("Invalid format.")
                    return

                if re.match(r"(mp3)", args.format):
                    audio = True
                    default_format = args.format
                else:
                    default_format = args.format

        if not skip_check:
            video = await get_video(ctx, url)

            if video is None:
                return await ctx.send("Invalid video url.")
        else:
            video = url

        pattern = re.compile(
            r"https://(www|vt|vm|m).tiktok.com/(@)?[a-zA-Z0-9_-]{3,}(/video/[0-9]{1,})?"
        )

        ydl_opts = {
            "format": f"bestvideo+bestaudio[ext={default_format}]/best"
            if not audio
            else f"bestaudio/best",
            "outtmpl": f"files/videos/{default_name}.%(ext)s",
            "quiet": True,
            "max_filesize": ctx.guild.filesize_limit,
        }

        if pattern.search(video):
            ydl_opts["format_sort"] = ["vcodec:h264"]

        if audio:
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]

        message = await ctx.reply("Downloading video")

        self.currently_downloading.append(f"{default_name}.{default_format}")

        start = time.perf_counter()
        await self.download_video(video, ydl_opts)
        stop = time.perf_counter()

        dl_time = (
            f"Took `{round(stop - start, 2)}` seconds to download, {ctx.author.mention}"
        )

        await message.edit(content="Downloaded, uploading...")

        try:
            _file = discord.File(f"files/videos/{default_name}.{default_format}")
            await message.edit(content=dl_time, attachments=[_file])

        except (ValueError, discord.Forbidden):
            await message.edit(content="Failed to download, try again later?")

        except (FileNotFoundError, discord.HTTPException):
            await message.edit(
                content=f"Video file size is too big, try a shorter video. This server's file size limit is **`{natural_size(ctx.guild.filesize_limit)}`**."
            )

        try:
            os.remove(f"files/videos/{default_name}.{default_format}")
        except (FileNotFoundError, PermissionError):
            pass

        self.currently_downloading.remove(f"{default_name}.{default_format}")

    @tasks.loop(minutes=10.0)
    async def delete_videos(self):
        valid_formats = (
            "mp4",
            "webm",
            "mov",
            "mp3",
            "ogg",
            "wav",
        )
        for file in os.listdir("files/videos"):
            if file.endswith(valid_formats):
                if file not in self.currently_downloading:
                    os.remove(f"files/videos/{file}")

    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji(
        self,
        ctx: Context,
        emoji: Optional[Union[discord.Emoji, discord.PartialEmoji, str]],
    ):
        """Gets information about an emoji."""

        if ctx.message.reference is None and emoji is None:
            raise commands.BadArgument("No emoji provided.")

        if isinstance(emoji, str):
            _emoji = await ctx.get_twemoji(str(emoji))

            if _emoji is None:
                raise commands.BadArgument("No emoji found.")

            await ctx.send(file=discord.File(BytesIO(_emoji), filename=f"emoji.png"))
            return

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

        if emoji is None:
            raise TypeError("No emoji found.")

        embed = discord.Embed(timestamp=emoji.created_at, title=emoji.name)

        if isinstance(emoji, discord.Emoji) and emoji.guild:
            if not emoji.available:
                embed.title = f"~~{emoji.name}~~"

            embed.add_field(
                name="Guild",
                value=f"{ctx.bot.e_replies}{str(emoji.guild)}\n"
                f"{ctx.bot.e_reply}{emoji.guild.id}",
            )

            femoji = await emoji.guild.fetch_emoji(emoji.id)
            if femoji.user:
                embed.add_field(
                    name="Created by",
                    value=f"{ctx.bot.e_replies}{str(femoji.user)}\n"
                    f"{ctx.bot.e_reply}{femoji.user.id}",
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

    @emoji.command(name="rename")
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

    @emoji.command(name="delete")
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: Context, *emojis: discord.Emoji):
        value = await ctx.prompt(
            f"Are you sure you want to delete {len(emojis):,} emoji{'s' if len(emojis) > 1 else ''}?"
        )
        if not value:
            await ctx.send(
                f"Well I didn't want to delete {'them' if len(emojis) > 1 else 'it'} anyway."
            )
            return

        message = await ctx.send(
            f"Deleting {len(emojis):,} emoji{'s' if len(emojis) > 1 else ''}..."
        )
        deleted_emojis = []

        for emoji in emojis:
            deleted_emojis.append(f"`{emoji}`")
            await emoji.delete()
            await message.edit(
                content=f"Successfully deleted {human_join(deleted_emojis, final='and')} *({len(deleted_emojis)}/{len(emojis)})*."
            )

    @commands.group(
        name="auto_download", aliases=("auto_dl", "adl"), invoke_without_command=True
    )
    async def auto_download(self, ctx: Context):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        results = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not results:
            message = "This server does not have an auto-download channel set yet."

            if (
                isinstance(ctx.author, discord.Member)
                and ctx.author.guild_permissions.manage_guild
            ):
                message += f"\nYou can set one with `{ctx.prefix}auto_download set`."

            await ctx.send(message)
            return

        channel = self.bot.get_channel(results)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        await ctx.send(f"Auto-download is set to {channel.mention}.")

    @auto_download.command(
        name="set", aliases=("create", "create_channel", "create_dl_channel")
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_download_set(
        self, ctx: Context, channel: Optional[discord.TextChannel]
    ):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        result = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if result is not None:
            await ctx.send(f"Auto-download is already setup here.")
            return

        if channel is None:
            if not ctx.me.guild_permissions.manage_channels:
                await ctx.send(
                    f"I cannot create a channel so you can either make one yourself or use `{ctx.prefix}auto_download set <channel>` to set an already made one."
                )
                return

            response = await ctx.prompt(
                "You didn't provide a channel so I will create one, is this okay?"
            )
            if response is None:
                await ctx.send(
                    f"Okay, I won't create a channel, instead specify one with `{ctx.prefix}auto_download set <channel>`."
                )
                return

            channel = await ctx.guild.create_text_channel(
                name=f"auto-download",
                topic="Valid links posted here will be auto downloaded. \nAccepted sites are, Youtube, TikTok, Twitter, and reddit.",
            )

            first_sql = """SELECT guild_id FROM guild_settings WHERE guild_id = $1"""
            results = await self.bot.pool.fetchval(first_sql, ctx.guild.id)

            sql = (
                """INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2)"""
                if not results
                else """UPDATE guild_settings SET auto_download = $2 WHERE guild_id = $1"""
            )

            await self.bot.pool.execute(sql, ctx.guild.id, channel.id)
            await self.bot.redis.sadd("auto_download_channels", channel.id)
            await ctx.send(f"Auto-download is now set to {channel.mention}.")
            return

        if not channel.permissions_for(ctx.me).send_messages:
            await ctx.send(
                f"I don't have permission to send messages in {channel.mention}."
            )
            return

        first_sql = """SELECT guild_id FROM guild_settings WHERE guild_id = $1"""
        results = await self.bot.pool.fetchval(first_sql, ctx.guild.id)

        sql = (
            """INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2)"""
            if not results
            else """UPDATE guild_settings SET auto_download = $2 WHERE guild_id = $1"""
        )

        await self.bot.pool.execute(sql, ctx.guild.id, channel.id)
        await self.bot.redis.sadd("auto_download_channels", channel.id)
        await ctx.send(f"Auto-download is now set to {channel.mention}.")

    @auto_download.command(name="remove", aliases=("delete",))
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_download_remove(self, ctx: Context):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        result = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not result:
            await ctx.send(
                "This server does not have an auto-download channel set yet."
            )
            return

        if not isinstance(result, int):
            return

        channel = self.bot.get_channel(result)
        if isinstance(channel, discord.TextChannel):
            results = await ctx.prompt(
                f"Are you sure you want to delete {channel.mention}?"
            )
            if not results:
                await ctx.send(f"Well I didn't want to delete it anyway.")
                return

        sql = """UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1"""
        await self.bot.pool.execute(sql, ctx.guild.id)
        await self.bot.redis.srem("auto_download_channels", result)
        await ctx.send(f"Removed auto-downloads for this server.")

    @commands.group(name="tag", invoke_without_command=True)
    async def tag(self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]):
        sql = """
        SELECT content FROM tags WHERE guild_id = $1 AND LOWER(name)=$2
        """
        result = await self.bot.pool.fetchval(sql, ctx.guild.id, name)
        if not result:
            await ctx.send(f"A tag with the name `{name}` does not exist.")
            return

        await ctx.send(result, reference=ctx.replied_reference)

        # updating use count
        sql = """
        UPDATE tags SET uses = uses + 1 WHERE guild_id = $1 AND LOWER(name)=$2"""
        await self.bot.pool.execute(sql, ctx.guild.id, name)

    @tag.command(name="create")
    async def tag_create(
        self,
        ctx: Context,
        name: Annotated[str, TagName],
        *,
        content: Optional[Annotated[str, commands.clean_content]],
    ):
        sql = """
        INSERT INTO tags (guild_id, author_id, name, content, created_at) 
        VALUES ($1, $2, $3, $4, $5)
        """

        if content is None:
            if ctx.message.attachments:
                content = ctx.message.attachments[0].url
            else:
                await ctx.send("content is a required argument that is missing.")
                return

        try:
            await self.bot.pool.execute(
                sql, ctx.guild.id, ctx.author.id, name, content, discord.utils.utcnow()
            )
        except asyncpg.exceptions.UniqueViolationError:
            await ctx.send(f"A tag with the name `{name}` already exists.")
            return

        await ctx.send(f"Tag `{name}` created.")

    @tag.command(name="delete")
    async def tag_delete(
        self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]
    ):

        bypass_owner_check = (
            ctx.author.id == self.bot.owner_id
            or ctx.author.guild_permissions.manage_messages
        )
        method = "WHERE LOWER(name) = $1 AND guild_id = $2"

        if bypass_owner_check:
            args = [name, ctx.guild.id]
        else:
            args = [name, ctx.guild.id, ctx.author.id]
            method += " AND author_id = $3"

        sql = f"""
        DELETE FROM tags {method} RETURNING name;
        """

        deleted = await self.bot.pool.fetchrow(sql, *args)
        if deleted is None:
            await ctx.send(
                "Could not delete tag. Either it does not exist or you do not have permissions to do so."
            )
            return

        await ctx.send(f"Tag `{name}` deleted.")

    @tag.command(name="edit")
    async def tag_edit(
        self,
        ctx: Context,
        name: Annotated[str, TagName(lower=True)],
        *,
        content: Annotated[str, commands.clean_content],
    ):
        sql = """
        UPDATE tags SET content = $3 WHERE LOWER(name) = $1 AND guild_id = $2 AND author_id = $4 RETURNING name;
        """

        updated = await self.bot.pool.fetchrow(
            sql, name, ctx.guild.id, content, ctx.author.id
        )

        if updated is None:
            await ctx.send(
                "Could not update tag. Either it does not exist or you do not have permissions to do so."
            )
            return

        await ctx.send(f"Tag `{name}` updated.")

    @tag.command(name="list")
    async def tag_list(self, ctx: Context, member: discord.Member = commands.Author):
        sql = """
        SELECT * FROM tags WHERE guild_id = $1 AND author_id = $2
        """
        results = await self.bot.pool.fetch(sql, ctx.guild.id, member.id)

        if not results:
            await ctx.send(f"{member.mention} has no tags.")
            return

        entries = [
            (
                r["name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Tags for {member} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @tag.command(name="all")
    async def tag_all(self, ctx: Context):
        sql = """
        SELECT * FROM tags WHERE guild_id = $1
        """
        results = await self.bot.pool.fetch(sql, ctx.guild.id)

        if not results:
            await ctx.send("This server has no tags.")
            return

        entries = [
            (
                r["name"],
                f'{(await self.bot.getch_user(r["author_id"])).mention} | {discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Tags in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @tag.command(name="info")
    async def tag_info(
        self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]
    ):
        sql = """
        SELECT * FROM tags WHERE LOWER(name) = $1 AND guild_id = $2
        """

        tag = await self.bot.pool.fetchrow(sql, name, ctx.guild.id)

        if tag is None:
            await ctx.send("Tag not found.")
            return

        embed = discord.Embed(color=self.bot.embedcolor)
        author = await self.bot.getch_user(tag["author_id"])
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        embed.add_field(
            name="Created",
            value=f'{discord.utils.format_dt(tag["created_at"], "R")}\n{discord.utils.format_dt(tag["created_at"], "d")}',
        )
        embed.add_field(name="Uses", value=f'{int(tag["uses"]):,}')

        await ctx.send(embed=embed)

    @tag.command(name="raw")
    async def tag_raw(self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]):
        sql = """
        SELECT * FROM tags WHERE LOWER(name) = $1 AND guild_id = $2
        """

        tag: asyncpg.Record = await self.bot.pool.fetchrow(sql, name, ctx.guild.id)

        if tag is None:
            await ctx.send("Tag not found.")
            return

        await ctx.send(
            discord.utils.escape_markdown(tag["content"]),
            reference=ctx.replied_reference,
        )

    @commands.command(name="tags")
    async def tags_command(
        self, ctx: Context, member: discord.Member = commands.Author
    ):
        sql = """
        SELECT * FROM tags WHERE guild_id = $1 AND author_id = $2
        """

        results = await self.bot.pool.fetch(sql, ctx.guild.id, member.id)

        if not results:
            await ctx.send(f"{member.mention} has no tags.")
            return

        entries = [
            (
                r["name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Tags for {member} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)
