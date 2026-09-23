import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import role_rota_database as rota_db
from config import REMINDERS_CHANNEL_ID
from role_rota_database import (
    ROLES,
    STATUS_ACTIVE,
    STATUS_BACKUP,
)
from role_rota_image import CRITICAL_ROLES, clean_display_name, rota_file


log = logging.getLogger("rota-bot.role-rota")

STATUS_LABEL = {
    STATUS_ACTIVE: "Active",
    STATUS_BACKUP: "Backup",
}
MAX_ACTIVE_MAIN_SCHEDULERS = 2
MAX_ALLOWED_USER_MENTIONS = 100
DISCORD_CONTENT_LIMIT = 2000

_refresh_locks: dict[int, asyncio.Lock] = {}
_mutation_locks: dict[int, asyncio.Lock] = {}
_notification_locks: dict[int, asyncio.Lock] = {}


def display_name_from_interaction(interaction: discord.Interaction) -> str:
    raw = getattr(interaction.user, "display_name", interaction.user.name)
    return clean_display_name(raw)


def _user_alert_batches(
    body: str,
    user_ids: list[int],
) -> list[tuple[str, discord.AllowedMentions]]:
    """Build safe channel messages for opted-in member mentions."""
    if len(body) > DISCORD_CONTENT_LIMIT:
        raise ValueError("notification body exceeds Discord's content limit")

    ordered_ids = list(dict.fromkeys(int(user_id) for user_id in user_ids))
    batches: list[list[int]] = []
    current: list[int] = []
    for user_id in ordered_ids:
        candidate = current + [user_id]
        mention_text = " ".join(f"<@{item}>" for item in candidate)
        content_length = len(body) + 2 + len(mention_text)
        if current and (
            len(candidate) > MAX_ALLOWED_USER_MENTIONS
            or content_length > DISCORD_CONTENT_LIMIT
        ):
            batches.append(current)
            current = [user_id]
        else:
            current = candidate
    if current:
        batches.append(current)

    messages: list[tuple[str, discord.AllowedMentions]] = []
    for batch in batches:
        mentions = " ".join(f"<@{user_id}>" for user_id in batch)
        messages.append(
            (
                f"{body}\n\n{mentions}",
                discord.AllowedMentions(
                    everyone=False,
                    users=[discord.Object(id=user_id) for user_id in batch],
                    roles=False,
                    replied_user=False,
                ),
            )
        )
    return messages


