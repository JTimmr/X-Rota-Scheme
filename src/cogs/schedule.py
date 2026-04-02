import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="schedule", description="Schedule a post for X")
    @app_commands.describe(
        content="The post/tweet text",
        time="Scheduled datetime, e.g. 2026-04-03 14:00",
        timezone="IANA timezone, e.g. Europe/London (defaults to UTC)",
    )
    async def schedule(self, interaction: discord.Interaction, content: str, time: str, timezone: str = "Europe/London"):
        if interaction.channel_id != SCHEDULED_CHANNEL_ID:
            await interaction.response.send_message(
                f"This command can only be used in <#{SCHEDULED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, KeyError):
            await interaction.response.send_message(
                f"Unknown timezone `{timezone}`. Use an IANA timezone like `Europe/London` or `America/New_York`.",
                ephemeral=True,
            )
            return

        try:
            dt = datetime.strptime(time, "%Y-%m-%d %H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Invalid time format. Use `YYYY-MM-DD HH:MM`, e.g. `2026-04-03 14:00`.",
                ephemeral=True,
            )
            return

        dt = dt.replace(tzinfo=tz)
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
            f"Post scheduled for <t:{unix_ts}:F>. The message has been posted above — others can react to claim it.",
            ephemeral=True,
        )
        log.info(f"Post scheduled by {interaction.user} for {dt.isoformat()}, message {msg.id}")

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
