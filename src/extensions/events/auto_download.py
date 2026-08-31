from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, cast

import discord
from discord import MediaGalleryItem, ui
from discord.ext import commands

from core import Cog, is_operational_guild
from core.handoff import HandoffNoticeThrottle, is_legacy_instance, send_handoff_notice
from utils import (
    KLIPY_RE,
    TENOR_PAGE_RE,
    VIDEOS_RE,
    Downloader,
    KlipyUrlConverter,
    TemporaryMediaError,
    TenorUrlConverter,
    record_download,
    to_image,
    upload_temporary_media,
)

if TYPE_CHECKING:
    from extensions.context import Context


class AutoDownload(Cog):
    cd_mapping = commands.CooldownMapping.from_cooldown(
        1, 5, commands.BucketType.member
    )

    def _handoff_throttle(self) -> HandoffNoticeThrottle:
        """Return the throttle shared by legacy auto-download warnings."""

        throttle = getattr(self.bot, "_handoff_notice_throttle", None)
        if not isinstance(throttle, HandoffNoticeThrottle):
            throttle = HandoffNoticeThrottle()
            # Store it on the bot so an extension reload does not reset the
            # warning throttle and spam the configured channel.
            setattr(self.bot, "_handoff_notice_throttle", throttle)
        return throttle

    @commands.Cog.listener("on_message")
    async def auto_download(self, message: discord.Message):
        if is_operational_guild(message):
            return
        if message.channel.id not in self.bot.db_cache.auto_downloads:
            return

        if message.author.bot:
            return

        # During migration the old application must not continue processing
        # automatic downloads.  A throttled notice keeps bursts of links from
        # spamming the channel while still exposing the replacement links.
        if is_legacy_instance(self.bot):
            channel_id = getattr(message.channel, "id", None)
            user_id = getattr(message.author, "id", None)
            if channel_id is not None and user_id is not None:
                if await self._handoff_throttle().allow(user_id, channel_id):
                    await send_handoff_notice(
                        self.bot,
                        message.channel,
                        guild=message.guild,
                    )
            return

        video_match = VIDEOS_RE.search(message.content)
        tenor_match = TENOR_PAGE_RE.search(message.content)
        klipy_match = KLIPY_RE.search(message.content)
        has_video_match = bool(video_match and video_match.group(0))

        if not tenor_match and not klipy_match and not has_video_match:
            return

        bucket = self.cd_mapping.get_bucket(message)

        if bucket:
            retry_after = bucket.update_rate_limit()

            if retry_after:
                return

        ctx: Context = await self.bot.get_context(message)  # type: ignore

        if tenor_match:
            try:
                url = await TenorUrlConverter().convert(ctx, message.content)
                img = cast(BytesIO, await to_image(ctx.session, url))
                downloader = Downloader(
                    ctx,
                    url,
                    auto_download=True,
                    allow_temporary_hosting=True,
                )
                if img.getbuffer().nbytes > downloader.max_filesize:
                    try:
                        hosted_url = await upload_temporary_media(
                            self.bot,
                            img.getvalue(),
                            "tenor.gif",
                            content_type="image/gif",
                        )
                    except TemporaryMediaError:
                        await ctx.send(
                            f"This GIF exceeds {downloader.upload_limit_description} "
                            "and temporary hosting is unavailable.",
                            ephemeral=True,
                        )
                        return
                    container = ui.Container(
                        ui.MediaGallery(MediaGalleryItem(hosted_url)),
                        ui.TextDisplay(
                            "-# Discord's upload limit was exceeded. "
                            "This link expires in 30 minutes."
                        ),
                        accent_color=self.bot.embedcolor,
                    )
                    view_type = type("HostedAutoDownloadView", (ui.LayoutView,), {})
                    view = view_type(timeout=None)
                    view.add_item(container)
                    await ctx.send(view=view, ephemeral=True)
                    await record_download(ctx, tenor_match.group(0), auto_download=True)
                    return
                await ctx.send(
                    file=discord.File(img, filename="tenor.gif"), ephemeral=True
                )
                await record_download(ctx, tenor_match.group(0), auto_download=True)

                return

            except commands.BadArgument:
                pass

        if klipy_match:
            try:
                url = await KlipyUrlConverter().convert(ctx, klipy_match.group(0))
                async with ctx.typing(ephemeral=True):
                    await Downloader(
                        ctx,
                        url,
                        format="gif",
                        auto_download=True,
                        allow_temporary_hosting=True,
                    ).download()
                return
            except commands.BadArgument:
                pass

        async with ctx.typing(ephemeral=True):
            if not video_match or not video_match.group(0):
                return
            dl = Downloader(
                ctx,
                video_match.group(0),
                auto_download=True,
                allow_temporary_hosting=True,
            )

            await dl.download()