async def _send_shortage_alert(
    bot: commands.Bot,
    body: str,
    user_ids: list[int],
) -> bool:
    channel = bot.get_channel(REMINDERS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(REMINDERS_CHANNEL_ID)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception(
                "Could not resolve reminders channel %s for CTO X alert",
                REMINDERS_CHANNEL_ID,
            )
            return False

    try:
        batches = _user_alert_batches(body, user_ids)
        if not batches:
            await channel.send(
                body,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        for content, allowed_mentions in batches:
            await channel.send(
                content,
                allowed_mentions=allowed_mentions,
            )
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.exception(
            "Could not send CTO X shortage alert in channel %s",
            REMINDERS_CHANNEL_ID,
        )
        return False


async def refresh_rota(bot: commands.Bot, guild_id: int) -> bool:
    """Regenerate and replace the image on the stored rota message."""
    lock = _refresh_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        target = await rota_db.get_rota_message(guild_id)
        if not target:
            return False

        channel_id, message_id = target
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                log.exception("Could not resolve CTO X rota channel %s", channel_id)
                return False

        try:
            message = await channel.fetch_message(message_id)
            members = await rota_db.get_members(guild_id)
            assignments = await rota_db.get_assignments(guild_id)
            await message.edit(
                attachments=[rota_file(members, assignments)],
                view=MainRotaView(bot),
            )
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception("Could not refresh CTO X rota message %s", message_id)
            return False


async def active_count_for_role(
    guild_id: int,
    role_name: str,
    *,
    excluding_user_id: Optional[int] = None,
) -> int:
    assignments = await rota_db.get_assignments(guild_id)
    return sum(
        1
        for assignment in assignments
        if assignment.role_name == role_name
        and assignment.status == STATUS_ACTIVE
        and (
            excluding_user_id is None
            or assignment.user_id != excluding_user_id
        )
    )


async def send_shortage_notifications(
    bot: commands.Bot,
    guild_id: int,
) -> None:
    """Post once when critical coverage drops to zero active members."""
    lock = _notification_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        if not await rota_db.team_notifications_enabled(guild_id):
            return

        assignments = await rota_db.get_assignments(guild_id)
        active_counts = {role: 0 for role in CRITICAL_ROLES}
        for assignment in assignments:
            if (
                assignment.role_name in active_counts
                and assignment.status == STATUS_ACTIVE
            ):
                active_counts[assignment.role_name] += 1

        newly_short: list[str] = []
        for role in CRITICAL_ROLES:
            is_short = active_counts[role] == 0
            was_alerted = await rota_db.get_shortage_alerted(guild_id, role)
            if is_short and not was_alerted:
                newly_short.append(role)
            elif not is_short and was_alerted:
                await rota_db.set_shortage_alerted(guild_id, role, False)

        if not newly_short:
            return

        role_lines = "\n".join(
            f"• **{role}** — no active member"
            for role in sorted(newly_short)
        )
        body = (
            "⚠️ **CTO X rota coverage alert**\n"
            f"{role_lines}\n\n"
            "Please check the live rota and cover the role(s) if you can."
        )
        members = await rota_db.get_members(guild_id)
        opted_in_user_ids = [
            user_id
            for user_id, _, notifications_enabled in members
            if notifications_enabled
        ]

        delivered = await _send_shortage_alert(
            bot,
            body,
            opted_in_user_ids,
        )
        if not delivered:
            return

        for role in newly_short:
            await rota_db.set_shortage_alerted(guild_id, role, True)


async def refresh_and_check(bot: commands.Bot, guild_id: int) -> None:
    await refresh_rota(bot, guild_id)
    await send_shortage_notifications(bot, guild_id)


async def sync_actor_name(interaction: discord.Interaction) -> None:
    if (
        interaction.guild
        and await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        )
    ):
        await rota_db.upsert_member(
            interaction.guild.id,
            interaction.user.id,
            display_name_from_interaction(interaction),
        )


class RoleSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        custom_id: str,
        placeholder: str = "Choose a role…",
        roles: Optional[list[str]] = None,
    ):
        role_list = roles or ROLES
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=role, value=role)
                for role in role_list
            ],
            custom_id=custom_id,
        )


