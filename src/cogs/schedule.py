import asyncio
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import media as media_utils
from config import ARCHIVE_CHANNEL_ID, SCHEDULED_CHANNEL_ID
from database import (
    IMAGES_DIR,
    add_claim,
    add_unavailable,
    cancel_scheduled_post_by_message_id,
    get_cancellations_pending_archive,
    get_all_scheduled_posts,
    get_cancellation_with_post,
    get_claimers_for_post,
    get_open_cancellations_with_archive,
    get_post_by_id,
    get_post_by_message_id,
    get_unavailable_for_post,
    insert_post,
    record_cancellation_archive_message,
    remove_claim,
    remove_unavailable,
    reschedule_cancelled_post,
    update_post_content,
    update_post_image,
    update_post_message_id,
    update_post_scheduled_at,
    update_post_skip_unclaimed_pings,
    update_scheduled_post_discord_settings,
)

log = logging.getLogger("rota-bot.schedule")

DEFAULT_TZ = "Europe/London"
SCHEDULE_PANEL_CUSTOM_ID = "rota:schedule-post:v1"
SCHEDULE_PANEL_TEXT = (
    "**Schedule a post**\n"
    "Use the button below to write a post, optionally attach one media file, "
    "and choose when it should go live.\n"
    "JPG/PNG/WebP, GIF, and MP4 are supported. Videos must be MP4/H.264 and "
    "no longer than 140 seconds; GIF/video limits are checked before scheduling."
)
STARTUP_REFRESH_RETRY_SECONDS = 5
X_STANDARD_POST_CODEPOINTS = 280
DISCORD_CONTENT_LIMIT = 2_000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4_096
SCHEDULE_MEMBER_DISPLAY_LIMIT = 20
MEDIA_PICKER_PAGE_SIZE = 25
DISCORD_DELAY_CHOICES = (
    ("Immediately", 0),
    ("5 minutes", 5),
    ("15 minutes", 15),
    ("30 minutes", 30),
    ("1 hour", 60),
    ("2 hours", 120),
    ("3 hours", 180),
    ("6 hours", 360),
    ("12 hours", 720),
    ("24 hours", 1440),
)

# Compatibility names retained for integrations/tests built against the
# image-only phase. They now validate every supported media type.
SUPPORTED_IMAGE_ERROR = media_utils.SUPPORTED_MEDIA_ERROR
IMAGE_MISMATCH_ERROR = media_utils.MEDIA_MISMATCH_ERROR
IMAGE_SAVE_ERROR = media_utils.MEDIA_SAVE_ERROR
ImageValidationError = media_utils.MediaValidationError
IMAGE_EXTENSIONS = {
    extension: media_format
    for extension, media_format in media_utils.MEDIA_EXTENSIONS.items()
    if media_format in media_utils.IMAGE_FORMATS
}
IMAGE_CONTENT_TYPES = {
    content_type: media_format
    for content_type, media_format in media_utils.MEDIA_CONTENT_TYPES.items()
    if media_format in media_utils.IMAGE_FORMATS
}
NORMALIZED_IMAGE_EXTENSIONS = {
    media_format: extension
    for media_format, extension in media_utils.NORMALIZED_MEDIA_EXTENSIONS.items()
    if media_format in media_utils.IMAGE_FORMATS
}

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

QUICK_DATE_OPTION_COUNT = 25
QUICK_HOUR_START = 6
QUICK_HOUR_END = 22
QUICK_MINUTES = (0, 15, 30, 45)
SCHEDULE_HORIZON_DAYS = 60
EXACT_TIME_MODAL_CUSTOM_ID_PREFIX = "rota:exact-date-time:v1"
UTC = ZoneInfo("UTC")


class ScheduleDateTimeError(ValueError):
    """Raised when a selected local schedule time is invalid."""


def parse_schedule_date(value: str) -> date:
    """Parse an exact, unambiguous ISO calendar date."""
    cleaned = value.strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", cleaned):
        raise ScheduleDateTimeError(
            "Date must be a valid calendar date in YYYY-MM-DD format."
        )
    try:
        return date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ScheduleDateTimeError(
            "Date must be a valid calendar date in YYYY-MM-DD format."
        ) from exc


def parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse HHMM or HH:MM without accepting ambiguous variants."""
    cleaned = value.strip()
    match = re.fullmatch(r"([0-9]{2})(?::?)([0-9]{2})", cleaned)
    if match is None or len(cleaned) not in (4, 5):
        raise ScheduleDateTimeError(
            "Time must be exactly four digits (HHMM) or HH:MM."
        )

    hour, minute = (int(part) for part in match.groups())
    if not 0 <= hour <= 23:
        raise ScheduleDateTimeError("Hour must be between 00 and 23.")
    if not 0 <= minute <= 59:
        raise ScheduleDateTimeError("Minute must be between 00 and 59.")
    return hour, minute


def _schedule_timezone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (KeyError, TypeError, ValueError) as exc:
        raise ScheduleDateTimeError("The selected timezone is invalid.") from exc


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include timezone information")
    return now.astimezone(UTC)


def validate_schedule_day(
    selected_day: date,
    timezone: str,
    *,
    now: datetime | None = None,
) -> None:
    """Validate a local calendar date against past and horizon limits."""
    tz = _schedule_timezone(timezone)
    local_today = _utc_now(now).astimezone(tz).date()
    if selected_day < local_today:
        raise ScheduleDateTimeError(
            "That date is in the past. Choose today or a future date."
        )

    latest_day = local_today + timedelta(days=SCHEDULE_HORIZON_DAYS)
    if selected_day > latest_day:
        raise ScheduleDateTimeError(
            f"Date must be no later than {latest_day.isoformat()} "
            f"({SCHEDULE_HORIZON_DAYS} calendar days from today in {timezone})."
        )


def validate_scheduled_datetime(
    selected_day: str,
    selected_hour: int,
    selected_minute: int,
    timezone: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """Build and validate a future local datetime using an IANA timezone."""
    day = parse_schedule_date(selected_day)
    now_utc = _utc_now(now)
    if (
        not isinstance(selected_hour, int)
        or isinstance(selected_hour, bool)
        or not 0 <= selected_hour <= 23
    ):
        raise ScheduleDateTimeError("Hour must be between 00 and 23.")
    if (
        not isinstance(selected_minute, int)
        or isinstance(selected_minute, bool)
        or not 0 <= selected_minute <= 59
    ):
        raise ScheduleDateTimeError("Minute must be between 00 and 59.")

    validate_schedule_day(day, timezone, now=now_utc)
    tz = _schedule_timezone(timezone)
    wall_time = (
        day.year,
        day.month,
        day.day,
        selected_hour,
        selected_minute,
        0,
    )

    # ZoneInfo permits construction of nonexistent wall times. A UTC round trip
    # distinguishes real local times while retaining fold=0 for ambiguous ones.
    local_dt = None
    for fold in (0, 1):
        candidate = datetime(
            *wall_time,
            tzinfo=tz,
            fold=fold,
        )
        round_trip = candidate.astimezone(UTC).astimezone(tz)
        round_trip_wall_time = (
            round_trip.year,
            round_trip.month,
            round_trip.day,
            round_trip.hour,
            round_trip.minute,
            round_trip.second,
        )
        if round_trip_wall_time == wall_time:
            local_dt = candidate
            break
    if local_dt is None:
        raise ScheduleDateTimeError(
            "That local time does not exist in the selected timezone because "
            "of a daylight-saving change."
        )

    if local_dt.astimezone(UTC) <= now_utc:
        raise ScheduleDateTimeError(
            "That time is in the past or present. Choose a future time."
        )
    return local_dt


class _TimePickerView(discord.ui.View):
    """Shared exact/quick time state and server-side validation."""

    def __init__(self, custom_ids: dict[str, str]):
        super().__init__(timeout=300)
        self.selected_day: str | None = None
        self.selected_hour: int | None = None
        self.selected_minute: int | None = None
        self.timezone = DEFAULT_TZ
        self.tz_visible = False
        self.submitted = False
        self._custom_ids = custom_ids
        self._build_selects()

    def _build_selects(self):
        self.clear_items()

        if self.tz_visible:
            tz_select = discord.ui.Select(
                custom_id=self._custom_ids["timezone_select"],
                placeholder="Select timezone",
                row=0,
            )
            for label, value in TIMEZONE_CHOICES:
                tz_select.add_option(
                    label=label,
                    value=value,
                    default=(self.timezone == value),
                )
            tz_select.callback = self._on_tz_select
            self.add_item(tz_select)
        else:
            tz_button = discord.ui.Button(
                label="Change timezone (default: UK)",
                style=discord.ButtonStyle.secondary,
                custom_id=self._custom_ids["timezone_button"],
                row=0,
            )
            tz_button.callback = self._on_tz_button
            self.add_item(tz_button)

        today = datetime.now(tz=_schedule_timezone(self.timezone)).date()
        day_select = discord.ui.Select(
            custom_id=self._custom_ids["day"],
            placeholder="Select day",
            row=1,
        )
        for offset in range(QUICK_DATE_OPTION_COUNT):
            day = today + timedelta(days=offset)
            value = day.isoformat()
            if offset == 0:
                label = f"Today — {day.strftime('%A %d %b')}"
            elif offset == 1:
                label = f"Tomorrow — {day.strftime('%A %d %b')}"
            else:
                label = day.strftime("%A %d %b")
            day_select.add_option(
                label=label,
                value=value,
                default=(self.selected_day == value),
            )
        day_select.callback = self._on_day_select
        self.add_item(day_select)

        hour_select = discord.ui.Select(
            custom_id=self._custom_ids["hour"],
            placeholder="Select hour",
            row=2,
        )
        for hour in range(QUICK_HOUR_START, QUICK_HOUR_END + 1):
            hour_select.add_option(
                label=f"{hour:02d}:00",
                value=str(hour),
                default=(self.selected_hour == hour),
            )
        hour_select.callback = self._on_hour_select
        self.add_item(hour_select)

        minute_select = discord.ui.Select(
            custom_id=self._custom_ids["minute"],
            placeholder="Select minutes",
            row=3,
        )
        for minute in QUICK_MINUTES:
            minute_select.add_option(
                label=f":{minute:02d}",
                value=str(minute),
                default=(self.selected_minute == minute),
            )
        minute_select.callback = self._on_minute_select
        self.add_item(minute_select)

        exact_button = discord.ui.Button(
            label="Enter exact date/time",
            style=discord.ButtonStyle.secondary,
            custom_id=self._custom_ids["exact"],
            row=4,
        )
        exact_button.callback = self._on_exact_time
        self.add_item(exact_button)

    def _selection_status(self) -> str:
        day = self.selected_day or "—"
        hour = (
            f"{self.selected_hour:02d}"
            if self.selected_hour is not None
            else "—"
        )
        minute = (
            f"{self.selected_minute:02d}"
            if self.selected_minute is not None
            else "—"
        )
        return (
            f"Day: **{day}** | Time: **{hour}:{minute}** | "
            f"Timezone: **{self.timezone}**"
        )

    async def _show_picker(
        self,
        interaction: discord.Interaction,
        error: str | None = None,
    ):
        self._build_selects()
        content = self._status_text()
        if error:
            content += f"\n\n{error}"
        await interaction.response.edit_message(
            content=content,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _try_submit(self, interaction: discord.Interaction):
        if self.submitted:
            return
        if (
            self.selected_day is None
            or self.selected_hour is None
            or self.selected_minute is None
        ):
            await self._show_picker(interaction)
            return

        try:
            local_dt = validate_scheduled_datetime(
                self.selected_day,
                self.selected_hour,
                self.selected_minute,
                self.timezone,
            )
        except ScheduleDateTimeError as exc:
            await self._show_picker(interaction, str(exc))
            return

        self.submitted = True
        await self._finalize_time(
            interaction,
            int(local_dt.timestamp()),
            local_dt,
        )

    async def _apply_manual_input(
        self,
        interaction: discord.Interaction,
        date_value: str,
        time_value: str,
    ):
        date_value = date_value.strip()
        time_value = time_value.strip()
        missing_fields = []
        if not date_value and self.selected_day is None:
            missing_fields.append(
                "Date is required because no day is selected."
            )
        if not time_value and (
            self.selected_hour is None or self.selected_minute is None
        ):
            missing_fields.append(
                "Time is required because a complete time is not selected."
            )
        if missing_fields:
            await self._show_picker(
                interaction,
                " ".join(missing_fields),
            )
            return

        candidate_day = self.selected_day
        candidate_hour = self.selected_hour
        candidate_minute = self.selected_minute
        try:
            if date_value:
                parsed_day = parse_schedule_date(date_value)
                validate_schedule_day(parsed_day, self.timezone)
                candidate_day = parsed_day.isoformat()
            if time_value:
                candidate_hour, candidate_minute = parse_schedule_time(time_value)
            if (
                candidate_day is not None
                and candidate_hour is not None
                and candidate_minute is not None
            ):
                validate_scheduled_datetime(
                    candidate_day,
                    candidate_hour,
                    candidate_minute,
                    self.timezone,
                )
        except ScheduleDateTimeError as exc:
            await self._show_picker(interaction, str(exc))
            return

        self.selected_day = candidate_day
        self.selected_hour = candidate_hour
        self.selected_minute = candidate_minute
        await self._try_submit(interaction)

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
        await self._show_picker(interaction)

    async def _on_tz_select(self, interaction: discord.Interaction):
        self.timezone = interaction.data["values"][0]
        await self._try_submit(interaction)

    async def _on_exact_time(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ExactDateTimeModal(self))

    def _status_text(self) -> str:
        raise NotImplementedError

    async def _finalize_time(
        self,
        interaction: discord.Interaction,
        unix_ts: int,
        local_dt: datetime,
    ):
        raise NotImplementedError


class ExactDateTimeModal(discord.ui.Modal):
    """Apply optional exact date/time components to a picker."""

    def __init__(self, picker: _TimePickerView):
        super().__init__(
            title="Enter exact date/time",
            custom_id=(
                f"{EXACT_TIME_MODAL_CUSTOM_ID_PREFIX}:{uuid.uuid4().hex}"
            ),
        )
        self.picker = picker
        self.date_input = discord.ui.TextInput(
            label="Date (required if not selected)",
            placeholder=picker.selected_day or "YYYY-MM-DD",
            required=False,
            max_length=10,
        )
        selected_time = (
            f"{picker.selected_hour:02d}{picker.selected_minute:02d}"
            if picker.selected_hour is not None
            and picker.selected_minute is not None
            else "HHMM or HH:MM"
        )
        self.time_input = discord.ui.TextInput(
            label="Time (required if not selected)",
            placeholder=selected_time,
            required=False,
            max_length=5,
        )
        self.add_item(self.date_input)
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.picker._apply_manual_input(
            interaction,
            self.date_input.value,
            self.time_input.value,
        )


def _quote_content(content: str) -> str:
    return "\n".join(f"> {line}" for line in content.split("\n"))


def _format_discord_delay(minutes: int) -> str:
    if minutes == 0:
        return "immediately"
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def premium_long_post_status(content: str) -> str | None:
    """Return a concise, non-error status for X Premium long posts."""
    if len(content) <= X_STANDARD_POST_CODEPOINTS:
        return None
    return f"**X Premium long post** — {len(content):,} characters (accepted)."


def build_post_embed(content: str) -> discord.Embed:
    """Keep up to 4,000 authored characters within Discord's embed limit."""
    if len(content) > DISCORD_EMBED_DESCRIPTION_LIMIT:
        content = content[: DISCORD_EMBED_DESCRIPTION_LIMIT - 1] + "…"
    return discord.Embed(description=content)


