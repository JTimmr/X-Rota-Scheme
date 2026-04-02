import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser
import discord
from discord import app_commands
from discord.ext import commands

from config import SCHEDULED_CHANNEL_ID
from database import (
    add_reaction,
    delete_post_by_message_id,
    get_post_by_message_id,
    get_reactions_for_post,
    insert_post,
    remove_reaction,
)

log = logging.getLogger("rota-bot.schedule")

DEFAULT_TZ = "Europe/London"


def format_scheduled_message(content: str, scheduled_at: int, created_by_id: str, claimers: list[str] | None = None) -> str:
    lines = [
        f"**Scheduled Post**",
        f"",
        f"> {content}",
        f"",
        f"Scheduled for: <t:{scheduled_at}:F> (<t:{scheduled_at}:R>)",
        f"Scheduled by: <@{created_by_id}>",
        f"",
    ]
    if claimers:
        mentions = ", ".join(f"<@{uid}>" for uid in claimers)
        lines.append(f"Claimed by: {mentions}")
    else:
        lines.append("**Unclaimed** — react to this message to claim it!")

    return "\n".join(lines)


def _generate_time_suggestions(current_input: str) -> list[app_commands.Choice[str]]:
    """Generate autocomplete suggestions for the time parameter."""
    now = datetime.now(tz=ZoneInfo(DEFAULT_TZ))

    suggestions: list[tuple[str, str]] = []

    hours = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]

    today = now.date()
    for day_offset in range(7):
        day = today + timedelta(days=day_offset)
        if day_offset == 0:
            day_label = "today"
        elif day_offset == 1:
            day_label = "tomorrow"
        else:
            day_label = day.strftime("%A %d %b")

        for hour in hours:
            candidate = datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZoneInfo(DEFAULT_TZ))
            if candidate <= now:
                continue
            label = f"{day_label} {hour:02d}:00"
            suggestions.append((label, label))

    lower = current_input.lower()
    filtered = [(label, value) for label, value in suggestions if lower in label.lower()]

    return [app_commands.Choice(name=label, value=value) for label, value in filtered[:25]]


def parse_time_input(time_str: str, timezone: str) -> datetime | None:
    """Parse a time string using dateparser for natural language support."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz=tz)

    settings = {
        "TIMEZONE": timezone,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now.replace(tzinfo=None),
    }

    parsed = dateparser.parse(time_str, settings=settings)
    if parsed is None:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)

    return parsed


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="schedule", description="Schedule a post for X")
    @app_commands.describe(
        content="The post/tweet text",
        time="When to post, e.g. 'tomorrow 3pm', 'friday 14:00', '2026-04-05 10:00'",
        timezone="IANA timezone, e.g. Europe/London (defaults to Europe/London)",
    )
    async def schedule(self, interaction: discord.Interaction, content: str, time: str, timezone: str = DEFAULT_TZ):
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, KeyError):
            await interaction.response.send_message(
                f"Unknown timezone `{timezone}`. Use an IANA timezone like `Europe/London` or `America/New_York`.",
                ephemeral=True,
            )
            return

        dt = parse_time_input(time, timezone)
        if dt is None:
            await interaction.response.send_message(
                "Couldn't understand that time. Try something like:\n"
                "• `tomorrow 3pm`\n"
                "• `friday 14:00`\n"
                "• `next monday 9:00`\n"
                "• `in 2 hours`\n"
                "• `2026-04-05 10:00`",
                ephemeral=True,
            )
            return

        unix_ts = int(dt.timestamp())

        now_unix = int(datetime.now(tz=ZoneInfo("UTC")).timestamp())
        if unix_ts <= now_unix:
            await interaction.response.send_message(
                "That time is in the past. Please schedule for a future time.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(SCHEDULED_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Could not find the scheduled posts channel.", ephemeral=True)
            return

        msg_content = format_scheduled_message(content, unix_ts, str(interaction.user.id))
        msg = await channel.send(msg_content)

        await insert_post(
            discord_message_id=str(msg.id),
            content=content,
            scheduled_at=unix_ts,
            created_by=str(interaction.user.id),
        )

        await interaction.response.send_message(
            f"Post scheduled for <t:{unix_ts}:F> (<t:{unix_ts}:R>). The message has been posted above — others can react to claim it.",
            ephemeral=True,
        )
        log.info(f"Post scheduled by {interaction.user} for {dt.isoformat()}, message {msg.id}")

    @schedule.autocomplete("time")
    async def time_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _generate_time_suggestions(current)

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
