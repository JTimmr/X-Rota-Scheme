import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from config import SCHEDULED_CHANNEL_ID
from database import (
    IMAGES_DIR,
    add_reaction,
    delete_post_by_message_id,
    get_post_by_message_id,
    get_reactions_for_post,
    insert_post,
    remove_reaction,
)

log = logging.getLogger("rota-bot.schedule")

DEFAULT_TZ = "Europe/London"

TIMEZONE_CHOICES = [
    ("UK — Europe/London", "Europe/London"),
    ("CET — Europe/Amsterdam", "Europe/Amsterdam"),
    ("EET — Europe/Bucharest", "Europe/Bucharest"),
    ("US Eastern — America/New_York", "America/New_York"),
    ("US Central — America/Chicago", "America/Chicago"),
    ("US Pacific — America/Los_Angeles", "America/Los_Angeles"),
    ("Dubai — Asia/Dubai", "Asia/Dubai"),
    ("Vietnam — Asia/Ho_Chi_Minh", "Asia/Ho_Chi_Minh"),
    ("Jakarta — Asia/Jakarta", "Asia/Jakarta"),
    ("Brisbane — Australia/Brisbane", "Australia/Brisbane"),
    ("UTC", "UTC"),
]


def _quote_content(content: str) -> str:
    """Prefix every line with > for Discord quote formatting."""
    return "\n".join(f"> {line}" for line in content.split("\n"))


def format_scheduled_message(content: str, scheduled_at: int, created_by_id: str, claimers: list[str] | None = None) -> str:
    lines = [
        "**Scheduled Post**",
        "",
        _quote_content(content),
        "",
        f"Scheduled for: <t:{scheduled_at}:F> (<t:{scheduled_at}:R>)",
        f"Scheduled by: <@{created_by_id}>",
        "",
    ]
    if claimers:
        mentions = ", ".join(f"<@{uid}>" for uid in claimers)
        lines.append(f"Claimed by: {mentions}")
    else:
        lines.append("**Unclaimed** — react to this message to claim it!")

    return "\n".join(lines)


async def save_attachment(attachment: discord.Attachment) -> str | None:
    """Download an attachment and save it to disk. Returns the file path."""
    ext = Path(attachment.filename).suffix
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = IMAGES_DIR / filename
    try:
        await attachment.save(filepath)
        return str(filepath)
    except Exception:
        log.exception(f"Failed to save attachment {attachment.filename}")
        return None


def get_discord_file(image_path: str | None) -> discord.File | None:
    """Create a discord.File from a saved image path."""
    if not image_path:
        return None
    p = Path(image_path)
    if p.exists():
        return discord.File(p, filename=p.name)
    return None


class PostContentModal(discord.ui.Modal, title="Write your post"):
    content_input = discord.ui.TextInput(
        label="Post content",
        style=discord.TextStyle.long,
        placeholder="Write your post here... line breaks are preserved!",
        required=True,
        max_length=4000,
    )

    def __init__(self, bot: commands.Bot, user_id: int, image_path: str | None):
        super().__init__()
        self.bot = bot
        self.user_id = user_id
        self.image_path = image_path

    async def on_submit(self, interaction: discord.Interaction):
        content = self.content_input.value
        view = ScheduleView(self.bot, content, self.user_id, self.image_path)
        await interaction.response.send_message(
            content=view._status_text(),
            view=view,
            ephemeral=True,
        )