def _member_sort_key(user_id: str) -> tuple[int, int | str]:
    value = str(user_id)
    return (0, int(value)) if value.isdigit() else (1, value)


def _format_member_mentions(label: str, user_ids: list[str]) -> str:
    ordered = sorted({str(user_id) for user_id in user_ids}, key=_member_sort_key)
    shown = ordered[:SCHEDULE_MEMBER_DISPLAY_LIMIT]
    mentions = ", ".join(f"<@{user_id}>" for user_id in shown)
    remaining = len(ordered) - len(shown)
    suffix = f" (+{remaining} more)" if remaining else ""
    return f"{label}: {mentions}{suffix}"


def format_scheduled_message(
    content: str,
    scheduled_at: int,
    created_by_id: str,
    claimers: list[str] | None = None,
    unavailable: list[str] | None = None,
    skip_unclaimed_pings: bool = True,
    post_to_x: bool = True,
    post_to_discord: bool = True,
    discord_delay_minutes: int = 0,
) -> str:
    lines = [
        "**Scheduled Post**",
        "",
    ]
    premium_status = premium_long_post_status(content)
    if premium_status:
        lines.append(premium_status)
        lines.append("")
    if not post_to_x:
        lines.append(
            "**Manual X** — the bot will not tweet this. Claimers post on X themselves; "
            "no tweet links are sent to announcement channels."
        )
        lines.append("")
    elif not post_to_discord:
        lines.append(
            "**Discord live links off** — the bot will post to X but will not send "
            "the x.com link to the configured live-link channels."
        )
        lines.append("")
    elif discord_delay_minutes:
        lines.append(
            "**Discord live-link delay** — the x.com link will be sent "
            f"{_format_discord_delay(discord_delay_minutes)} after X confirms publication."
        )
        lines.append("")
    lines.extend(
        [
            f"Scheduled for: <t:{scheduled_at}:F> (<t:{scheduled_at}:R>)",
            f"Scheduled by: <@{created_by_id}>",
            "",
        ]
    )
    if claimers:
        lines.append(_format_member_mentions("Claimed by", claimers))
    elif skip_unclaimed_pings:
        lines.append(
            "**Unclaimed** — claiming is optional; the team will **not** be pinged while this stays unclaimed."
        )
    else:
        lines.append("**Unclaimed** — click Claim to take this post!")

    if unavailable:
        lines.append(_format_member_mentions("Not available", unavailable))

    message = "\n".join(lines)
    if len(message) > DISCORD_CONTENT_LIMIT:
        raise ValueError("scheduled-post metadata exceeded Discord's content limit")
    return message


def get_discord_file(media_path: str | None) -> discord.File | None:
    if not media_path:
        return None
    p = Path(media_path)
    if p.exists():
        return discord.File(p, filename=p.name)
    return None


def format_cancelled_archive_message(cancellation: dict) -> str:
    lines = [
        f"**Scheduled post cancelled** <t:{cancellation['cancelled_at']}:F>",
        "",
        (
            "Originally scheduled for: "
            f"<t:{cancellation['cancelled_scheduled_at']}:F>"
        ),
        f"Scheduled by: <@{cancellation['created_by']}>",
    ]
    if cancellation.get("rescheduled_at"):
        lines.extend(
            [
                "",
                (
                    f"**Rescheduled for:** <t:{cancellation['scheduled_at']}:F> "
                    f"(<t:{cancellation['scheduled_at']}:R>)"
                ),
                f"Rescheduled by: <@{cancellation['rescheduled_by']}>",
            ]
        )
    return "\n".join(lines)


def _get_internal_deleted_message_ids(bot: commands.Bot) -> set[str]:
    message_ids = vars(bot).get("_rota_internal_deleted_message_ids")
    if message_ids is None:
        message_ids = set()
        vars(bot)["_rota_internal_deleted_message_ids"] = message_ids
    return message_ids


def precheck_media_attachment(attachment: discord.Attachment) -> str:
    return media_utils.precheck_media_attachment(attachment)


def precheck_image_attachment(attachment: discord.Attachment) -> str:
    """Backward-compatible alias for generic media metadata validation."""
    return precheck_media_attachment(attachment)


def detect_image_format(data: bytes) -> str | None:
    return media_utils.detect_image_format(data)


async def save_validated_media_attachment(
    attachment: discord.Attachment,
) -> tuple[str | None, str | None]:
    return await media_utils.save_validated_media_attachment(
        attachment,
        storage_dir=IMAGES_DIR,
    )


async def save_validated_image_attachment(
    attachment: discord.Attachment,
) -> tuple[str | None, str | None]:
    """Backward-compatible alias for generic media validation/storage."""
    return await save_validated_media_attachment(attachment)


async def delete_uncommitted_media(media_path: str | None) -> None:
    if not media_path:
        return
    path = Path(media_path)
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        log.exception("Failed to remove uncommitted media %s", path)


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


class EditTimeView(_TimePickerView):
    """Dropdowns to pick a new time for an existing post."""

    def __init__(self, bot: commands.Bot, post_id: int):
        self.bot = bot
        self.post_id = post_id
        super().__init__(
            {
                "timezone_select": "et_tz_select",
                "timezone_button": "et_tz_button",
                "day": "et_day",
                "hour": "et_hour",
                "minute": "et_minute",
                "exact": "et_exact_time",
            }
        )

    def _status_text(self) -> str:
        return f"**Pick a new time:**\n{self._selection_status()}"

    async def _finalize_time(
        self,
        interaction: discord.Interaction,
        unix_ts: int,
        local_dt: datetime,
    ):
        await update_post_scheduled_at(self.post_id, unix_ts)
        await interaction.response.edit_message(
            content=f"Time updated to <t:{unix_ts}:F> (<t:{unix_ts}:R>). Refreshing schedule...",
            view=None,
        )
        await repost_all_scheduled(self.bot)
        self.stop()

    async def _on_day(self, interaction: discord.Interaction):
        await self._on_day_select(interaction)

    async def _on_hour(self, interaction: discord.Interaction):
        await self._on_hour_select(interaction)

    async def _on_minute(self, interaction: discord.Interaction):
        await self._on_minute_select(interaction)


