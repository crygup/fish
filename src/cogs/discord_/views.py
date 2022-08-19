from utils import AuthorView
import discord
from typing import List, Union
from bot import Bot, Context, re
from discord.ext import commands


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

    @discord.ui.button(label="Avatar", row=0, style=discord.ButtonStyle.blurple)
    async def avatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user.avatar is None:
            return

        self.asset = self.user.avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Default", row=0, style=discord.ButtonStyle.blurple)
    async def default(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.asset = self.user.default_avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Server", row=0, style=discord.ButtonStyle.blurple)
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

    @discord.ui.button(label="Quality", row=2, style=discord.ButtonStyle.green)
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

    @discord.ui.button(label="Format", row=2, style=discord.ButtonStyle.green)
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

    @discord.ui.button(label="Save", row=2, style=discord.ButtonStyle.green)
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

    @discord.ui.button(label="Go back", row=1, style=discord.ButtonStyle.red)
    async def go_back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
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

    @discord.ui.button(label="Go back", row=1, style=discord.ButtonStyle.red)
    async def go_back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
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
        original_embed: discord.Embed,
    ):
        self.user = user
        self.ctx: Context = ctx
        self.original_embed = original_embed

        # Set the options that will be presented inside the dropdown
        options = [
            discord.SelectOption(
                label="Index",
                description=f"Goes back to home page",
                emoji="\U0001f3e0",
                value="index",
            ),
        ]
        member_options = [
            discord.SelectOption(
                label="Roles",
                description=f"{user.name}'s roles",
                emoji="\U0001f9fb",
                value="roles",
            ),
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

        if user.bot:
            options.extend(bot_options)

        if isinstance(user, discord.Member):
            options.extend(member_options)

        super().__init__(min_values=1, max_values=1, options=options)

    async def role_callback(
        self, member: discord.Member, ctx: Context
    ) -> discord.Embed:
        roles = sorted(member.roles, key=lambda role: role.position, reverse=True)
        roles = [
            role.mention if role.id != ctx.guild.id else "`@everyone`" for role in roles
        ]
        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(roles),
        )
        embed.set_author(
            name=f"{member.name}'s roles", icon_url=member.display_avatar.url
        )
        embed.set_footer(text="\u2800" * 47)
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

        if ctx.guild is None:
            raise commands.GuildNotFound("Unknown guild")

        if self.values[0] == "roles":
            if not isinstance(member, discord.Member):
                return
            embed = await self.role_callback(member, ctx)

        elif self.values[0] == "devices":
            if not isinstance(member, discord.Member):
                return
            embed = await self.device_callback(member, ctx)

        elif self.values[0] == "perms":
            if not isinstance(member, discord.Member):
                return
            embed = await self.perms_callback(member, ctx)

        elif self.values[0] == "botinfo":
            embed = await self.botinfo_callback(member, ctx)

        else:
            embed = self.original_embed

        await interaction.response.edit_message(embed=embed)


class UserInfoView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        original_embed: discord.Embed,
    ):
        super().__init__(ctx)
        self.ctx = ctx
        self.user = user
        if isinstance(user, discord.Member) or user.bot:
            self.add_item(UserInfoDropdown(ctx, user, original_embed))

        if not isinstance(user, discord.Member):
            self.nicknames.disabled = True

        self.avyh_check: bool = False
        self.usernames_check: bool = False
        self.nicknames_check: bool = False

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, __
    ) -> None:
        if isinstance(error, commands.BadArgument):
            await interaction.response.send_message(content=str(error), ephemeral=True)
        else:
            await interaction.response.send_message(
                "Oops! Something went wrong.", ephemeral=True
            )

        self.ctx.bot.logger.error(error)

    @discord.ui.button(label="Avatar History", row=2, style=discord.ButtonStyle.blurple)
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

    @discord.ui.button(
        label="Username History", row=2, style=discord.ButtonStyle.blurple
    )
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

    @discord.ui.button(
        label="Nickname History", row=2, style=discord.ButtonStyle.blurple
    )
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