class ScheduleView(discord.ui.View):
    def __init__(self, bot: commands.Bot, content: str, user_id: int, image_path: str | None = None):
        super().__init__(timeout=300)
        self.bot = bot
        self.content = content
        self.user_id = user_id
        self.image_path = image_path
        self.selected_day: str | None = None
        self.selected_hour: int | None = None
        self.selected_minute: int | None = None
        self.timezone: str = DEFAULT_TZ
        self.tz_visible = False
        self.submitted = False

        self._build_selects()

    def _build_selects(self):
        self.clear_items()

        if self.tz_visible:
            tz_select = discord.ui.Select(
                custom_id="tz_select",
                placeholder="Select timezone",
                row=0,
            )
            for label, value in TIMEZONE_CHOICES:
                tz_select.add_option(label=label, value=value, default=(self.timezone == value))
            tz_select.callback = self._on_tz_select
            self.add_item(tz_select)
        else:
            tz_button = discord.ui.Button(
                label="Change timezone (default: UK)",
                style=discord.ButtonStyle.secondary,
                custom_id="tz_button",
                row=0,
            )
            tz_button.callback = self._on_tz_button
            self.add_item(tz_button)

        now = datetime.now(tz=ZoneInfo(self.timezone))
        today = now.date()

        day_select = discord.ui.Select(
            custom_id="day_select",
            placeholder="Select day",
            row=1,
        )
        for offset in range(7):
            day = today + timedelta(days=offset)
            value = day.isoformat()
            if offset == 0:
                label = f"Today — {day.strftime('%A %d %b')}"
            elif offset == 1:
                label = f"Tomorrow — {day.strftime('%A %d %b')}"
            else:
                label = day.strftime("%A %d %b")
            day_select.add_option(label=label, value=value, default=(self.selected_day == value))
        day_select.callback = self._on_day_select
        self.add_item(day_select)

        hour_select = discord.ui.Select(
            custom_id="hour_select",
            placeholder="Select hour",
            row=2,
        )
        for h in range(6, 23):
            label = f"{h:02d}:00"
            hour_select.add_option(label=label, value=str(h), default=(self.selected_hour == h))
        hour_select.callback = self._on_hour_select
        self.add_item(hour_select)

        minute_select = discord.ui.Select(
            custom_id="minute_select",
            placeholder="Select minutes",
            row=3,
        )
        for m in [0, 15, 30, 45]:
            label = f":{m:02d}"
            minute_select.add_option(label=label, value=str(m), default=(self.selected_minute == m))
        minute_select.callback = self._on_minute_select
        self.add_item(minute_select)

    def _status_text(self) -> str:
        preview = self.content[:100] + ("..." if len(self.content) > 100 else "")
        parts = [f"**Scheduling post:**\n{_quote_content(preview)}\n"]

        if self.image_path:
            parts.append("📎 Image attached\n")

        day_str = self.selected_day or "—"
        hour_str = f"{self.selected_hour:02d}" if self.selected_hour is not None else "—"
        minute_str = f"{self.selected_minute:02d}" if self.selected_minute is not None else "—"

        parts.append(f"Day: **{day_str}** | Time: **{hour_str}:{minute_str}** | Timezone: **{self.timezone}**")

        return "\n".join(parts)

    async def _try_submit(self, interaction: discord.Interaction):
        if self.submitted:
            return
        if self.selected_day is None or self.selected_hour is None or self.selected_minute is None:
            self._build_selects()
            await interaction.response.edit_message(content=self._status_text(), view=self)
            return

        self.submitted = True

        tz = ZoneInfo(self.timezone)
        day = datetime.fromisoformat(self.selected_day)
        dt = datetime(day.year, day.month, day.day, self.selected_hour, self.selected_minute, tzinfo=tz)
        unix_ts = int(dt.timestamp())

        now_unix = int(datetime.now(tz=ZoneInfo("UTC")).timestamp())
        if unix_ts <= now_unix:
            self.submitted = False
            self._build_selects()
            await interaction.response.edit_message(
                content=self._status_text() + "\n\nThat time is in the past. Pick a different day or time.",
                view=self,
            )
            return

        channel = self.bot.get_channel(SCHEDULED_CHANNEL_ID)
        if not channel:
            await interaction.response.edit_message(content="Could not find the scheduled posts channel.", view=None)
            return

        msg_content = format_scheduled_message(self.content, unix_ts, str(self.user_id))
        file = get_discord_file(self.image_path)
        msg = await channel.send(msg_content, file=file)

        await insert_post(
            discord_message_id=str(msg.id),
            content=self.content,
            scheduled_at=unix_ts,
            created_by=str(self.user_id),
            image_path=self.image_path,
        )

        await interaction.response.edit_message(
            content=f"Post scheduled for <t:{unix_ts}:F> (<t:{unix_ts}:R>).\nThe message has been posted in <#{SCHEDULED_CHANNEL_ID}> — others can react to claim it.",
            view=None,
        )
        log.info(f"Post scheduled by {self.user_id} for {dt.isoformat()}, message {msg.id}")
        self.stop()

    async def _on_day_select(self, interaction: discord.Interaction):
        self.selected_day = interaction.data["values"][0]
        await self._try_submit(interaction)

    async def _on_hour_select(self, interaction: discord.Interaction):
        self.selected_hour = int(interaction.data["values"][0])
        await self._try_submit(interaction)

    async def _on_minute_select(self, interaction: discord.Interaction):
        self.selected_minute = int(interaction.data["values"][0])
        await self._try_submit(interaction)

    async def _on_tz_button(self, interaction: discord.Interaction):
        self.tz_visible = True
        self._build_selects()
        await interaction.response.edit_message(content=self._status_text(), view=self)

    async def _on_tz_select(self, interaction: discord.Interaction):
        self.timezone = interaction.data["values"][0]
        await self._try_submit(interaction)

    async def on_timeout(self):
        pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the person who ran /schedule can use this.", ephemeral=True)
            return False
        return True


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="schedule", description="Schedule a post for X")
    @app_commands.describe(
        image="Optional image to include with the post",
    )
    async def schedule(self, interaction: discord.Interaction, image: discord.Attachment | None = None):
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        image_path = None
        if image:
            if not image.content_type or not image.content_type.startswith("image/"):
                await interaction.response.send_message(
                    "That file doesn't look like an image. Please attach a JPG, PNG, GIF, or WebP.",
                    ephemeral=True,
                )
                return
            image_path = await save_attachment(image)
            if not image_path:
                await interaction.response.send_message(
                    "Failed to save the image. Please try again.",
                    ephemeral=True,
                )
                return

        modal = PostContentModal(self.bot, interaction.user.id, image_path)
        await interaction.response.send_modal(modal)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != SCHEDULED_CHANNEL_ID:
            return
        if payload.user_id == self.bot.user.id:
            return

        post = await get_post_by_message_id(str(payload.message_id))
        if not post or post["status"] != "scheduled":
            return

        await add_reaction(post["id"], str(payload.user_id))

        claimers = await get_reactions_for_post(post["id"])
        channel = self.bot.get_channel(SCHEDULED_CHANNEL_ID)
        if channel:
            try:
                msg = await channel.fetch_message(payload.message_id)
                new_content = format_scheduled_message(
                    post["content"], post["scheduled_at"], post["created_by"], claimers
                )
                await msg.edit(content=new_content)
            except discord.NotFound:
                pass

        log.info(f"Reaction added by {payload.user_id} on post {post['id']}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != SCHEDULED_CHANNEL_ID:
            return
        if payload.user_id == self.bot.user.id:
            return

        post = await get_post_by_message_id(str(payload.message_id))
        if not post or post["status"] != "scheduled":
            return

        await remove_reaction(post["id"], str(payload.user_id))

        claimers = await get_reactions_for_post(post["id"])
        channel = self.bot.get_channel(SCHEDULED_CHANNEL_ID)
        if channel:
            try:
                msg = await channel.fetch_message(payload.message_id)
                new_content = format_scheduled_message(
                    post["content"], post["scheduled_at"], post["created_by"], claimers
                )
                await msg.edit(content=new_content)
            except discord.NotFound:
                pass

        log.info(f"Reaction removed by {payload.user_id} on post {post['id']}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.channel_id != SCHEDULED_CHANNEL_ID:
            return

        post = await get_post_by_message_id(str(payload.message_id))
        if not post:
            return
        if post["status"] != "scheduled":
            return

        await delete_post_by_message_id(str(payload.message_id))
        log.info(f"Cancelled post {post['id']} (message {payload.message_id} was deleted)")


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