class RescheduleCancelledView(_TimePickerView):
    """Pick a new time while retaining the original cancellation record."""

    def __init__(
        self,
        bot: commands.Bot,
        cancellation_id: int,
        user_id: int,
        archive_message,
    ):
        self.bot = bot
        self.cancellation_id = cancellation_id
        self.user_id = user_id
        self.archive_message = archive_message
        suffix = str(cancellation_id)
        super().__init__(
            {
                "timezone_select": f"rc_tz_select:{suffix}",
                "timezone_button": f"rc_tz_button:{suffix}",
                "day": f"rc_day:{suffix}",
                "hour": f"rc_hour:{suffix}",
                "minute": f"rc_minute:{suffix}",
                "exact": f"rc_exact_time:{suffix}",
            }
        )

    def _status_text(self) -> str:
        return f"**Pick a new time for the cancelled post:**\n{self._selection_status()}"

    async def _finalize_time(
        self,
        interaction: discord.Interaction,
        unix_ts: int,
        local_dt: datetime,
    ):
        rescheduled_at = int(time.time())
        updated = await reschedule_cancelled_post(
            self.cancellation_id,
            unix_ts,
            str(interaction.user.id),
            rescheduled_at,
        )
        if not updated:
            await interaction.response.edit_message(
                content="This cancellation has already been rescheduled.",
                view=None,
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content=(
                f"Post rescheduled for <t:{unix_ts}:F> (<t:{unix_ts}:R>). "
                "Refreshing the schedule..."
            ),
            view=None,
        )

        cancellation = await get_cancellation_with_post(self.cancellation_id)
        if cancellation and self.archive_message is not None:
            try:
                await self.archive_message.edit(
                    content=format_cancelled_archive_message(cancellation),
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                log.exception(
                    "Post %s was rescheduled but cancellation archive message "
                    "could not be updated",
                    cancellation["id"],
                )

        await repost_all_scheduled(self.bot)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who started this reschedule can use it.",
                ephemeral=True,
            )
            return False
        return True


