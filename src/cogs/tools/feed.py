from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import tweepy
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Bot, Context

from ._base import CogBase


class FeedCommands(CogBase):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.group(name="feed", invoke_without_command=True)
    async def feed_group(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @feed_group.group(name="twitter", invoke_without_command=True)
    async def twitter_group(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @twitter_group.command(name="follow")
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def twitter_follow(
        self, ctx: Context, user: str, *, channel: discord.TextChannel
    ):
        twitter_user = await ctx.bot.twitter.get_user(
            username=user, user_fields=["profile_image_url"]
        )
        user_id = twitter_user.data.id  # type: ignore # shut up
        username = twitter_user.data.username  # type: ignore # shut up
        await self.bot.live_twitter.add_rules(tweepy.StreamRule(f"from:{username}"))
        async with self.bot.session.get(twitter_user.data.profile_image_url) as resp:  # type: ignore # shut up
            profile_picture = await resp.read()

        await self.bot.redis.sadd(f"twitter_feed:{user_id}", channel.id)

        webhook = await channel.create_webhook(name=username, avatar=profile_picture)

        try:
            self.bot.feed_webhooks[user_id].append(webhook)
        except KeyError:
            self.bot.feed_webhooks[user_id] = [webhook]

        SQL = """
        INSERT INTO twitter_feed
        (tweeter_id, guild_id, channel_id, webhook, include_replies, created_at, author_id)
        VALUES($1, $2, $3, $4, $5, $6, $7)
        """
        await self.bot.pool.execute(
            SQL,
            user_id,
            channel.guild.id,
            channel.id,
            webhook.url,
            True,
            discord.utils.utcnow(),
            ctx.author.id,
        )

        await ctx.send(
            f"Alright, from now on {username}'s tweets will be sent in {channel.mention}."
        )

    @twitter_group.command(name="unfollow")
    async def twitter_unfollow(
        self, ctx: Context, user: str, *, channel: discord.TextChannel
    ):
        twitter_user = await ctx.bot.twitter.get_user(
            username=user, user_fields=["profile_image_url"]
        )
        user_id = twitter_user.data.id  # type: ignore
        username = twitter_user.data.username  # type: ignore
        results = await self.bot.pool.fetchrow(
            "DELETE FROM twitter_feed WHERE tweeter_id = $1 AND channel_id = $2 AND guild_id = $3 RETURNING *",
            user_id,
            channel.id,
            channel.guild.id,
        )

        if not results:
            raise ValueError(
                "Unable to find that twitter user with the specified channel"
            )

        try:
            webhook = discord.Webhook.from_url(
                results["webhook"], session=ctx.bot.session
            )
            self.bot.feed_webhooks[user_id].remove(webhook)
        except:  # we dont care if it errors because that means we cant delete it or it doesnt exist, so ignoring is the best way to handle this
            pass

        await ctx.send(f"Successfully stopped following {username}'s tweets")
