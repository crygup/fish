from __future__ import annotations

import random
import re
import textwrap
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Union, Any

import discord
from discord.ext import commands

from ..helpers import to_bytesio, to_thread, RockPaperScissors as RPS
from ..vars import BURPLE, CHECK, GREEN, RED, TRASH

if TYPE_CHECKING:
    from cogs.context import Context


class AuthorView(discord.ui.View):
    def __init__(self, ctx: Context, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)

        self.message: Optional[discord.Message] = None
        self.ctx = ctx

    def disable_all(self) -> None:
        for button in self.children:
            if isinstance(button, discord.ui.Button):
                button.disabled = True
            if isinstance(button, discord.ui.Select):
                button.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message:
            await self.message.edit(view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user and interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message(
            f'You can\'t use this, sorry. \nIf you\'d like to use this then run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ""}`',
            ephemeral=True,
        )
        return False


class AvatarView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: discord.Member | discord.User,
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        super().__init__(ctx)
        self.user = user
        self.ctx = ctx
        self.embed = embed
        self.asset = asset
        self.files: List[discord.File] = []

        self.server.disabled = (
            not isinstance(user, discord.Member)
            or isinstance(user, discord.Member)
            and user.guild_avatar is None
        )
        self.avatar.disabled = user.avatar is None

    async def edit_message(self, interaction: discord.Interaction):
        self.embed.set_image(url=self.asset.url)
        await interaction.response.edit_message(embed=self.embed)

    @discord.ui.button(label="Avatar", row=0, style=BURPLE)
    async def avatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user.avatar is None:
            return

        self.asset = self.user.avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Default", row=0, style=BURPLE)
    async def default(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.asset = self.user.default_avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Server", row=0, style=BURPLE)
    async def server(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(self.user, discord.Member) or self.user.guild_avatar is None:
            return

        self.asset = self.user.guild_avatar

        await self.edit_message(interaction)

    async def check_asset(self, interaction: discord.Interaction) -> bool:
        if self.asset.url.startswith("https://cdn.discordapp.com/embed"):
            await interaction.response.send_message(
                "This is a default discord avatar and cannot be changed.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="Quality", row=2, style=GREEN)
    async def quality(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        results = await self.check_asset(interaction)
        if interaction.message is None or not results:
            return

        await interaction.message.edit(
            view=QualityView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()

    @discord.ui.button(label="Format", row=2, style=GREEN)
    async def _format(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        results = await self.check_asset(interaction)
        if interaction.message is None or not results:
            return

        await interaction.message.edit(
            view=FormatView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()

    @discord.ui.button(label="Save", row=2, style=GREEN)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.message is None:
            return

        file_type = re.search(r".(jpg|png|webp|jpeg|gif)", self.asset.url)

        if file_type is None:
            return

        avatar_file = await self.asset.to_file(filename=f"avatar.{file_type.group()}")
        self.embed.set_image(url=f"attachment://avatar.{file_type.group()}")
        await interaction.message.edit(
            view=None, embed=self.embed, attachments=[avatar_file]
        )
        await interaction.response.defer()


class QualityView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        super().__init__(ctx)
        self.user = user
        self.ctx = ctx
        self.embed = embed
        self.asset = asset
        self.add_item(QualityDropdown(ctx, user, embed, asset))

    @discord.ui.button(label="Go back", row=1, style=RED)
    async def go_back(self, interaction: discord.Interaction, _):
        if interaction.message is None:
            return

        await interaction.message.edit(
            view=AvatarView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()


class QualityDropdown(discord.ui.Select):
    view: QualityView

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        self.ctx = ctx
        self.user = user
        self.embed = embed

        valid_sizes = ["16", "32", "64", "128", "256", "512", "1024", "2048", "4096"]
        options = [
            discord.SelectOption(label=f"{size}px", value=size) for size in valid_sizes
        ]

        super().__init__(
            min_values=1, max_values=1, options=options, placeholder="Select a size"
        )

    async def callback(self, interaction: discord.Interaction):
        value = int(self.values[0])

        self.view.asset = self.view.asset.with_size(value)
        if interaction.message is None:
            return

        self.embed.set_image(url=self.view.asset.url)

        await interaction.message.edit(embed=self.embed)
        await interaction.response.defer()


class FormatView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        super().__init__(ctx)
        self.user = user
        self.ctx = ctx
        self.embed = embed
        self.asset = asset
        self.add_item(FormatDropdown(ctx, user, embed, asset))

    @discord.ui.button(label="Go back", row=1, style=RED)
    async def go_back(self, interaction: discord.Interaction, _):
        if interaction.message is None:
            return

        await interaction.message.edit(
            view=AvatarView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()


class FormatDropdown(discord.ui.Select):
    view: FormatView

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        self.ctx = ctx
        self.user = user
        self.embed = embed
        self.asset = asset

        valid_formats = ["webp", "jpeg", "jpg", "png"]

        if asset.is_animated():
            valid_formats.append("gif")

        options = [
            discord.SelectOption(label=_format, value=_format)
            for _format in valid_formats
        ]

        super().__init__(
            min_values=1, max_values=1, options=options, placeholder="Select a format"
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]

        self.view.asset = self.view.asset.with_format(value)  # type: ignore
        if interaction.message is None:
            return

        self.embed.set_image(url=self.view.asset.url)

        await interaction.message.edit(embed=self.embed)
        await interaction.response.defer()


class UserInfoDropdown(discord.ui.Select):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        fetched_user: discord.User,
        original_embed: discord.Embed,
    ):
        self.user = user
        self.ctx: Context = ctx
        self.fetched_user = fetched_user
        self.original_embed = original_embed

        # Set the options that will be presented inside the dropdown
        options = [
            discord.SelectOption(
                label="Index",
                description=f"Goes back to home page",
                emoji="\U0001f3e0",
                value="index",
            ),
            discord.SelectOption(
                label="Avatar",
                description=f"{user.name}'s avatar",
                emoji="\U0001f3a8",
                value="avatar",
            ),
        ]
        member_options = [
            discord.SelectOption(
                label="Devices",
                description=f"{user.name}'s current devices",
                emoji="\U0001f5a5",
                value="devices",
            ),
            discord.SelectOption(
                label="Permissions",
                description=f"{user.name}'s permissions",
                emoji="<:certified_moderator:949147443264622643>",
                value="perms",
            ),
        ]
        bot_options = [
            discord.SelectOption(
                label="Bot Specific",
                description=f"{user.name}'s bot information",
                emoji="\U0001f916",
                value="botinfo",
            ),
        ]

        if fetched_user.banner:
            options.append(
                discord.SelectOption(
                    label="Banner",
                    description=f"{user.name}'s banner",
                    emoji="\U0001f5bc\U0000fe0f",
                    value="banner",
                )
            )

        if user.bot:
            options.extend(bot_options)

        if isinstance(user, discord.Member):
            options.extend(member_options)

        super().__init__(min_values=1, max_values=1, options=options)

    async def avatar_callback(
        self, user: Union[discord.Member, discord.User], ctx: Context
    ) -> discord.Embed:
        images = [f"[Default Avatar]({user.default_avatar.url})"]

        if user.avatar:
            images.append(f"[Avatar]({user.avatar.url})")

        if isinstance(user, discord.Member) and user.guild_avatar:
            images.append(f"[Guild avatar]({user.guild_avatar.url})")

        embed = discord.Embed(color=ctx.bot.embedcolor, description=", ".join(images))
        embed.set_author(name=f"{user.name}'s avatar", icon_url=user.display_avatar.url)
        embed.set_image(url=user.display_avatar.url)
        embed.set_footer(text=f"Run fish pfp for more details")
        return embed

    async def banner_callback(self, __, ctx: Context) -> discord.Embed:
        user = self.fetched_user
        assert user.banner is not None

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name=f"{user.name}'s banner", icon_url=user.display_avatar.url)
        embed.set_image(url=user.banner.url)
        return embed

    async def device_callback(
        self, member: discord.Member, ctx: Context
    ) -> discord.Embed:
        device_types = {
            "dnd": "Do Not Disturb",
            "idle": "Idle",
            "offline": "Offline",
            "online": "Online",
        }

        devices = [
            f"**Desktop**: {device_types[member.desktop_status.value]}",
            f"**Mobile**: {device_types[member.mobile_status.value]}",
            f"**Web**: {device_types[member.web_status.value]}",
        ]

        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(devices),
        )
        embed.set_author(
            name=f"{member.name}'s devices", icon_url=member.display_avatar.url
        )
        embed.set_footer(text="\u2800" * 47)
        return embed

    async def perms_callback(
        self, member: discord.Member, ctx: Context
    ) -> discord.Embed:
        permissions = member.guild_permissions

        allowed = []
        for name, value in permissions:
            name = name.replace("_", " ").replace("guild", "server").title()
            if value:
                allowed.append(name)

        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(allowed),
        )
        embed.set_author(
            name=f"{member.name}'s permissions", icon_url=member.display_avatar.url
        )

        embed.set_footer(text="\u2800" * 47)
        return embed

    async def botinfo_callback(
        self, user: Union[discord.Member, discord.User], ctx: Context
    ) -> discord.Embed:
        url = f"https://discord.com/api/v10/oauth2/applications/{user.id}/rpc"
        async with self.ctx.bot.session.get(url) as r:
            if r.status != 200:
                raise commands.BadArgument(f"Unable to fetch info on {user}")

            results = await r.json()

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(
            name=f"{user.name}'s bot information", icon_url=user.display_avatar.url
        )
        embed.set_footer(text="\u2800" * 47)

        embed.description = results["description"]

        embed.add_field(
            name="Bot Info",
            value=f"Public: {ctx.yes_no(results['bot_public'])}\n"
            f"Require Code Grant: {ctx.yes_no(results['bot_require_code_grant'])}\n",
        )
        if results.get("tags"):
            embed.add_field(
                name="Tags",
                value="\n".join(
                    f"`{tag}`"
                    for tag in sorted(
                        results["tags"], key=lambda x: len(x), reverse=True
                    )
                ),
            )

        admin_perms = discord.Permissions.none()
        admin_perms.administrator = True
        invite_perms = [
            ("Advanced", discord.Permissions.advanced()),
            ("None", discord.Permissions.none()),
            ("All", discord.Permissions.all()),
            ("General", discord.Permissions.general()),
            ("Administrator", admin_perms),
        ]

        embed.add_field(
            name="Invites",
            value=f"\n".join(
                f"[`{name}`]({discord.utils.oauth_url(user.id, permissions=perms)})"
                for name, perms in sorted(
                    invite_perms, key=lambda x: len(x[0]), reverse=True
                )
            ),
        )

        return embed

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        member = self.user

        choice = self.values[0]
        choices: Dict[str, Callable] = {
            "devices": self.device_callback,
            "perms": self.perms_callback,
            "botinfo": self.botinfo_callback,
            "avatar": self.avatar_callback,
            "banner": self.banner_callback,
        }

        if choice in [key for key, _ in choices.items()]:
            embed = await choices[choice](member, ctx)  # type: ignore # wont be user
        else:
            embed = self.original_embed

        await interaction.response.edit_message(embed=embed)


class UserInfoView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        fetched_user: discord.User,
        original_embed: discord.Embed,
    ):
        super().__init__(ctx)
        self.ctx = ctx
        self.user = user
        if isinstance(user, discord.Member) or user.bot:
            self.add_item(UserInfoDropdown(ctx, user, fetched_user, original_embed))

        if not isinstance(user, discord.Member):
            self.nicknames.disabled = True

        self.avyh_check: bool = False
        self.usernames_check: bool = False
        self.nicknames_check: bool = False

    @discord.ui.button(label="Avatar History", row=2, style=BURPLE)
    async def avyh(self, interaction: discord.Interaction, __):
        command = self.ctx.bot.get_command("avyh")
        if command is None:
            return

        if self.avyh_check:
            await interaction.response.defer()
            return

        self.avyh_check = True
        await interaction.response.defer()
        await command(self.ctx, user=self.user)

    @discord.ui.button(label="Username History", row=2, style=BURPLE)
    async def usernames(self, interaction: discord.Interaction, __):
        command = self.ctx.bot.get_command("usernames")
        if command is None:
            return

        if self.usernames_check:
            await interaction.response.defer()
            return

        self.usernames_check = True
        await interaction.response.defer()
        await command(self.ctx, user=self.user)

    @discord.ui.button(label="Nickname History", row=2, style=BURPLE)
    async def nicknames(self, interaction: discord.Interaction, __):
        command = self.ctx.bot.get_command("nicknames")
        if command is None:
            return

        if self.nicknames_check:
            await interaction.response.defer()
            return

        self.nicknames_check = True
        await interaction.response.defer()
        await command(self.ctx, user=self.user)


class DeleteView(AuthorView):
    def __init__(self, ctx: Context):
        super().__init__(ctx)

    @discord.ui.button(emoji=TRASH, style=RED)
    async def delete(self, interaction: discord.Interaction, _):
        if interaction.message:
            await interaction.message.delete()

        try:
            await self.ctx.message.add_reaction(CHECK)
        except:  # blank except because this failing will never raise any suspicion
            pass


class RPSView(AuthorView):
    bot_choice = random.choice((RPS("rock"), RPS("paper"), RPS("scissors")))

    async def send_result(self, choice: RPS, interaction: discord.Interaction):
        assert interaction.message is not None
        await interaction.message.edit(
            content=f"You {['lose', 'win'][choice > self.bot_choice]}! I chose {self.bot_choice}.",
            view=None,
        )

    @discord.ui.button(label="Rock", emoji="\U0001faa8")
    async def rock_button(self, interaction: discord.Interaction, _):
        await self.send_result(RPS("rock"), interaction)

    @discord.ui.button(label="Paper", emoji="\U0001f4c4")
    async def paper_button(self, interaction: discord.Interaction, _):
        await self.send_result(RPS("paper"), interaction)

    @discord.ui.button(label="Scissors", emoji="\U00002702\U0000fe0f")
    async def scissors_button(self, interaction: discord.Interaction, _):
        await self.send_result(RPS("scissors"), interaction)


class CoverView(AuthorView):
    def __init__(self, ctx: Context, data: Dict):
        self.data = data
        super().__init__(ctx)

    @discord.ui.button(label="Mark NSFW", style=discord.ButtonStyle.red)
    async def mark_nsfw(self, interaction: discord.Interaction, __):
        sql = """INSERT INTO nsfw_covers(album_id) VALUES ($1)"""
        cover_id = self.data["albums"]["items"][0]["id"]
        bot = self.ctx.bot
        await bot.pool.execute(sql, cover_id)
        await bot.redis.sadd("nsfw_covers", cover_id)

        if interaction.message is None:
            return

        await interaction.message.edit(
            content=f"Successfully marked `{cover_id}` as NSFW.",
            attachments=[
                await interaction.message.attachments[0].to_file(spoiler=True)
            ],
        )
        await interaction.response.defer()

    @discord.ui.button(label="Unmark NSFW", style=discord.ButtonStyle.green)
    async def unmark_nsfw(self, interaction: discord.Interaction, __):
        sql = """DELETE FROM nsfw_covers WHERE album_id = $1"""
        cover_id = self.data["albums"]["items"][0]["id"]
        bot = self.ctx.bot
        await bot.pool.execute(sql, cover_id)
        await bot.redis.srem("nsfw_covers", cover_id)

        if interaction.message is None:
            return

        await interaction.message.edit(
            content=f"Successfully unmarked `{cover_id}` as NSFW.",
            attachments=[
                await interaction.message.attachments[0].to_file(spoiler=False)
            ],
        )
        await interaction.response.defer()


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

    def do_we_add_s(self, number: int) -> str:
        return "s" if number > 1 or number == 0 else ""

    async def update_data(self) -> discord.Embed:
        correct_name = str(self.data["Data"]["name"]).lower()
        given_name = self.pokemon.value.lower()
        correct: bool = correct_name == given_name
        embed = discord.Embed(color=0x008341 if correct else 0xFF0000)
        embed.set_author(name="Correct!" if correct else "Incorrect!")

        data = await self.ctx.bot.pool.fetchrow(
            "SELECT * FROM pokemon_guesses WHERE pokemon_name = $1 AND author_id = $2",
            correct_name,
            self.ctx.author.id,
        )

        if data:
            sql = f"""
            UPDATE pokemon_guesses SET {'correct' if correct else 'incorrect'} = {'correct' if correct else 'incorrect'} + 1 WHERE pokemon_name = $1 AND author_id = $2 RETURNING *
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


class WTPView(AuthorView):
    def __init__(self, ctx: Context, data: dict[str, str]):
        super().__init__(ctx)
        self.ctx = ctx
        self.data = data

    @discord.ui.button(label="click me")
    async def modal(self, interaction: discord.Interaction, __):
        await interaction.response.send_modal(WTPModal(self.ctx, self.data))