class CancelledPostView(discord.ui.View):
    """Persistent archive control for restoring a cancelled scheduled post."""

    def __init__(self, bot: commands.Bot, cancellation_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.cancellation_id = cancellation_id
        button = discord.ui.Button(
            label="Reschedule",
            style=discord.ButtonStyle.primary,
            custom_id=f"reschedule_cancelled:{cancellation_id}",
        )
        button.callback = self._on_reschedule
        self.add_item(button)

    async def _on_reschedule(self, interaction: discord.Interaction):
        cancellation = await get_cancellation_with_post(self.cancellation_id)
        if (
            cancellation is None
            or cancellation.get("rescheduled_at") is not None
            or cancellation.get("status") != "cancelled"
        ):
            await interaction.response.send_message(
                "This cancellation has already been rescheduled.",
                ephemeral=True,
            )
            return

        view = RescheduleCancelledView(
            self.bot,
            self.cancellation_id,
            interaction.user.id,
            interaction.message,
        )
        await interaction.response.send_message(
            view._status_text(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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
        await update_post_image(self.post_id, None)
        if post and post.get("image_path"):
            p = Path(post["image_path"])
            try:
                p.unlink(missing_ok=True)
            except OSError:
                log.exception(
                    "Media removed from post %s but stored file could not be deleted: %s",
                    self.post_id,
                    p,
                )
        await interaction.response.edit_message(content="Media removed. Refreshing schedule...", view=None)
        await repost_all_scheduled(self.bot)
        self.stop()


# ---------------------------------------------------------------------------
# Main post buttons (Claim, Not available, Change time/content/media)
# ---------------------------------------------------------------------------

async def _scheduled_post_message_payload(
    bot: commands.Bot,
    post_id: int,
) -> dict | None:
    post = await get_post_by_id(post_id)
    if not post or post.get("status", "scheduled") != "scheduled":
        return None

    claimers = await get_claimers_for_post(post_id)
    unavailable = await get_unavailable_for_post(post_id)
    skip_pings = bool(post.get("skip_unclaimed_pings"))
    post_to_x = bool(post.get("post_to_x", 1))
    post_to_discord = bool(post.get("post_to_discord", 1))
    discord_delay_minutes = int(post.get("discord_delay_minutes", 0))
    return {
        "content": format_scheduled_message(
            post["content"],
            post["scheduled_at"],
            post["created_by"],
            claimers,
            unavailable,
            skip_unclaimed_pings=skip_pings,
            post_to_x=post_to_x,
            post_to_discord=post_to_discord,
            discord_delay_minutes=discord_delay_minutes,
        ),
        "embed": build_post_embed(post["content"]),
        "view": PostButtonView(
            bot,
            post_id,
            skip_unclaimed_pings=skip_pings,
            post_to_discord=post_to_discord,
            discord_delay_minutes=discord_delay_minutes,
        ),
        "allowed_mentions": discord.AllowedMentions.none(),
    }


class CancelPostConfirmationView(discord.ui.View):
    """Require an explicit confirmation instead of treating deletion as cancel."""

    def __init__(
        self,
        bot: commands.Bot,
        post_id: int,
        user_id: int,
        schedule_message,
    ):
        super().__init__(timeout=60)
        self.bot = bot
        self.post_id = post_id
        self.user_id = user_id
        self.schedule_message = schedule_message

        confirm = discord.ui.Button(
            label="Confirm cancellation",
            style=discord.ButtonStyle.danger,
            custom_id=f"confirm_cancel:{post_id}",
        )
        confirm.callback = self._on_confirm
        self.add_item(confirm)

        keep = discord.ui.Button(
            label="Keep scheduled",
            style=discord.ButtonStyle.secondary,
            custom_id=f"keep_scheduled:{post_id}",
        )
        keep.callback = self._on_keep
        self.add_item(keep)

    async def _on_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with get_schedule_refresh_lock(self.bot):
            post = await get_post_by_id(self.post_id)
            cancellation = None
            if post and post.get("status") == "scheduled":
                cancellation = await cancel_scheduled_post_by_message_id(
                    str(post["discord_message_id"]),
                    int(time.time()),
                )

        if cancellation is None:
            await interaction.edit_original_response(
                content="This post is no longer scheduled.",
                view=None,
            )
            self.stop()
            return

        archived = await archive_cancellation(self.bot, cancellation)
        if self.schedule_message is not None:
            message_id = str(self.schedule_message.id)
            internal_deletions = _get_internal_deleted_message_ids(self.bot)
            internal_deletions.add(message_id)
            try:
                await self.schedule_message.delete()
            except discord.NotFound:
                internal_deletions.discard(message_id)
            except (discord.Forbidden, discord.HTTPException):
                internal_deletions.discard(message_id)
                log.exception(
                    "Post %s was cancelled but its schedule card could not be deleted",
                    self.post_id,
                )

        await interaction.edit_original_response(
            content=(
                "Post cancelled and moved to the archive."
                if archived
                else (
                    "Post cancelled. The archive channel was unavailable; "
                    "the bot will retry archiving after restart."
                )
            ),
            view=None,
        )
        log.info(
            "Post %s explicitly cancelled by %s (archived=%s)",
            self.post_id,
            interaction.user.id,
            archived,
        )
        self.stop()

    async def _on_keep(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Cancellation aborted; the post remains scheduled.",
            view=None,
        )
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who opened this confirmation can use it.",
                ephemeral=True,
            )
            return False
        return True


class DiscordDelayPickerView(discord.ui.View):
    """Ephemeral delay picker kept off the public scheduled-post card."""

    def __init__(
        self,
        bot: commands.Bot,
        post_id: int,
        user_id: int,
        schedule_message,
        current_delay_minutes: int,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.post_id = post_id
        self.user_id = user_id
        self.schedule_message = schedule_message

        select = discord.ui.Select(
            placeholder=(
                f"Current delay: {_format_discord_delay(current_delay_minutes)}"
            ),
            custom_id=f"setdiscorddelay:{post_id}",
        )
        for label, minutes in DISCORD_DELAY_CHOICES:
            select.add_option(
                label=label,
                value=str(minutes),
                default=(current_delay_minutes == minutes),
            )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        delay_minutes = int(interaction.data["values"][0])
        async with get_schedule_refresh_lock(self.bot):
            post = await get_post_by_id(self.post_id)
            if not post or post.get("status", "scheduled") != "scheduled":
                await interaction.response.edit_message(
                    content="This post is no longer scheduled.",
                    view=None,
                )
                self.stop()
                return
            updated = await update_scheduled_post_discord_settings(
                self.post_id,
                bool(post.get("post_to_discord", 1)),
                delay_minutes,
            )

        if not updated:
            await interaction.response.edit_message(
                content="This post is no longer scheduled.",
                view=None,
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content=(
                "Discord live-link delay updated to "
                f"**{_format_discord_delay(delay_minutes)}**."
            ),
            view=None,
        )
        payload = await _scheduled_post_message_payload(self.bot, self.post_id)
        if payload and self.schedule_message is not None:
            try:
                await self.schedule_message.edit(**payload)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                log.exception(
                    "Post %s delay changed but its schedule card could not be updated",
                    self.post_id,
                )
        log.info(
            "Post %s discord_delay_minutes=%s (by %s)",
            self.post_id,
            delay_minutes,
            interaction.user.id,
        )
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who opened this delay menu can use it.",
                ephemeral=True,
            )
            return False
        return True


class PostButtonView(discord.ui.View):
    """Buttons attached to each scheduled post message."""

    def __init__(
        self,
        bot: commands.Bot,
        post_id: int,
        skip_unclaimed_pings: bool = True,
        post_to_discord: bool = True,
        discord_delay_minutes: int = 0,
    ):
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

        if skip_unclaimed_pings:
            ping_btn = discord.ui.Button(
                label="Require a claimer",
                style=discord.ButtonStyle.secondary,
                custom_id=f"skippings:{post_id}",
                row=2,
            )
        else:
            ping_btn = discord.ui.Button(
                label="Claimer not required",
                style=discord.ButtonStyle.secondary,
                custom_id=f"skippings:{post_id}",
                row=2,
            )
        ping_btn.callback = self._on_toggle_skip_unclaimed_pings
        self.add_item(ping_btn)

        cancel_btn = discord.ui.Button(
            label="Cancel post",
            style=discord.ButtonStyle.danger,
            custom_id=f"cancelpost:{post_id}",
            row=2,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

        discord_btn = discord.ui.Button(
            label=(
                "Discord live links: on"
                if post_to_discord
                else "Discord live links: off"
            ),
            style=(
                discord.ButtonStyle.green
                if post_to_discord
                else discord.ButtonStyle.red
            ),
            custom_id=f"postdiscord:{post_id}",
            row=3,
        )
        discord_btn.callback = self._on_toggle_post_to_discord
        self.add_item(discord_btn)

        delay_btn = discord.ui.Button(
            label="Change delay",
            style=discord.ButtonStyle.secondary,
            custom_id=f"changedelay:{post_id}",
            row=4,
            disabled=not post_to_discord,
        )
        delay_btn.callback = self._on_change_discord_delay
        self.add_item(delay_btn)

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

    async def _on_toggle_skip_unclaimed_pings(self, interaction: discord.Interaction):
        post = await get_post_by_id(self.post_id)
        if not post:
            await interaction.response.send_message("This post no longer exists.", ephemeral=True)
            return
        new_val = not bool(post.get("skip_unclaimed_pings"))
        await update_post_skip_unclaimed_pings(self.post_id, new_val)
        log.info(f"Post {self.post_id} skip_unclaimed_pings={new_val} (by {interaction.user.id})")
        await self._update_message(interaction)

    async def _on_cancel(self, interaction: discord.Interaction):
        post = await get_post_by_id(self.post_id)
        if not post or post.get("status") != "scheduled":
            await interaction.response.send_message(
                "This post is no longer scheduled.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Cancel this post and move it to the archive?",
            view=CancelPostConfirmationView(
                self.bot,
                self.post_id,
                interaction.user.id,
                interaction.message,
            ),
            ephemeral=True,
        )

    async def _on_toggle_post_to_discord(
        self,
        interaction: discord.Interaction,
    ):
        async with get_schedule_refresh_lock(self.bot):
            post = await get_post_by_id(self.post_id)
            if not post or post.get("status", "scheduled") != "scheduled":
                await interaction.response.send_message(
                    "This post is no longer scheduled.",
                    ephemeral=True,
                )
                return
            post_to_discord = not bool(post.get("post_to_discord", 1))
            updated = await update_scheduled_post_discord_settings(
                self.post_id,
                post_to_discord,
                int(post.get("discord_delay_minutes", 0)),
            )
        if not updated:
            await interaction.response.send_message(
                "This post is no longer scheduled.",
                ephemeral=True,
            )
            return
        log.info(
            "Post %s post_to_discord=%s (by %s)",
            self.post_id,
            post_to_discord,
            interaction.user.id,
        )
        await self._update_message(interaction)

    async def _on_change_discord_delay(
        self,
        interaction: discord.Interaction,
    ):
        post = await get_post_by_id(self.post_id)
        if not post or post.get("status", "scheduled") != "scheduled":
            await interaction.response.send_message(
                "This post is no longer scheduled.",
                ephemeral=True,
            )
            return

        current_delay = int(post.get("discord_delay_minutes", 0))
        await interaction.response.send_message(
            (
                "Choose when successful X links should be sent to the "
                "configured Discord live-link channels."
            ),
            view=DiscordDelayPickerView(
                self.bot,
                self.post_id,
                interaction.user.id,
                interaction.message,
                current_delay,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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

        status = "Current post has attached media." if has_media else "No media currently attached."
        await interaction.response.send_message(
            content=(
                f"{status}\n\n"
                "To add or replace it, use `/updatemedia` and attach one JPG, "
                "PNG, WebP, GIF, or MP4. Video must be MP4/H.264 and no longer "
                "than 140 seconds; GIF/video limits are checked."
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
        skip_pings = bool(post.get("skip_unclaimed_pings"))

        post_to_x = bool(post.get("post_to_x", 1))
        post_to_discord = bool(post.get("post_to_discord", 1))
        discord_delay_minutes = int(post.get("discord_delay_minutes", 0))
        new_content = format_scheduled_message(
            post["content"],
            post["scheduled_at"],
            post["created_by"],
            claimers,
            unavailable,
            skip_unclaimed_pings=skip_pings,
            post_to_x=post_to_x,
            post_to_discord=post_to_discord,
            discord_delay_minutes=discord_delay_minutes,
        )
        new_view = PostButtonView(
            self.bot,
            self.post_id,
            skip_unclaimed_pings=skip_pings,
            post_to_discord=post_to_discord,
            discord_delay_minutes=discord_delay_minutes,
        )
        await interaction.response.edit_message(
            content=new_content,
            embed=build_post_embed(post["content"]),
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ---------------------------------------------------------------------------
# Repost helper
# ---------------------------------------------------------------------------

class SchedulePanelView(discord.ui.View):
    """Persistent entry point for the unified scheduling flow."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This button can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Schedule post",
        style=discord.ButtonStyle.primary,
        custom_id=SCHEDULE_PANEL_CUSTOM_ID,
    )
    async def schedule_post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            SchedulePostModal(self.bot, interaction.user.id)
        )


async def archive_cancellation(bot: commands.Bot, cancellation: dict) -> bool:
    """Post one durable cancellation record with a reschedule control."""
    if cancellation.get("cancellation_archive_message_id"):
        return True

    channel = bot.get_channel(ARCHIVE_CHANNEL_ID)
    if channel is None:
        log.warning(
            "Cancellation %s could not be archived: channel %s was not found",
            cancellation["cancellation_id"],
            ARCHIVE_CHANNEL_ID,
        )
        return False

    view = None
    if (
        cancellation.get("rescheduled_at") is None
        and cancellation.get("status") == "cancelled"
    ):
        view = CancelledPostView(bot, cancellation["cancellation_id"])

    send_kwargs = {
        "embed": build_post_embed(cancellation["content"]),
        "view": view,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    file = get_discord_file(cancellation.get("image_path"))
    if file is not None:
        send_kwargs["file"] = file

    try:
        message = await channel.send(
            format_cancelled_archive_message(cancellation),
            **send_kwargs,
        )
    except (discord.Forbidden, discord.HTTPException):
        log.exception(
            "Failed to archive cancellation %s",
            cancellation["cancellation_id"],
        )
        return False

    recorded = await record_cancellation_archive_message(
        cancellation["cancellation_id"],
        str(message.id),
    )
    if not recorded:
        log.warning(
            "Cancellation %s archive message %s was sent but not recorded",
            cancellation["cancellation_id"],
            message.id,
        )
        return False
    return True


async def archive_pending_cancellations(bot: commands.Bot) -> bool:
    success = True
    for cancellation in await get_cancellations_pending_archive():
        if not await archive_cancellation(bot, cancellation):
            success = False
    return success


async def send_schedule_panel(channel, bot: commands.Bot) -> discord.Message | None:
    """Send one scheduling panel to the configured scheduled channel."""
    channel_id = getattr(channel, "id", None)
    if channel_id != SCHEDULED_CHANNEL_ID:
        log.warning(
            "Refusing to send schedule panel outside configured channel %s (got %s)",
            SCHEDULED_CHANNEL_ID,
            channel_id,
        )
        return None

    try:
        return await channel.send(
            SCHEDULE_PANEL_TEXT,
            view=SchedulePanelView(bot),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        log.exception("Failed to send schedule panel in channel %s", channel_id)
        return None


def get_schedule_refresh_lock(bot: commands.Bot) -> asyncio.Lock:
    """Return the single refresh lock owned by this bot instance."""
    lock = vars(bot).get("_rota_schedule_repost_lock")
    if lock is None:
        lock = asyncio.Lock()
        vars(bot)["_rota_schedule_repost_lock"] = lock
    return lock


# Backward-compatible internal name used by earlier phase tests/integrations.
_get_repost_lock = get_schedule_refresh_lock


async def repost_all_scheduled(bot: commands.Bot) -> bool:
    """Atomically refresh scheduled messages and return whether it succeeded."""
    async with get_schedule_refresh_lock(bot):
        channel = bot.get_channel(SCHEDULED_CHANNEL_ID)
        if not channel:
            log.warning(
                "Schedule refresh failed: configured channel %s was not found",
                SCHEDULED_CHANNEL_ID,
            )
            return False

        stage = "querying scheduled posts"
        try:
            # Query only after acquiring the lock so queued refreshes use current data.
            posts = await get_all_scheduled_posts()

            stage = "invalidating stored message IDs"
            for post in posts:
                await update_post_message_id(
                    post["id"], f"reposting_{post['id']}"
                )

            stage = "deleting existing bot messages"
            internal_deletions = _get_internal_deleted_message_ids(bot)
            async for msg in channel.history(limit=500):
                if msg.author == bot.user:
                    message_id = str(msg.id)
                    internal_deletions.add(message_id)
                    try:
                        await msg.delete()
                    except discord.NotFound:
                        internal_deletions.discard(message_id)
                    except Exception:
                        internal_deletions.discard(message_id)
                        raise

            stage = "sending scheduled post messages"
            for post in posts:
                claimers = await get_claimers_for_post(post["id"])
                unavailable = await get_unavailable_for_post(post["id"])
                skip_pings = bool(post.get("skip_unclaimed_pings"))
                post_to_x = bool(post.get("post_to_x", 1))
                post_to_discord = bool(post.get("post_to_discord", 1))
                discord_delay_minutes = int(
                    post.get("discord_delay_minutes", 0)
                )
                content = format_scheduled_message(
                    post["content"],
                    post["scheduled_at"],
                    post["created_by"],
                    claimers,
                    unavailable,
                    skip_unclaimed_pings=skip_pings,
                    post_to_x=post_to_x,
                    post_to_discord=post_to_discord,
                    discord_delay_minutes=discord_delay_minutes,
                )
                view = PostButtonView(
                    bot,
                    post["id"],
                    skip_unclaimed_pings=skip_pings,
                    post_to_discord=post_to_discord,
                    discord_delay_minutes=discord_delay_minutes,
                )
                file = get_discord_file(post.get("image_path"))
                send_kwargs = {
                    "embed": build_post_embed(post["content"]),
                    "view": view,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if file is not None:
                    send_kwargs["file"] = file
                msg = await channel.send(content, **send_kwargs)
                await update_post_message_id(post["id"], str(msg.id))

            stage = "sending schedule panel"
            panel = await send_schedule_panel(channel, bot)
            if panel is None:
                log.error(
                    "Schedule refresh failed while %s in channel %s",
                    stage,
                    SCHEDULED_CHANNEL_ID,
                )
                return False
        except Exception:
            log.exception(
                "Schedule refresh failed while %s in channel %s; "
                "a later refresh will rebuild IDs and messages",
                stage,
                SCHEDULED_CHANNEL_ID,
            )
            return False

        log.info(
            "Schedule refresh succeeded: reposted %s posts followed by the panel",
            len(posts),
        )
        return True


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

    def __init__(
        self,
        bot: commands.Bot,
        user_id: int,
        attachment: discord.Attachment | None,
        post_to_x: bool = True,
        post_to_discord: bool = True,
        discord_delay_minutes: int = 0,
    ):
        super().__init__()
        self.bot = bot
        self.user_id = user_id
        self.attachment = attachment
        self.post_to_x = post_to_x
        self.post_to_discord = post_to_discord
        self.discord_delay_minutes = discord_delay_minutes
        self._saved_media_path: str | None = None

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        media_path = None
        if self.attachment:
            media_path, error = await save_validated_media_attachment(
                self.attachment
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            self._saved_media_path = media_path

        view = ScheduleView(
            self.bot,
            self.content_input.value,
            self.user_id,
            media_path,
            post_to_x=self.post_to_x,
            post_to_discord=self.post_to_discord,
            discord_delay_minutes=self.discord_delay_minutes,
        )
        try:
            await interaction.followup.send(
                content=view._status_text(),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            await delete_uncommitted_media(self._saved_media_path)
            self._saved_media_path = None
            view.media_path = None
            raise
        self._saved_media_path = None

    async def on_timeout(self):
        await delete_uncommitted_media(self._saved_media_path)
        self._saved_media_path = None


class SchedulePostModal(discord.ui.Modal, title="Schedule a post"):
    """Collect post content and one optional media file in one modal."""

    def __init__(self, bot: commands.Bot, user_id: int):
        super().__init__()
        self.bot = bot
        self.user_id = user_id
        self._saved_media_path: str | None = None
        self.content_input = discord.ui.TextInput(
            style=discord.TextStyle.long,
            placeholder="Write your post here... line breaks are preserved!",
            required=True,
            max_length=4000,
        )
        self.file_upload = discord.ui.FileUpload(
            custom_id="schedule_post_media",
            required=False,
            min_values=0,
            max_values=1,
        )
        self.discord_delivery_select = discord.ui.Select(
            custom_id="schedule_post_discord_delivery",
            placeholder="Choose Discord live-link timing",
            required=True,
        )
        self.discord_delivery_select.add_option(
            label="Do not post in Discord live-link channels",
            value="off",
            description="X posting, archive, and reminders still work.",
        )
        for label, minutes in DISCORD_DELAY_CHOICES:
            self.discord_delivery_select.add_option(
                label=label,
                value=str(minutes),
                description=(
                    "Send after X confirms publication."
                    if minutes
                    else "Send the X link as soon as it is published."
                ),
                default=(minutes == 0),
            )
        self.add_item(
            discord.ui.Label(text="Post content", component=self.content_input)
        )
        self.add_item(
            discord.ui.Label(
                text="Media (optional)",
                description=(
                    "JPG/PNG/WebP ≤5 MiB; GIF ≤15 MiB. Video: MP4/H.264, "
                    "≤140s. GIF/video limits checked."
                ),
                component=self.file_upload,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Discord live-link delivery",
                description=(
                    "Choose whether and when successful x.com links reach the "
                    "configured live-link channels."
                ),
                component=self.discord_delivery_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        attachment = self.file_upload.values[0] if self.file_upload.values else None
        media_path = None
        if attachment:
            media_path, error = await save_validated_media_attachment(attachment)
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            self._saved_media_path = media_path

        delivery_value = (
            self.discord_delivery_select.values[0]
            if self.discord_delivery_select.values
            else "0"
        )
        post_to_discord = delivery_value != "off"
        discord_delay_minutes = (
            int(delivery_value) if post_to_discord else 0
        )
        view = ScheduleView(
            self.bot,
            self.content_input.value,
            self.user_id,
            media_path,
            post_to_x=True,
            post_to_discord=post_to_discord,
            discord_delay_minutes=discord_delay_minutes,
        )
        try:
            await interaction.followup.send(
                content=view._status_text(),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            await delete_uncommitted_media(self._saved_media_path)
            self._saved_media_path = None
            view.media_path = None
            raise
        self._saved_media_path = None

    async def on_timeout(self):
        await delete_uncommitted_media(self._saved_media_path)
        self._saved_media_path = None


class ScheduleView(_TimePickerView):
    def __init__(
        self,
        bot: commands.Bot,
        content: str,
        user_id: int,
        media_path: str | None = None,
        post_to_x: bool = True,
        post_to_discord: bool = True,
        discord_delay_minutes: int = 0,
    ):
        self.bot = bot
        self.content = content
        self.user_id = user_id
        self.media_path = media_path
        self.post_to_x = post_to_x
        self.post_to_discord = post_to_discord
        self.discord_delay_minutes = discord_delay_minutes
        super().__init__(
            {
                "timezone_select": "tz_select",
                "timezone_button": "tz_button",
                "day": "day_select",
                "hour": "hour_select",
                "minute": "minute_select",
                "exact": "exact_time",
            }
        )

    def _status_text(self) -> str:
        preview = self.content[:100] + ("..." if len(self.content) > 100 else "")
        parts = [f"**Scheduling post:**\n{_quote_content(preview)}\n"]

        premium_status = premium_long_post_status(self.content)
        if premium_status:
            parts.append(f"{premium_status}\n")

        if self.media_path:
            parts.append("Media attached\n")

        if not self.post_to_x:
            parts.append("**Manual X** — bot will not tweet; you post on X when the slot is live.\n")
        elif not self.post_to_discord:
            parts.append(
                "**Discord live links off** — the bot will post to X without "
                "sending the link to configured live-link channels.\n"
            )
        elif self.discord_delay_minutes:
            parts.append(
                "**Discord live-link delay:** "
                f"{_format_discord_delay(self.discord_delay_minutes)} after "
                "X confirms publication.\n"
            )

        parts.append(self._selection_status())

        return "\n".join(parts)

    async def _finalize_time(
        self,
        interaction: discord.Interaction,
        unix_ts: int,
        local_dt: datetime,
    ):
        try:
            await insert_post(
                discord_message_id="pending",
                content=self.content,
                scheduled_at=unix_ts,
                created_by=str(self.user_id),
                image_path=self.media_path,
                post_to_x=self.post_to_x,
                post_to_discord=self.post_to_discord,
                discord_delay_minutes=self.discord_delay_minutes,
            )
        except Exception:
            self.submitted = False
            await delete_uncommitted_media(self.media_path)
            self.media_path = None
            log.exception("Failed to save scheduled post for user %s", self.user_id)
            await interaction.response.edit_message(
                content="The post could not be saved. Please start the scheduling flow again.",
                view=None,
            )
            self.stop()
            return

        # The database now owns the file; timeout cleanup must not remove it.
        self.media_path = None

        await interaction.response.edit_message(
            content=f"Post scheduled for <t:{unix_ts}:F> (<t:{unix_ts}:R>). Updating the schedule...",
            view=None,
        )

        await repost_all_scheduled(self.bot)

        log.info(
            "Post scheduled by %s for %s",
            self.user_id,
            local_dt.isoformat(),
        )
        self.stop()

    async def on_timeout(self):
        if not self.submitted:
            await delete_uncommitted_media(self.media_path)
            self.media_path = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who started this schedule can use it.",
                ephemeral=True,
            )
            return False
        return True


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_done = False
        self._startup_refreshing = False

    async def cog_load(self):
        self.bot.add_view(SchedulePanelView(self.bot))
        for cancellation in await get_open_cancellations_with_archive():
            message_id = cancellation["cancellation_archive_message_id"]
            try:
                numeric_message_id = int(message_id)
            except (TypeError, ValueError):
                log.error(
                    "Cancellation %s has invalid archive message ID %r",
                    cancellation["cancellation_id"],
                    message_id,
                )
                continue
            self.bot.add_view(
                CancelledPostView(self.bot, cancellation["cancellation_id"]),
                message_id=numeric_message_id,
            )

    @commands.Cog.listener()
    async def on_ready(self):
        if self._startup_done or self._startup_refreshing:
            return

        self._startup_refreshing = True
        try:
            if await repost_all_scheduled(self.bot):
                self._startup_done = True
                await archive_pending_cancellations(self.bot)
                return

            log.warning(
                "Initial schedule refresh failed; retrying once in %s seconds",
                STARTUP_REFRESH_RETRY_SECONDS,
            )
            await asyncio.sleep(STARTUP_REFRESH_RETRY_SECONDS)
            if await repost_all_scheduled(self.bot):
                self._startup_done = True
                await archive_pending_cancellations(self.bot)
            else:
                log.error(
                    "Startup schedule refresh failed after one retry; "
                    "the next ready event will retry"
                )
        finally:
            self._startup_refreshing = False

    @app_commands.command(name="schedule", description="Schedule a post for X")
    @app_commands.describe(
        media="Optional JPG/PNG/WebP/GIF or MP4/H.264 video (max 140 seconds)",
        post_to_x="If off, the bot does not tweet or send tweet links; claimers post on X manually. Reminders unchanged.",
        post_to_discord="If off, successful X links are not sent to the configured live-link channels.",
        discord_delay_minutes="Delay live-link posts until this long after X confirms publication.",
    )
    @app_commands.choices(
        discord_delay_minutes=[
            app_commands.Choice(name=label, value=minutes)
            for label, minutes in DISCORD_DELAY_CHOICES
        ],
    )
    async def schedule(
        self,
        interaction: discord.Interaction,
        media: discord.Attachment | None = None,
        post_to_x: bool = True,
        post_to_discord: bool = True,
        discord_delay_minutes: int = 0,
    ):
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        if media:
            try:
                precheck_media_attachment(media)
            except media_utils.MediaValidationError as exc:
                await interaction.response.send_message(
                    str(exc),
                    ephemeral=True,
                )
                return

        modal = PostContentModal(
            self.bot,
            interaction.user.id,
            media,
            post_to_x=post_to_x,
            post_to_discord=post_to_discord,
            discord_delay_minutes=discord_delay_minutes,
        )
        await interaction.response.send_modal(modal)

    @app_commands.command(name="updatemedia", description="Add or replace the media on a scheduled post")
    @app_commands.describe(
        media="One JPG/PNG/WebP/GIF or MP4/H.264 video (max 140 seconds)"
    )
    async def updatemedia(
        self,
        interaction: discord.Interaction,
        media: discord.Attachment,
    ):
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        try:
            precheck_media_attachment(media)
        except media_utils.MediaValidationError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
            )
            return

        posts = await get_all_scheduled_posts()
        if not posts:
            await interaction.response.send_message("No scheduled posts to update.", ephemeral=True)
            return

        # Show a dropdown to pick which post to update
        view = MediaPostPicker(self.bot, posts, media)
        await interaction.response.send_message(
            view.status_text(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.channel_id != SCHEDULED_CHANNEL_ID:
            return

        message_id = str(payload.message_id)
        internal_deletions = _get_internal_deleted_message_ids(self.bot)
        if message_id in internal_deletions:
            internal_deletions.discard(message_id)
            log.debug("Ignored internal deletion of schedule message %s", message_id)
            return

        post = await get_post_by_message_id(message_id)
        if post is None or post.get("status") != "scheduled":
            return

        # Raw deletion events do not identify the actor. A moderator/security
        # bot deleting a card must never be interpreted as user cancellation.
        log.warning(
            "Schedule message %s for post %s was deleted externally; "
            "the database row remains scheduled",
            payload.message_id,
            post["id"],
        )


class MediaPostPicker(discord.ui.View):
    """Paginated dropdown that keeps one pending replacement attachment."""

    def __init__(self, bot: commands.Bot, posts: list[dict], attachment: discord.Attachment):
        super().__init__(timeout=120)
        self.bot = bot
        self.posts = list(posts)
        self.attachment = attachment
        self.handled = False
        self.page_index = 0
        self.page_count = max(
            1,
            (len(self.posts) + MEDIA_PICKER_PAGE_SIZE - 1)
            // MEDIA_PICKER_PAGE_SIZE,
        )
        self._build_page()

    def status_text(self) -> str:
        start = self.page_index * MEDIA_PICKER_PAGE_SIZE + 1
        end = min(
            len(self.posts),
            (self.page_index + 1) * MEDIA_PICKER_PAGE_SIZE,
        )
        return (
            "Which post do you want to attach this media to?\n"
            f"Page **{self.page_index + 1}/{self.page_count}** "
            f"(posts {start}-{end} of {len(self.posts)})."
        )

    def _build_page(self) -> None:
        self.clear_items()
        start = self.page_index * MEDIA_PICKER_PAGE_SIZE
        page_posts = self.posts[start : start + MEDIA_PICKER_PAGE_SIZE]
        select = discord.ui.Select(
            placeholder="Select a post",
            custom_id="media_pick",
            row=0,
        )
        for post in page_posts:
            preview = post["content"][:80].replace("\n", " ").strip()
            select.add_option(
                label=preview or "(empty post)",
                value=str(post["id"]),
                description=f"Scheduled for <t:{post['scheduled_at']}:f>",
            )
        select.callback = self._on_select
        self.add_item(select)

        if self.page_count > 1:
            previous = discord.ui.Button(
                label="Prev",
                style=discord.ButtonStyle.secondary,
                custom_id="media_pick_prev",
                row=1,
                disabled=self.page_index == 0,
            )
            previous.callback = self._on_previous
            self.add_item(previous)

            next_button = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                custom_id="media_pick_next",
                row=1,
                disabled=self.page_index == self.page_count - 1,
            )
            next_button.callback = self._on_next
            self.add_item(next_button)

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self._build_page()
        await interaction.response.edit_message(
            content=self.status_text(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_previous(self, interaction: discord.Interaction):
        if self.page_index > 0:
            self.page_index -= 1
        await self._show_page(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        if self.page_index < self.page_count - 1:
            self.page_index += 1
        await self._show_page(interaction)

    async def _on_select(self, interaction: discord.Interaction):
        if self.handled:
            return
        self.handled = True
        await interaction.response.defer()

        post_id = int(interaction.data["values"][0])
        post = await get_post_by_id(post_id)
        if not post:
            await interaction.edit_original_response(
                content="That post no longer exists.", view=None
            )
            return

        media_path, error = await save_validated_media_attachment(self.attachment)
        if error:
            await interaction.edit_original_response(content=error, view=None)
            return

        try:
            await update_post_image(post_id, media_path)
        except Exception:
            # The old DB reference remains intact; remove only the unused new file.
            if media_path:
                try:
                    Path(media_path).unlink(missing_ok=True)
                except OSError:
                    log.exception(
                        "Failed to remove unused replacement media %s",
                        media_path,
                    )
            raise

        # Delete the old file only after the new DB reference is committed.
        if post.get("image_path"):
            p = Path(post["image_path"])
            try:
                p.unlink(missing_ok=True)
            except OSError:
                log.exception(
                    "Post %s now uses %s but old media could not be deleted: %s",
                    post_id,
                    media_path,
                    p,
                )
        await interaction.edit_original_response(
            content="Media updated. Refreshing schedule...", view=None
        )
        await repost_all_scheduled(self.bot)
        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
