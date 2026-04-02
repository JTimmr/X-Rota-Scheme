import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks

from config import ARCHIVE_CHANNEL_ID, REMINDERS_CHANNEL_ID, SCHEDULED_CHANNEL_ID
from database import (
    get_active_user_ids,
    get_available_active_user_ids,
    get_claimers_for_post,
    get_due_posts,
    get_posts_without_claims_in_range,
    get_scheduled_posts_in_range,
    mark_post_live,
    update_post_message_id,
)


def _get_discord_file(image_path: str | None) -> discord.File | None:
    if not image_path:
        return None
    p = Path(image_path)
    if p.exists():
        return discord.File(p, filename=p.name)
    return None


def _quote_content(content: str) -> str:
    return "\n".join(f"> {line}" for line in content.split("\n"))

log = logging.getLogger("rota-bot.scheduler")

SECONDS_15_MIN = 15 * 60
SECONDS_4_HOURS = 4 * 60 * 60
SECONDS_24_HOURS = 24 * 60 * 60


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_gap_check_date: str | None = None
        self._reminded_pre_post: set[int] = set()
        self._reminded_unassigned: set[int] = set()

    async def cog_load(self):
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=60)
    async def tick(self):
        try:
            await self._check_go_live()
            await self._check_pre_post_reminders()
            await self._check_unassigned_posts()
            await self._check_daily_gap()
        except Exception:
            log.exception("Error in scheduler tick")

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    async def _check_go_live(self):
        due_posts = await get_due_posts()
        if not due_posts:
            return

        schedule_channel = self.bot.get_channel(SCHEDULED_CHANNEL_ID)
        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)

        for post in due_posts:
            await mark_post_live(post["id"])
            claimers = await get_claimers_for_post(post["id"])

            # Invalidate message ID so the delete event doesn't remove the post
            await update_post_message_id(post["id"], f"live_{post['id']}")

            if schedule_channel:
                try:
                    msg = await schedule_channel.fetch_message(int(post["discord_message_id"]))
                    await msg.delete()
                except (discord.NotFound, ValueError):
                    pass
                except discord.Forbidden:
                    log.warning(f"No permission to delete message {post['discord_message_id']}")

            if archive_channel:
                live_ts = int(time.time())
                archive_text = (
                    f"**Post went live** <t:{live_ts}:F>\n"
                    f"\n"
                    f"{_quote_content(post['content'])}\n"
                    f"\n"
                    f"Originally scheduled for: <t:{post['scheduled_at']}:F>\n"
                    f"Scheduled by: <@{post['created_by']}>"
                )
                file = _get_discord_file(post.get("image_path"))
                await archive_channel.send(archive_text, file=file)

            if reminders_channel and claimers:
                mentions = " ".join(f"<@{uid}>" for uid in claimers)
                await reminders_channel.send(
                    f"Your post just went live! Time to share the link and engage with replies.\n\n"
                    f"{_quote_content(post['content'])}\n\n"
                    f"{mentions}"
                )
            elif reminders_channel:
                available = await get_available_active_user_ids(post["id"])
                if available:
                    mentions = " ".join(f"<@{uid}>" for uid in available)
                    await reminders_channel.send(
                        f"A post just went live but **nobody claimed it**! Someone needs to share the link and engage.\n\n"
                        f"{_quote_content(post['content'])}\n\n"
                        f"{mentions}"
                    )

            self._reminded_pre_post.discard(post["id"])
            self._reminded_unassigned.discard(post["id"])
            log.info(f"Post {post['id']} went live and moved to archive")

    async def _check_pre_post_reminders(self):
        now = int(time.time())
        upcoming = await get_scheduled_posts_in_range(now, now + SECONDS_15_MIN)

        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)
        if not reminders_channel:
            return

        for post in upcoming:
            if post["id"] in self._reminded_pre_post:
                continue

            claimers = await get_claimers_for_post(post["id"])
            if claimers:
                mentions = " ".join(f"<@{uid}>" for uid in claimers)
                await reminders_channel.send(
                    f"Your post goes live <t:{post['scheduled_at']}:R> — get ready to engage!\n\n"
                    f"{_quote_content(post['content'])}\n\n"
                    f"{mentions}"
                )
            else:
                available = await get_available_active_user_ids(post["id"])
                if available:
                    mentions = " ".join(f"<@{uid}>" for uid in available)
                    await reminders_channel.send(
                        f"A post goes live <t:{post['scheduled_at']}:R> and **still nobody has claimed it**!\n\n"
                        f"{_quote_content(post['content'])}\n\n"
                        f"Claim it in <#{SCHEDULED_CHANNEL_ID}>.\n\n"
                        f"{mentions}"
                    )
            self._reminded_pre_post.add(post["id"])

    async def _check_unassigned_posts(self):
        now = int(time.time())
        unassigned = await get_posts_without_claims_in_range(now, now + SECONDS_4_HOURS)

        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)
        if not reminders_channel:
            return

        for post in unassigned:
            if post["id"] in self._reminded_unassigned:
                continue

            available = await get_available_active_user_ids(post["id"])
            if available:
                mentions = " ".join(f"<@{uid}>" for uid in available)
                await reminders_channel.send(
                    f"This post goes live <t:{post['scheduled_at']}:R> and **nobody has claimed it**!\n\n"
                    f"{_quote_content(post['content'])}\n\n"
                    f"Claim it in <#{SCHEDULED_CHANNEL_ID}>.\n\n"
                    f"{mentions}"
                )
            self._reminded_unassigned.add(post["id"])

    async def _check_daily_gap(self):
        utc_now = datetime.now(timezone.utc)
        today_key = utc_now.strftime("%Y-%m-%d")

        if utc_now.hour != 20 or self._last_gap_check_date == today_key:
            return

        self._last_gap_check_date = today_key

        now = int(time.time())
        upcoming = await get_scheduled_posts_in_range(now, now + SECONDS_24_HOURS)

        if len(upcoming) >= 2:
            return

        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)
        if not reminders_channel:
            return

        active_users = await get_active_user_ids()
        if not active_users:
            return

        mentions = " ".join(f"<@{uid}>" for uid in active_users)
        count = len(upcoming)
        if count == 0:
            await reminders_channel.send(
                f"**No posts** are scheduled for the next 24 hours! We need at least 2.\n\n"
                f"Use `/schedule` in <#{SCHEDULED_CHANNEL_ID}> to add posts.\n\n"
                f"{mentions}"
            )
        else:
            await reminders_channel.send(
                f"Only **{count} post** is scheduled for the next 24 hours. We need at least 2.\n\n"
                f"Use `/schedule` in <#{SCHEDULED_CHANNEL_ID}> to add more.\n\n"
                f"{mentions}"
            )

        log.info(f"Gap detection alert: {count} posts in next 24h")


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