class StatusView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        target_user_id: int,
        role_name: str,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.target_user_id = target_user_id
        self.role_name = role_name

    async def _target_name(
        self,
        interaction: discord.Interaction,
    ) -> Optional[str]:
        assert interaction.guild is not None
        if self.target_user_id == interaction.user.id:
            name = display_name_from_interaction(interaction)
            if not await rota_db.is_member(
                interaction.guild.id,
                self.target_user_id,
            ):
                return None
            await rota_db.upsert_member(
                interaction.guild.id,
                self.target_user_id,
                name,
            )
            return name

        member = await rota_db.get_member(
            interaction.guild.id,
            self.target_user_id,
        )
        return member[1] if member else None

    async def apply(
        self,
        interaction: discord.Interaction,
        status: str,
    ) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        mutation_lock = _mutation_locks.setdefault(
            guild_id,
            asyncio.Lock(),
        )

        async with mutation_lock:
            target_name = await self._target_name(interaction)
            if target_name is None:
                await interaction.response.send_message(
                    "That person is no longer on the CTO X team.",
                    ephemeral=True,
                )
                return

            if self.role_name == "Main Scheduler" and status == STATUS_ACTIVE:
                other_active = await active_count_for_role(
                    guild_id,
                    "Main Scheduler",
                    excluding_user_id=self.target_user_id,
                )
                if other_active >= MAX_ACTIVE_MAIN_SCHEDULERS:
                    await interaction.response.send_message(
                        "There are already **2 active Main Schedulers**. "
                        "Change one to Backup or remove that assignment first.",
                        ephemeral=True,
                    )
                    return

            await interaction.response.defer(ephemeral=True, thinking=True)
            await rota_db.set_assignment(
                guild_id,
                self.target_user_id,
                self.role_name,
                status,
            )
            await refresh_and_check(self.bot, guild_id)

        await interaction.edit_original_response(
            content=(
                f"Updated **{target_name}** — **{self.role_name}** "
                f"→ **{STATUS_LABEL[status]}**."
            ),
            view=None,
        )

    @discord.ui.button(
        label="Active",
        emoji="🟢",
        style=discord.ButtonStyle.success,
    )
    async def active(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        await self.apply(interaction, STATUS_ACTIVE)

    @discord.ui.button(
        label="Backup",
        emoji="🟡",
        style=discord.ButtonStyle.secondary,
    )
    async def backup(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        await self.apply(interaction, STATUS_BACKUP)

    @discord.ui.button(
        label="Remove",
        emoji="➖",
        style=discord.ButtonStyle.danger,
    )
    async def remove(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id
        mutation_lock = _mutation_locks.setdefault(
            guild_id,
            asyncio.Lock(),
        )
        async with mutation_lock:
            target_name = await self._target_name(interaction)
            if target_name is None:
                await interaction.edit_original_response(
                    content="That person is no longer on the CTO X team.",
                    view=None,
                )
                return
            await rota_db.remove_assignment(
                guild_id,
                self.target_user_id,
                self.role_name,
            )
            await refresh_and_check(self.bot, guild_id)
        await interaction.edit_original_response(
            content=(
                f"Removed **{target_name}** from **{self.role_name}**."
            ),
            view=None,
        )


class AssignRoleView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        target_user_id: int,
        roles: Optional[list[str]] = None,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.target_user_id = target_user_id
        select = RoleSelect(
            custom_id="cto_x:assign_role",
            roles=roles,
        )
        select.callback = self.role_selected
        self.add_item(select)

    async def role_selected(self, interaction: discord.Interaction):
        role_name = interaction.data["values"][0]
        await interaction.response.edit_message(
            content=f"Choose the status for **{role_name}**:",
            view=StatusView(self.bot, self.target_user_id, role_name),
        )


class TeamMemberSelect(discord.ui.Select):
    def __init__(self, members: list[tuple[int, str, bool]]):
        super().__init__(
            placeholder="Choose a team member…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name[:100], value=str(user_id))
                for user_id, name, _ in members[:25]
            ],
        )


class ConfirmManagedRemovalView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        target_user_id: int,
        target_name: str,
    ):
        super().__init__(timeout=60)
        self.bot = bot
        self.target_user_id = target_user_id
        self.target_name = target_name

    @discord.ui.button(
        label="Remove from CTO X Team",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id
        mutation_lock = _mutation_locks.setdefault(
            guild_id,
            asyncio.Lock(),
        )
        async with mutation_lock:
            await rota_db.remove_member(guild_id, self.target_user_id)
            await refresh_and_check(self.bot, guild_id)
        await interaction.edit_original_response(
            content=(
                f"Removed **{self.target_name}** from the CTO X team."
            ),
            view=None,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="No changes made.",
            view=None,
        )


class SelectedMemberView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        target_user_id: int,
        target_name: str,
    ):
        super().__init__(timeout=180)
        self.bot = bot
        self.target_user_id = target_user_id
        self.target_name = target_name
        role_select = RoleSelect(
            custom_id="cto_x:manage_role",
            placeholder="Choose a role to edit…",
        )
        role_select.callback = self.role_selected
        self.add_item(role_select)

    async def role_selected(self, interaction: discord.Interaction):
        role_name = interaction.data["values"][0]
        await interaction.response.edit_message(
            content=(
                f"Editing **{self.target_name}** — **{role_name}**. "
                "Choose a new status or remove the assignment:"
            ),
            view=StatusView(self.bot, self.target_user_id, role_name),
        )

    @discord.ui.button(
        label="Remove from Team",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
    )
    async def remove_member(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content=(
                f"Remove **{self.target_name}** from the CTO X team? "
                "This also removes all of their assignments."
            ),
            view=ConfirmManagedRemovalView(
                self.bot,
                self.target_user_id,
                self.target_name,
            ),
        )


class ManageRotaView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        members: list[tuple[int, str, bool]],
        *,
        personal_enabled: bool,
        team_enabled: bool,
    ):
        super().__init__(timeout=180)
        self.bot = bot
        self.members = members
        self.personal_enabled = personal_enabled
        self.team_enabled = team_enabled

        if members:
            select = TeamMemberSelect(members)
            select.callback = self.member_selected
            self.add_item(select)

        personal = discord.ui.Button(
            label=(
                f"Mention Me: {'ON' if personal_enabled else 'OFF'}"
            ),
            emoji="🔔",
            style=(
                discord.ButtonStyle.success
                if personal_enabled
                else discord.ButtonStyle.secondary
            ),
            custom_id="cto_x:my_notifications",
        )
        personal.callback = self.toggle_personal_notifications
        self.add_item(personal)

        team = discord.ui.Button(
            label=f"Team Alerts: {'ON' if team_enabled else 'OFF'}",
            emoji="📣",
            style=(
                discord.ButtonStyle.success
                if team_enabled
                else discord.ButtonStyle.danger
            ),
            custom_id="cto_x:team_notifications",
        )
        team.callback = self.toggle_team_notifications
        self.add_item(team)

    async def member_selected(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        user_id = int(interaction.data["values"][0])
        member = await rota_db.get_member(interaction.guild.id, user_id)
        if member is None:
            await interaction.response.edit_message(
                content="That person is no longer on the CTO X team.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"Managing **{member[1]}**:",
            view=SelectedMemberView(self.bot, user_id, member[1]),
        )

    async def toggle_personal_notifications(
        self,
        interaction: discord.Interaction,
    ):
        assert interaction.guild is not None
        await sync_actor_name(interaction)
        if not await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        ):
            await interaction.response.send_message(
                "Join the CTO X team before changing mention preferences.",
                ephemeral=True,
            )
            return

        current = await rota_db.notifications_enabled(
            interaction.guild.id,
            interaction.user.id,
        )
        new_state = not current
        await rota_db.set_notifications(
            interaction.guild.id,
            interaction.user.id,
            new_state,
        )
        await interaction.response.edit_message(
            content=(
                "CTO X shortage-alert channel mentions are now "
                f"**{'ON' if new_state else 'OFF'}** for you. "
                "Alerts still appear in the reminders channel."
            ),
            view=ManageRotaView(
                self.bot,
                await rota_db.get_members(interaction.guild.id),
                personal_enabled=new_state,
                team_enabled=await rota_db.team_notifications_enabled(
                    interaction.guild.id
                ),
            ),
        )

    async def toggle_team_notifications(
        self,
        interaction: discord.Interaction,
    ):
        assert interaction.guild is not None
        if not await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        ):
            await interaction.response.send_message(
                "Only CTO X team members can change team alerts.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        current = await rota_db.team_notifications_enabled(guild_id)
        new_state = not current
        await rota_db.set_team_notifications(guild_id, new_state)

        await interaction.response.edit_message(
            content=(
                f"Team shortage alerts are now "
                f"**{'ON' if new_state else 'OFF'}**. "
                "Alerts are posted in the reminders channel when Main "
                "Scheduler, Sniping & Raid Replies, or Live Response has "
                "zero active members."
            ),
            view=ManageRotaView(
                self.bot,
                await rota_db.get_members(guild_id),
                personal_enabled=await rota_db.notifications_enabled(
                    guild_id,
                    interaction.user.id,
                ),
                team_enabled=new_state,
            ),
        )

        if new_state:
            await send_shortage_notifications(self.bot, guild_id)


class ConfirmLeaveView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(
        label="Leave CTO X Team",
        emoji="🚪",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id
        mutation_lock = _mutation_locks.setdefault(
            guild_id,
            asyncio.Lock(),
        )
        async with mutation_lock:
            await rota_db.remove_member(guild_id, interaction.user.id)
            await refresh_and_check(self.bot, guild_id)
        await interaction.edit_original_response(
            content=(
                "You have left the CTO X team and your task assignments "
                "were removed."
            ),
            view=None,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="No changes made.",
            view=None,
        )


class EditMyTasksView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        user_id: int,
        assigned_roles: list[str],
    ):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        select = RoleSelect(
            custom_id="cto_x:edit_my_role",
            placeholder="Choose one of your assigned roles…",
            roles=assigned_roles,
        )
        select.callback = self.role_selected
        self.add_item(select)

    async def role_selected(self, interaction: discord.Interaction):
        role_name = interaction.data["values"][0]
        await interaction.response.edit_message(
            content=f"Edit your **{role_name}** assignment:",
            view=StatusView(self.bot, self.user_id, role_name),
        )

    @discord.ui.button(
        label="Leave CTO X Team",
        emoji="🚪",
        style=discord.ButtonStyle.danger,
    )
    async def leave_team(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content=(
                "Leaving removes all of your CTO X task assignments. "
                "Continue?"
            ),
            view=ConfirmLeaveView(self.bot),
        )


class MainRotaView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Refresh Rota",
        emoji="🔄",
        style=discord.ButtonStyle.danger,
        custom_id="cto_x:refresh_rota",
        row=0,
    )
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        if interaction.guild is None:
            return

        members = await rota_db.get_members(interaction.guild.id)
        assignments = await rota_db.get_assignments(interaction.guild.id)
        try:
            await interaction.response.edit_message(
                attachments=[rota_file(members, assignments)],
                view=MainRotaView(self.bot),
            )
        except discord.HTTPException:
            log.exception("Could not refresh CTO X rota from interaction")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "I couldn't refresh the rota image.",
                    ephemeral=True,
                )
            return

        try:
            await interaction.followup.send(
                "Rota refreshed.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Join CTO X Team",
        emoji="➕",
        style=discord.ButtonStyle.success,
        custom_id="cto_x:join",
        row=0,
    )
    async def join(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = display_name_from_interaction(interaction)
        already_member = await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        )
        await rota_db.upsert_member(
            interaction.guild.id,
            interaction.user.id,
            name,
        )
        await refresh_rota(self.bot, interaction.guild.id)
        await interaction.edit_original_response(
            content=(
                "Your display name was refreshed on the CTO X rota."
                if already_member
                else "You’re now on the **CTO X Team** rota."
            )
        )

    @discord.ui.button(
        label="Assign My Tasks",
        emoji="📋",
        style=discord.ButtonStyle.primary,
        custom_id="cto_x:assign_my_tasks",
        row=0,
    )
    async def assign_my_tasks(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        if interaction.guild is None:
            return
        await sync_actor_name(interaction)
        if not await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        ):
            await interaction.response.send_message(
                "Join the **CTO X Team** first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Choose the task you want to assign to yourself:",
            view=AssignRoleView(self.bot, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Edit My Tasks",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
        custom_id="cto_x:edit_my_tasks",
        row=0,
    )
    async def edit_my_tasks(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        if interaction.guild is None:
            return
        await sync_actor_name(interaction)
        if not await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        ):
            await interaction.response.send_message(
                "You are not currently on the CTO X team.",
                ephemeral=True,
            )
            return

        assignments = await rota_db.get_assignments(
            interaction.guild.id,
            interaction.user.id,
        )
        if not assignments:
            await interaction.response.send_message(
                "You do not have any tasks yet. Use **Assign My Tasks** "
                "first.\nYou can still leave the team below.",
                view=ConfirmLeaveView(self.bot),
                ephemeral=True,
            )
            return

        role_order = {role: index for index, role in enumerate(ROLES)}
        assignments.sort(
            key=lambda assignment: role_order[assignment.role_name]
        )
        summary = "\n".join(
            f"• **{assignment.role_name}** — "
            f"{STATUS_LABEL[assignment.status]}"
            for assignment in assignments
        )
        await interaction.response.send_message(
            f"Your current assignments:\n{summary}\n\n"
            "Choose a role below to change or remove it.",
            view=EditMyTasksView(
                self.bot,
                interaction.user.id,
                [assignment.role_name for assignment in assignments],
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Manage Rota",
        emoji="⚙️",
        style=discord.ButtonStyle.secondary,
        custom_id="cto_x:manage_rota",
        row=0,
    )
    async def manage_rota(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ):
        if interaction.guild is None:
            return
        await sync_actor_name(interaction)
        if not await rota_db.is_member(
            interaction.guild.id,
            interaction.user.id,
        ):
            await interaction.response.send_message(
                "Only members of the CTO X team can manage the rota.",
                ephemeral=True,
            )
            return

        members = await rota_db.get_members(interaction.guild.id)
        await interaction.response.send_message(
            "Manage a team member or shortage-alert settings. Alerts apply "
            "only to critical roles with zero active coverage.",
            view=ManageRotaView(
                self.bot,
                members,
                personal_enabled=await rota_db.notifications_enabled(
                    interaction.guild.id,
                    interaction.user.id,
                ),
                team_enabled=await rota_db.team_notifications_enabled(
                    interaction.guild.id
                ),
            ),
            ephemeral=True,
        )


class RoleRotaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await rota_db.init_role_rota_db()
        self.bot.add_view(MainRotaView(self.bot))

    @app_commands.command(
        name="setup_rota",
        description="Create or repair the CTO X live rota panel here.",
    )
    async def setup_rota(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "Run this command in a server channel.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        if await rota_db.is_member(guild_id, interaction.user.id):
            await rota_db.upsert_member(
                guild_id,
                interaction.user.id,
                display_name_from_interaction(interaction),
            )

        existing = await rota_db.get_rota_message(guild_id)
        if existing:
            channel_id, message_id = existing
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (
                    discord.Forbidden,
                    discord.NotFound,
                    discord.HTTPException,
                ):
                    channel = None

            if channel is not None:
                try:
                    await channel.fetch_message(message_id)
                except (
                    discord.Forbidden,
                    discord.NotFound,
                    discord.HTTPException,
                ):
                    pass
                else:
                    await interaction.response.defer(ephemeral=True)
                    refreshed = await refresh_rota(self.bot, guild_id)
                    await interaction.edit_original_response(
                        content=(
                            "The existing CTO X rota panel was refreshed."
                            if refreshed
                            else "I found the existing panel, but Discord "
                            "would not let me refresh it."
                        )
                    )
                    return

        members = await rota_db.get_members(guild_id)
        assignments = await rota_db.get_assignments(guild_id)
        try:
            await interaction.response.send_message(
                file=rota_file(members, assignments),
                view=MainRotaView(self.bot),
            )
            message = await interaction.original_response()
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Discord blocked creation of the rota in this channel. "
                    "Please check the bot's channel permissions.",
                    ephemeral=True,
                )
            return

        await rota_db.set_rota_message(
            guild_id,
            interaction.channel.id,
            message.id,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleRotaCog(bot))
