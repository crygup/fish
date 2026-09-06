from __future__ import annotations

import asyncio
import datetime
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Optional, cast

import discord
from discord.abc import Messageable
from discord.ext import commands, tasks

from core import Cog, is_operational_guild

if TYPE_CHECKING:
    pass


class Guilds(Cog):
    # Discord does not expose an application-install event to the gateway.
    # Watching for the replacement bot as a member gives the legacy process a
    # deterministic hand-off path while both applications are online.
    LEGACY_BOT_ID = 876391494485950504
    REPLACEMENT_BOT_ID = 1537535633038381190
    MAX_UNIQUE_USERS = 8_500

    allowed_guilds = [1159760133895766049]
    # This user is trusted to add Fishie to small guilds.  The ID is checked
    # against the recent bot-add audit entry before the membership threshold.
    allowed_adders = {427951571025002497}

    def __init__(self) -> None:
        # Guilds is mixed into the single Events cog.  Keep these sets on the
        # cog rather than in module globals so a reload does not leak state
        # between bot instances.
        super().__init__()
        self._capacity_baseline_guilds: set[int] = set()
        self._capacity_baseline_ready = False
        self._capacity_exempt_guilds: set[int] = set()
        self._legacy_handoff_left: set[int] = set()

    def _current_bot_id(self) -> int | None:
        # Prefer the identity selected by the launcher.  ``active_bot_id``
        # and ``configured_bot_id`` are properties on the production bot;
        # checking them first keeps hand-off decisions correct before
        # Discord has populated ``Client.user``.
        for attribute in ("active_bot_id", "configured_bot_id"):
            configured = getattr(self.bot, attribute, None)
            if configured is None:
                continue
            try:
                configured = configured() if callable(configured) else configured
                return int(configured)
            except (TypeError, ValueError):
                continue
        user = getattr(self.bot, "user", None)
        if user is not None:
            try:
                return int(user.id)
            except (TypeError, ValueError):
                pass
        try:
            ids = self.bot.config.get("ids", {})
            marker = getattr(self.bot, "is_new_bot", None)
            if marker is not None:
                try:
                    if bool(marker() if callable(marker) else marker):
                        return int(ids.get("new_bot_id") or self.REPLACEMENT_BOT_ID)
                except Exception:
                    pass
            return int(ids.get("bot_id") or self.LEGACY_BOT_ID)
        except (KeyError, TypeError, ValueError):
            return None

    def _is_replacement_bot(self) -> bool:
        marker = getattr(self.bot, "is_new_bot", None)
        if marker is not None:
            try:
                return bool(marker() if callable(marker) else marker)
            except Exception:
                pass
        return self._current_bot_id() == self.REPLACEMENT_BOT_ID

    def _is_legacy_bot(self) -> bool:
        explicit = getattr(self.bot, "is_legacy_bot", None)
        if explicit is not None:
            try:
                return bool(explicit() if callable(explicit) else explicit)
            except Exception:
                pass
        return self._current_bot_id() == self.LEGACY_BOT_ID

    @staticmethod
    def _member_present(guild: discord.Guild, member_id: int) -> bool:
        """Return whether a member ID is visible in a guild's local cache."""

        try:
            if guild.get_member(member_id) is not None:
                return True
        except (AttributeError, TypeError):
            pass
        for member in list(getattr(guild, "members", ()) or ()):
            try:
                if int(getattr(member, "id", 0)) == member_id:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    async def _member_present_remotely(
        self, guild: discord.Guild, member_id: int
    ) -> bool:
        """Check a bot member once when the guild cache is incomplete.

        Guild-create payloads may contain only the current bot when member
        intents are unavailable. A one-off fetch on the hand-off path keeps
        an existing legacy membership from being treated as a new guild (and
        vice versa), without making the hourly capacity scan API-heavy.
        """

        if self._member_present(guild, member_id):
            return True
        try:
            await guild.fetch_member(member_id)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            AttributeError,
        ):
            return False
        return True

    def _replacement_member_present(self, guild: discord.Guild) -> bool:
        """Return whether the replacement app is present in *guild*.

        Bot members are normally included in guild-create/member events even
        when privileged member chunking is unavailable.  The cache iteration
        fallback keeps this reliable for partial guild doubles in tests.
        """

        replacement_id = self.REPLACEMENT_BOT_ID
        marker = getattr(self.bot, "new_bot_id", None)
        if marker is not None:
            try:
                value = cast(Any, marker)() if callable(marker) else marker
                if isinstance(value, (str, int)):
                    replacement_id = int(value)
            except (TypeError, ValueError):
                pass
        return self._member_present(guild, replacement_id)

    def _bot_label(self) -> str:
        """Human-readable instance label for lifecycle logs."""

        if self._is_replacement_bot():
            return "Replacement Fishie"
        if self._is_legacy_bot():
            return "Legacy Fishie"
        return "Fishie"

    def _is_capacity_exempt(self, guild: discord.Guild) -> bool:
        """Return whether a guild is exempt from replacement capacity checks.

        The operations guild is never part of the public capacity pool.  A
        guild that already contains the legacy bot is also exempt: this is
        the migration hand-off case where the administrator explicitly
        allowed the replacement to join an existing Fishie server.  Keep the
        exemption after the legacy bot leaves so the hourly pass cannot evict
        a guild during the hand-off.
        """

        if is_operational_guild(guild):
            return True
        guild_id = int(guild.id)
        if guild_id in self._capacity_exempt_guilds:
            return True
        if self._member_present(guild, self.LEGACY_BOT_ID):
            self._capacity_exempt_guilds.add(guild_id)
            return True
        return False

    @classmethod
    def _max_unique_humans(cls, guilds: object) -> int:
        """Return a safe upper bound for unique human members in *guilds*.

        Member caches can be partial immediately after a guild join.  Count
        known non-bot IDs exactly and conservatively account for uncached
        members using ``member_count`` so an over-capacity guild cannot slip
        through merely because chunking has not completed yet.
        """

        if not isinstance(guilds, (list, tuple, set, frozenset)):
            return 0
        known: set[int] = set()
        uncached = 0
        seen_guilds: set[int] = set()
        for guild in guilds:
            if is_operational_guild(guild):
                continue
            try:
                guild_id = int(guild.id)
            except (AttributeError, TypeError, ValueError):
                guild_id = None
            # A caller may pass a candidate guild that is already present in
            # ``bot.guilds``.  Counting its uncached member range twice would
            # falsely evict the candidate near the capacity boundary.
            if guild_id is not None:
                if guild_id in seen_guilds:
                    continue
                seen_guilds.add(guild_id)
            members = list(getattr(guild, "members", ()) or ())
            for member in members:
                if getattr(member, "bot", False):
                    continue
                try:
                    known.add(int(member.id))
                except (TypeError, ValueError):
                    continue
            member_count = getattr(guild, "member_count", None)
            if isinstance(member_count, int) and member_count > len(members):
                # This is intentionally an upper bound.  We cannot identify
                # uncached bot members, so counting all missing entries is the
                # only safe option before a guild is fully chunked.
                uncached += member_count - len(members)
        return len(known) + uncached

    async def _over_capacity(self, guild: discord.Guild) -> bool:
        configured_guilds = cast(
            Iterable[discord.Guild], getattr(self.bot, "guilds", ()) or ()
        )
        guilds: list[discord.Guild] = [
            item for item in configured_guilds if not is_operational_guild(item)
        ]
        if not is_operational_guild(guild) and guild not in guilds:
            guilds.append(guild)
        return self._max_unique_humans(guilds) > self.MAX_UNIQUE_USERS

    @staticmethod
    def _guild_join_sort_key(guild: discord.Guild) -> tuple[float, int]:
        joined_at = getattr(getattr(guild, "me", None), "joined_at", None)
        if not isinstance(joined_at, datetime.datetime):
            joined_at = None
        timestamp = joined_at.timestamp() if joined_at is not None else 0.0
        return timestamp, int(getattr(guild, "id", 0))

    async def _enforce_capacity(self) -> None:
        """Evict at most the newest unexempt guild when over the cap.

        Existing guilds are snapshotted on the first pass after startup.  A
        periodic check must not unexpectedly evict a long-standing server;
        only a guild that joined after that baseline (or one whose join event
        was missed) is eligible.  Joining is still checked synchronously in
        ``on_guild_join``.
        """

        if not self._is_replacement_bot():
            return
        guilds = [
            guild
            for guild in list(getattr(self.bot, "guilds", ()) or ())
            if not is_operational_guild(guild)
        ]
        for guild in guilds:
            self._is_capacity_exempt(guild)
        if not self._capacity_baseline_ready:
            self._capacity_baseline_guilds = {
                int(guild.id)
                for guild in guilds
                if int(guild.id) not in self._capacity_exempt_guilds
            }
            self._capacity_baseline_ready = True
            return

        candidates = [
            guild
            for guild in guilds
            if int(guild.id) not in self._capacity_baseline_guilds
            and int(guild.id) not in self._capacity_exempt_guilds
        ]
        if not candidates:
            return
        candidate = max(candidates, key=self._guild_join_sort_key)
        if await self._over_capacity(candidate):
            await self.guild_at_capacity(candidate)

    @staticmethod
    def _preferred_message_channel(guild: discord.Guild) -> Messageable | None:
        channels = list(getattr(guild, "text_channels", ()) or ())
        if not channels:
            return None
        channel = discord.utils.find(
            lambda item: re.search(r"(general|main|chat|lounge)", item.name.casefold()),
            channels,
        )
        return channel or channels[0]

    async def get_bot_adder(self, guild: discord.Guild) -> int | None:
        """Return the user who added Fishie when a recent audit entry is available."""
        me = guild.me
        bot_user = self.bot.user
        if me is None or bot_user is None or not me.guild_permissions.view_audit_log:
            return None

        for attempt in range(4):
            try:
                async for entry in guild.audit_logs(
                    limit=10,
                    action=discord.AuditLogAction.bot_add,
                ):
                    if getattr(entry.target, "id", None) != bot_user.id:
                        continue
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if -2 <= age <= 30 and entry.user is not None:
                        return entry.user.id
            except (discord.Forbidden, discord.HTTPException):
                return None

            if attempt < 3:
                await asyncio.sleep(0.5)

        return None

    async def post_guild(self, embed: discord.Embed, guild: discord.Guild):
        embed.add_field(
            name="Created", value=discord.utils.format_dt(guild.created_at, "d")
        )
        embed.add_field(
            name="Members",
            value=f"{guild.member_count:,} ({sum(m.bot for m in guild.members)} bots)",
        )

        embed.set_footer(
            text=(
                f"ID: {guild.id} \n"
                f"Bot: {self._bot_label()} (`{self._current_bot_id() or 'unknown'}`)\n"
                f"Owner ID: {guild.owner_id} \nCreated at"
            )
        )

        embed.color = [discord.Colour.green(), discord.Colour.red()][
            sum(m.bot for m in guild.members) > sum(not m.bot for m in guild.members)
        ]

        channel = self.bot.get_channel(self.bot.config["ids"]["join_logs_id"])

        if not isinstance(channel, Messageable):
            raise commands.BadArgument("Join logs channel is not messageable.")

        await channel.send(embed=embed)

    async def guild_too_small(self, guild: discord.Guild):
        if guild.owner_id == self.bot.config["ids"]["owner_id"]:
            return

        self.bot.logger.info(
            f"I left the guild '{guild}' (ID: {guild.id}) due to its small membership size. There were only {guild.member_count} members."
        )
        # A legacy process is being retired; any invite in its leave notice
        # must point to the replacement application so the administrator is
        # not sent back to the bot that just left.
        if self._is_legacy_bot():
            replacement_id = getattr(self.bot, "new_bot_id", None)
            if callable(replacement_id):
                replacement_id = cast(Any, replacement_id)()
            try:
                candidate = replacement_id or self.REPLACEMENT_BOT_ID
                bot_id = (
                    int(candidate)
                    if isinstance(candidate, (str, int))
                    else self.REPLACEMENT_BOT_ID
                )
            except (TypeError, ValueError):
                bot_id = self.REPLACEMENT_BOT_ID
        else:
            bot_id = self._current_bot_id() or int(self.bot.config["ids"]["bot_id"])
        url = discord.utils.oauth_url(
            bot_id,
            permissions=self.bot.bot_permissions,
            scopes=("bot", "applications.commands"),
        )

        embed = discord.Embed(
            color=discord.Color.red(),
            title="NOTICE | Message from developers",
            timestamp=discord.utils.utcnow(),
        )

        msg = f"This server currently has only {sum(not m.bot for m in guild.members)} members, which is insufficient for using this bot. The solution to this issue is to either add the bot to a larger server or join the support server and utilize the bot there."

        embed.add_field(name="Member count issue", value=msg)
        embed.add_field(
            name="Links",
            inline=False,
            value=f"[Support Server](https://discord.gg/Fct5UGadcb)\n"
            f"[Bot Invite URL]({url})",
        )

        channel: Optional[discord.TextChannel] = discord.utils.find(
            lambda ch: re.search("(general|main|chat|lounge)", ch.name.lower()),
            guild.text_channels,
        )

        try:
            channel = channel or guild.text_channels[0]
        except IndexError:
            await guild.leave()
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

        await guild.leave()

    async def guild_at_capacity(self, guild: discord.Guild) -> None:
        """Tell a newly joined replacement guild why it cannot be retained."""

        self.bot.logger.info(
            "Leaving guild '%s' (ID: %s): replacement bot capacity exceeds %s users",
            guild,
            guild.id,
            self.MAX_UNIQUE_USERS,
        )
        channel = self._preferred_message_channel(guild)
        if channel is not None:
            bot_id = self._current_bot_id() or int(self.bot.config["ids"]["bot_id"])
            url = discord.utils.oauth_url(
                bot_id,
                permissions=self.bot.bot_permissions,
                scopes=("bot", "applications.commands"),
            )
            embed = discord.Embed(
                color=discord.Colour.red(),
                title="NOTICE | Message from developers",
                timestamp=discord.utils.utcnow(),
                description=(
                    "Sorry, Fishie is currently at capacity and cannot stay in "
                    "this server. Please join the support server to use the bot."
                ),
            )
            embed.add_field(
                name="Links",
                value=(
                    "[Support Server](https://discord.gg/rM9u4MRFBE)\n"
                    f"[Bot Invite URL]({url})"
                ),
            )
            try:
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
        await guild.leave()

    @tasks.loop(hours=1.0)
    async def capacity_check_task(self) -> None:
        """Periodically enforce replacement capacity for newly joined guilds."""

        try:
            await self._enforce_capacity()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Replacement bot capacity check failed")

    @capacity_check_task.before_loop
    async def before_capacity_check_task(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_member_join")
    async def on_replacement_bot_join(self, member: discord.Member) -> None:
        """Have the legacy process leave as soon as the replacement arrives."""

        replacement_id = self.REPLACEMENT_BOT_ID
        configured = getattr(self.bot, "new_bot_id", None)
        try:
            if configured is not None:
                value = cast(Any, configured)() if callable(configured) else configured
                if isinstance(value, (str, int)):
                    replacement_id = int(value)
        except (TypeError, ValueError):
            replacement_id = self.REPLACEMENT_BOT_ID
        if not self._is_legacy_bot() or member.id != replacement_id:
            return
        guild = member.guild
        self.bot.logger.info(
            "Replacement Fishie bot joined guild %s; leaving legacy membership",
            guild.id,
        )
        try:
            await guild.leave()
            self._legacy_handoff_left.add(int(guild.id))
        except (discord.Forbidden, discord.HTTPException):
            self._legacy_handoff_left.discard(int(guild.id))
            self.bot.logger.warning(
                "Could not leave guild %s after replacement bot joined",
                guild.id,
                exc_info=True,
            )

    @commands.Cog.listener("on_ready")
    async def on_legacy_ready(self) -> None:
        """Reconcile guild overlap after reconnecting the legacy process.

        ``on_member_join`` can be missed during a gateway reconnect.  A
        one-time-per-membership scan on READY closes that gap without polling
        Discord continuously.  Membership hand-off applies to every guild,
        including the private operations guild; event automation itself still
        excludes that guild below.
        """

        if not self._is_legacy_bot():
            return
        guilds = cast(Iterable[discord.Guild], getattr(self.bot, "guilds", ()) or ())
        for guild in guilds:
            guild_id = int(getattr(guild, "id", 0))
            if guild_id in self._legacy_handoff_left:
                continue
            if not self._replacement_member_present(guild):
                continue
            self._legacy_handoff_left.add(guild_id)
            self.bot.logger.info(
                "Replacement Fishie already present in guild %s; leaving legacy membership",
                guild_id,
            )
            try:
                await guild.leave()
            except (discord.Forbidden, discord.HTTPException):
                self._legacy_handoff_left.discard(guild_id)
                self.bot.logger.warning(
                    "Could not leave guild %s during legacy hand-off scan",
                    guild_id,
                    exc_info=True,
                )

    @commands.Cog.listener("on_guild_join")
    async def on_guild_join(self, guild: discord.Guild):
        # If the retiring process is invited after the replacement is already
        # present, leave immediately.  The replacement is always preferred;
        # this also covers deployments where the old process missed the
        # replacement member's ``on_member_join`` dispatch.
        replacement_present = self._replacement_member_present(guild)
        if (
            self._is_legacy_bot()
            and not replacement_present
            and await self._member_present_remotely(guild, self.REPLACEMENT_BOT_ID)
        ):
            replacement_present = True
        if self._is_legacy_bot() and replacement_present:
            self.bot.logger.info(
                "Legacy Fishie joined guild %s where replacement is present; leaving",
                guild.id,
            )
            try:
                await guild.leave()
            except (discord.Forbidden, discord.HTTPException):
                self.bot.logger.warning(
                    "Could not leave guild %s after detecting replacement bot",
                    guild.id,
                    exc_info=True,
                )
            return

        # The operations guild is intentionally absent from generic lifecycle
        # events. Review/upload commands target its channels explicitly. The
        # replacement/legacy hand-off above still runs for that guild so only
        # one application remains in every server.
        if is_operational_guild(guild):
            return

        # If both applications briefly overlap, the old bot's leave event is
        # responsible for the hand-off.  The replacement must not reject a
        # guild based on member count during that short window.
        legacy_present = self._member_present(guild, self.LEGACY_BOT_ID)
        if (
            self._is_replacement_bot()
            and not legacy_present
            and await self._member_present_remotely(guild, self.LEGACY_BOT_ID)
        ):
            legacy_present = True
        replacement_overlap = self._is_replacement_bot() and legacy_present
        if replacement_overlap:
            self._capacity_exempt_guilds.add(int(guild.id))
        if self._is_replacement_bot() and not replacement_overlap:
            if await self._over_capacity(guild):
                await self.guild_at_capacity(guild)
                return

        added_by = await self.get_bot_adder(guild)
        if (
            sum(not m.bot for m in guild.members) <= 5
            and guild.id not in self.allowed_guilds
            and added_by not in self.allowed_adders
            and not replacement_overlap
        ):
            await self.guild_too_small(guild)
            return

        # Server history is public by default.  Keep this row separate from
        # the automation settings so a newly joined guild is visible even
        # before an administrator opens the settings panel.
        await self.bot.pool.execute(
            "INSERT INTO guild_settings (guild_id, history_public) VALUES ($1, TRUE) "
            "ON CONFLICT (guild_id) DO NOTHING",
            guild.id,
        )

        embed = discord.Embed(title=guild.name, timestamp=discord.utils.utcnow())
        embed.set_author(
            name=f"Joined Guild · {self._bot_label()}",
            icon_url=guild.icon.url if guild.icon else None,
        )

        await self.post_guild(embed, guild)

        sql = """
        INSERT INTO guild_join_logs(guild_id, owner_id, added_by, time)
        VALUES($1, $2, $3, $4)
        """

        await self.bot.pool.execute(
            sql,
            guild.id,
            guild.owner_id,
            added_by,
            discord.utils.utcnow(),
        )

    @commands.Cog.listener("on_guild_remove")
    async def on_guild_remove(self, guild: discord.Guild):
        # The private operations guild owns review/upload/logging channels;
        # lifecycle automation (including custom-badge reconciliation) must
        # never run there.
        if is_operational_guild(guild):
            return
        # Only the replacement instance reconciles global custom-badge
        # ownership.  During hand-off the legacy process also receives a
        # guild-remove event, and letting it revoke from its stale emoji cache
        # could refund a badge that the replacement still serves.
        if self._is_replacement_bot():
            try:
                # Purchased custom-emoji badges are global user records. If
                # the bot leaves this guild, re-check all visible emojis and
                # refund half the purchase price for badges whose emoji is no
                # longer accessible.
                badges = getattr(self.bot, "badges", None)
                revoke = getattr(badges, "revoke_unavailable_custom_badges", None)
                if revoke is not None:
                    await revoke({emoji.id for emoji in self.bot.emojis})
            except Exception:
                self.bot.logger.exception(
                    "Failed to reconcile custom badge emojis after leaving guild %s",
                    guild.id,
                )
        if sum(not m.bot for m in guild.members) <= 5:
            return
        embed = discord.Embed(title=guild.name, timestamp=discord.utils.utcnow())
        embed.set_author(
            name=f"Left Guild · {self._bot_label()}",
            icon_url=guild.icon.url if guild.icon else None,
        )

        await self.post_guild(embed, guild)
