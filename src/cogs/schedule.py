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
    add_claim,
    add_unavailable,
    delete_post_by_message_id,
    get_all_scheduled_posts,
    get_claimers_for_post,
    get_post_by_id,
    get_post_by_message_id,
    get_unavailable_for_post,
    insert_post,
    remove_claim,
    remove_unavailable,
    update_post_content,
    update_post_image,
    update_post_message_id,
    update_post_scheduled_at,
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
    return "\n".join(f"> {line}" for line in content.split("\n"))


def format_scheduled_message(
    content: str,
    scheduled_at: int,
    created_by_id: str,
    claimers: list[str] | None = None,
    unavailable: list[str] | None = None,
) -> str:
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
        lines.append("**Unclaimed** — click Claim to take this post!")

    if unavailable:
        mentions = ", ".join(f"<@{uid}>" for uid in unavailable)
        lines.append(f"Not available: {mentions}")

    return "\n".join(lines)


def get_discord_file(image_path: str | None) -> discord.File | None:
    if not image_path:
        return None
    p = Path(image_path)
    if p.exists():
        return discord.File(p, filename=p.name)
    return None


async def save_attachment(attachment: discord.Attachment) -> str | None:
    ext = Path(attachment.filename).suffix
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = IMAGES_DIR / filename
    try:
        await attachment.save(filepath)
        return str(filepath)
    except Exception:
        log.exception(f"Failed to save attachment {attachment.filename}")
        return None


# ---------------------------------------------------------------------------
# Edit modals / views (for the three edit buttons)
# ---------------------------------------------------------------------------

class EditContentModal(discord.ui.Modal, title="Edit post content"):
    content_input = discord.ui.TextInput(
        label="Post content",
        style=discord.TextStyle.long,
        placeholder="Write your post here... line breaks are preserved!",
        required=True,
        max_length=4000,
    )

    def __init__(self, bot: commands.Bot, post_id: int, current_content: str):
        super().__init__()
        self.bot = bot
        self.post_id = post_id
        self.content_input.default = current_content

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.content_input.value
        await update_post_content(self.post_id, new_content)
        await interaction.response.send_message("Content updated. Refreshing schedule...", ephemeral=True)
        await repost_all_scheduled(self.bot)


class EditTimeView(discord.ui.View):
    """Dropdowns to pick a new time for an existing post."""

    def __init__(self, bot: commands.Bot, post_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.post_id = post_id
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
                custom_id="et_tz_select",
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
                custom_id="et_tz_button",
                row=0,
            )
            tz_button.callback = self._on_tz_button
            self.add_item(tz_button)

        now = datetime.now(tz=ZoneInfo(self.timezone))
        today = now.date()

        day_select = discord.ui.Select(custom_id="et_day", placeholder="Select day", row=1)
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
        day_select.callback = self._on_day
        self.add_item(day_select)

        hour_select = discord.ui.Select(custom_id="et_hour", placeholder="Select hour", row=2)
        for h in range(6, 23):
            hour_select.add_option(label=f"{h:02d}:00", value=str(h), default=(self.selected_hour == h))
        hour_select.callback = self._on_hour
        self.add_item(hour_select)

        minute_select = discord.ui.Select(custom_id="et_minute", placeholder="Select minutes", row=3)
        for m in [0, 15, 30, 45]:
            minute_select.add_option(label=f":{m:02d}", value=str(m), default=(self.selected_minute == m))
        minute_select.callback = self._on_minute
        self.add_item(minute_select)

    def _status_text(self) -> str:
        day_str = self.selected_day or "—"
        hour_str = f"{self.selected_hour:02d}" if self.selected_hour is not None else "—"
        minute_str = f"{self.selected_minute:02d}" if self.selected_minute is not None else "—"
        return f"**Pick a new time:**\nDay: **{day_str}** | Time: **{hour_str}:{minute_str}** | Timezone: **{self.timezone}**"

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

        await update_post_scheduled_at(self.post_id, unix_ts)
        await interaction.response.edit_message(
            content=f"Time updated to <t:{unix_ts}:F> (<t:{unix_ts}:R>). Refreshing schedule...",
            view=None,
        )
        await repost_all_scheduled(self.bot)
        self.stop()

    async def _on_day(self, interaction: discord.Interaction):
        self.selected_day = interaction.data["values"][0]
        await self._try_submit(interaction)

    async def _on_hour(self, interaction: discord.Interaction):
        self.selected_hour = int(interaction.data["values"][0])
        await self._try_submit(interaction)

    async def _on_minute(self, interaction: discord.Interaction):
        self.selected_minute = int(interaction.data["values"][0])
        await self._try_submit(interaction)

    async def _on_tz_button(self, interaction: discord.Interaction):
        self.tz_visible = True
        self._build_selects()
        await interaction.response.edit_message(content=self._status_text(), view=self)

    async def _on_tz_select(self, interaction: discord.Interaction):
        self.timezone = interaction.data["values"][0]
        await self._try_submit(interaction)


class EditMediaView(discord.ui.View):
    """Shown after clicking 'Change media'. Offers remove or replace."""

    def __init__(self, bot: commands.Bot, post_id: int, has_media: bool):
        super().__init__(timeout=120)
        self.bot = bot
        self.post_id = post_id
        self.handled = False

        if has_media:
            remove_btn = discord.ui.Button(
                label="Remove media",
                style=discord.ButtonStyle.red,
                custom_id=f"rmmedia:{post_id}",
                row=0,
            )
            remove_btn.callback = self._on_remove
            self.add_item(remove_btn)

    async def _on_remove(self, interaction: discord.Interaction):
        if self.handled:
            return
        self.handled = True

        post = await get_post_by_id(self.post_id)
        if post and post.get("image_path"):
            p = Path(post["image_path"])
            if p.exists():
                p.unlink(missing_ok=True)

        await update_post_image(self.post_id, None)
        await interaction.response.edit_message(content="Media removed. Refreshing schedule...", view=None)
        await repost_all_scheduled(self.bot)
        self.stop()


# ---------------------------------------------------------------------------
# Main post buttons (Claim, Not available, Change time/content/media)
# ---------------------------------------------------------------------------

class PostButtonView(discord.ui.View):
    """Buttons attached to each scheduled post message."""

    def __init__(self, bot: commands.Bot, post_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.post_id = post_id

        claim_btn = discord.ui.Button(
            label="Claim", style=discord.ButtonStyle.green,
            custom_id=f"claim:{post_id}", row=0,
        )
        claim_btn.callback = self._on_claim
        self.add_item(claim_btn)

        unavail_btn = discord.ui.Button(
            label="Not available", style=discord.ButtonStyle.red,
            custom_id=f"unavail:{post_id}", row=0,
        )
        unavail_btn.callback = self._on_unavailable
        self.add_item(unavail_btn)

        edit_time_btn = discord.ui.Button(
            label="Change time", style=discord.ButtonStyle.secondary,
            custom_id=f"edittime:{post_id}", row=1,
        )
        edit_time_btn.callback = self._on_edit_time
        self.add_item(edit_time_btn)

        edit_content_btn = discord.ui.Button(
            label="Change content", style=discord.ButtonStyle.secondary,
            custom_id=f"editcontent:{post_id}", row=1,
        )
        edit_content_btn.callback = self._on_edit_content
        self.add_item(edit_content_btn)

        edit_media_btn = discord.ui.Button(
            label="Change media", style=discord.ButtonStyle.secondary,
            custom_id=f"editmedia:{post_id}", row=1,
        )
        edit_media_btn.callback = self._on_edit_media
        self.add_item(edit_media_btn)

    async def _on_claim(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        claimers = await get_claimers_for_post(self.post_id)
        if uid in claimers:
            await remove_claim(self.post_id, uid)
            log.info(f"User {uid} unclaimed post {self.post_id}")
        else:
            await add_claim(self.post_id, uid)
            log.info(f"User {uid} claimed post {self.post_id}")
        await self._update_message(interaction)

    async def _on_unavailable(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        unavailable = await get_unavailable_for_post(self.post_id)
        if uid in unavailable:
            await remove_unavailable(self.post_id, uid)
            log.info(f"User {uid} removed unavailable from post {self.post_id}")
        else:
            await add_unavailable(self.post_id, uid)
            log.info(f"User {uid} marked unavailable for post {self.post_id}")
        await self._update_message(interaction)

    async def _on_edit_time(self, interaction: discord.Interaction):
        view = EditTimeView(self.bot, self.post_id)
        await interaction.response.send_message(
            content=view._status_text(),
            view=view,
            ephemeral=True,
        )

    async def _on_edit_content(self, interaction: discord.Interaction):
        post = await get_post_by_id(self.post_id)
        if not post:
            await interaction.response.send_message("This post no longer exists.", ephemeral=True)
            return
        modal = EditContentModal(self.bot, self.post_id, post["content"])
        await interaction.response.send_modal(modal)

    async def _on_edit_media(self, interaction: discord.Interaction):
        post = await get_post_by_id(self.post_id)
        if not post:
            await interaction.response.send_message("This post no longer exists.", ephemeral=True)
            return

        has_media = bool(post.get("image_path"))
        media_view = EditMediaView(self.bot, self.post_id, has_media)

        status = "Current post has an attached image." if has_media else "No media currently attached."
        await interaction.response.send_message(
            content=(
                f"{status}\n\n"
                f"To upload new media, use `/updatemedia` and attach your image."
            ),
            view=media_view,
            ephemeral=True,
        )

    async def _update_message(self, interaction: discord.Interaction):
        post = await get_post_by_id(self.post_id)
        if not post:
            await interaction.response.send_message("This post no longer exists.", ephemeral=True)
            return

        claimers = await get_claimers_for_post(self.post_id)
        unavailable = await get_unavailable_for_post(self.post_id)

        new_content = format_scheduled_message(
            post["content"], post["scheduled_at"], post["created_by"], claimers, unavailable
        )
        await interaction.response.edit_message(content=new_content)


# ---------------------------------------------------------------------------
# Repost helper
# ---------------------------------------------------------------------------

async def repost_all_scheduled(bot: commands.Bot):
    """Delete all bot messages in the scheduled channel and repost everything in chronological order."""
    channel = bot.get_channel(SCHEDULED_CHANNEL_ID)
    if not channel:
        log.warning("Cannot repost: scheduled channel not found")
        return

    posts = await get_all_scheduled_posts()

    # Invalidate all message IDs in DB first so on_raw_message_delete won't remove posts
    for post in posts:
        await update_post_message_id(post["id"], f"reposting_{post['id']}")

    # Delete all bot messages from the channel
    async for msg in channel.history(limit=500):
        if msg.author == bot.user:
            try:
                await msg.delete()
            except discord.NotFound:
                pass

    # Repost (DESC = furthest first, soonest last = soonest at bottom of chat)
    for post in posts:
        claimers = await get_claimers_for_post(post["id"])
        unavailable = await get_unavailable_for_post(post["id"])
        content = format_scheduled_message(
            post["content"], post["scheduled_at"], post["created_by"], claimers, unavailable
        )
        view = PostButtonView(bot, post["id"])
        file = get_discord_file(post.get("image_path"))
        no_pings = discord.AllowedMentions.none()
        msg = await channel.send(content, view=view, file=file, allowed_mentions=no_pings)
        await update_post_message_id(post["id"], str(msg.id))

    log.info(f"Reposted {len(posts)} scheduled posts in chronological order")


# ---------------------------------------------------------------------------
# New-post modal & scheduling view
# ---------------------------------------------------------------------------

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
                custom_id="tz_select", placeholder="Select timezone", row=0,
            )
            for label, value in TIMEZONE_CHOICES:
                tz_select.add_option(label=label, value=value, default=(self.timezone == value))
            tz_select.callback = self._on_tz_select
            self.add_item(tz_select)
        else:
            tz_button = discord.ui.Button(
                label="Change timezone (default: UK)",
                style=discord.ButtonStyle.secondary,
                custom_id="tz_button", row=0,
            )
            tz_button.callback = self._on_tz_button
            self.add_item(tz_button)

        now = datetime.now(tz=ZoneInfo(self.timezone))
        today = now.date()

        day_select = discord.ui.Select(custom_id="day_select", placeholder="Select day", row=1)
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

        hour_select = discord.ui.Select(custom_id="hour_select", placeholder="Select hour", row=2)
        for h in range(6, 23):
            hour_select.add_option(label=f"{h:02d}:00", value=str(h), default=(self.selected_hour == h))
        hour_select.callback = self._on_hour_select
        self.add_item(hour_select)

        minute_select = discord.ui.Select(custom_id="minute_select", placeholder="Select minutes", row=3)
        for m in [0, 15, 30, 45]:
            minute_select.add_option(label=f":{m:02d}", value=str(m), default=(self.selected_minute == m))
        minute_select.callback = self._on_minute_select
        self.add_item(minute_select)

    def _status_text(self) -> str:
        preview = self.content[:100] + ("..." if len(self.content) > 100 else "")
        parts = [f"**Scheduling post:**\n{_quote_content(preview)}\n"]

        if self.image_path:
            parts.append("Image attached\n")

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

        await insert_post(
            discord_message_id="pending",
            content=self.content,
            scheduled_at=unix_ts,
            created_by=str(self.user_id),
            image_path=self.image_path,
        )

        await interaction.response.edit_message(
            content=f"Post scheduled for <t:{unix_ts}:F> (<t:{unix_ts}:R>). Updating the schedule...",
            view=None,
        )

        await repost_all_scheduled(self.bot)

        log.info(f"Post scheduled by {self.user_id} for {dt.isoformat()}")
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


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_done = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._startup_done:
            return
        self._startup_done = True
        await repost_all_scheduled(self.bot)

    @app_commands.command(name="schedule", description="Schedule a post for X")
    @app_commands.describe(image="Optional image to include with the post")
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

    @app_commands.command(name="updatemedia", description="Add or replace the media on a scheduled post")
    @app_commands.describe(image="The new image to attach")
    async def updatemedia(self, interaction: discord.Interaction, image: discord.Attachment):
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message(
                "That file doesn't look like an image. Please attach a JPG, PNG, GIF, or WebP.",
                ephemeral=True,
            )
            return

        posts = await get_all_scheduled_posts()
        if not posts:
            await interaction.response.send_message("No scheduled posts to update.", ephemeral=True)
            return

        # Show a dropdown to pick which post to update
        view = MediaPostPicker(self.bot, posts, image)
        await interaction.response.send_message(
            "Which post do you want to attach this image to?",
            view=view,
            ephemeral=True,
        )

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

        await repost_all_scheduled(self.bot)


class MediaPostPicker(discord.ui.View):
    """Dropdown to pick which scheduled post gets the new media."""

    def __init__(self, bot: commands.Bot, posts: list[dict], attachment: discord.Attachment):
        super().__init__(timeout=120)
        self.bot = bot
        self.attachment = attachment

        select = discord.ui.Select(placeholder="Select a post", custom_id="media_pick", row=0)
        for post in posts:
            preview = post["content"][:80].replace("\n", " ")
            select.add_option(
                label=preview,
                value=str(post["id"]),
                description=f"Scheduled for <t:{post['scheduled_at']}:f>",
            )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        post_id = int(interaction.data["values"][0])
        post = await get_post_by_id(post_id)
        if not post:
            await interaction.response.edit_message(content="That post no longer exists.", view=None)
            return

        # Remove old image file if it exists
        if post.get("image_path"):
            p = Path(post["image_path"])
            if p.exists():
                p.unlink(missing_ok=True)

        image_path = await save_attachment(self.attachment)
        if not image_path:
            await interaction.response.edit_message(content="Failed to save the image. Try again.", view=None)
            return

        await update_post_image(post_id, image_path)
        await interaction.response.edit_message(content="Media updated. Refreshing schedule...", view=None)
        await repost_all_scheduled(self.bot)
        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
