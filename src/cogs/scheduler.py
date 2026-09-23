import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks

from cogs.schedule import (
    DISCORD_CONTENT_LIMIT,
    build_post_embed,
    get_schedule_refresh_lock,
)
from config import (
    ARCHIVE_CHANNEL_ID,
    GUILD_ID,
    REMINDERS_CHANNEL_ID,
    ROTA_ALERT_ROLE_ID,
    SCHEDULED_CHANNEL_ID,
    X_ENABLED,
    X_LIVE_POST_LINK_CHANNEL_IDS,
)
from database import (
    get_active_user_ids,
    get_available_active_user_ids,
    get_claimers_for_post,
    get_due_discord_deliveries,
    get_due_posts,
    get_post_by_id,
    get_posts_without_claims_in_range,
    get_scheduled_posts_in_range,
    record_discord_delivery_failure,
    record_discord_delivery_success,
    record_post_alert_delivery,
    record_x_post_success,
    transition_due_post_to_live,
    was_post_alert_delivered,
)
from x_client import (
    X_POST_FAILED,
    X_POST_SUCCESS,
    X_POST_UNKNOWN,
    XPostResult,
    post_tweet_result,
)


def _get_discord_file(media_path: str | None) -> discord.File | None:
    if not media_path:
        return None
    p = Path(media_path)
    if p.exists():
        return discord.File(p, filename=p.name)
    return None


def _user_alert_target(user_ids: list[str]) -> tuple[str, discord.AllowedMentions]:
    mentions = " ".join(f"<@{uid}>" for uid in user_ids)
    allowed_mentions = discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=int(uid)) for uid in user_ids],
        roles=False,
        replied_user=False,
    )
    return mentions, allowed_mentions


MAX_ALLOWED_USER_MENTIONS = 100


def _user_alert_batches(
    body: str,
    user_ids: list[str],
) -> list[tuple[str, discord.AllowedMentions]]:
    """Batch user mentions within Discord content and allowed-mention limits."""
    if len(body) > DISCORD_CONTENT_LIMIT:
        raise ValueError("notification body exceeds Discord's content limit")

    ordered = list(dict.fromkeys(str(user_id) for user_id in user_ids))
    batches: list[list[str]] = []
    current: list[str] = []
    for user_id in ordered:
        candidate = current + [user_id]
        mention_text = " ".join(f"<@{uid}>" for uid in candidate)
        content_length = len(body) + (2 if body else 0) + len(mention_text)
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

    results = []
    for batch in batches:
        mentions, allowed_mentions = _user_alert_target(batch)
        content = f"{body}\n\n{mentions}" if body else mentions
        results.append((content, allowed_mentions))
    return results


log = logging.getLogger("rota-bot.scheduler")

SECONDS_15_MIN = 15 * 60
SECONDS_4_HOURS = 4 * 60 * 60
SECONDS_24_HOURS = 24 * 60 * 60
ALERT_KIND_FOUR_HOUR = "unclaimed_4h"
ALERT_KIND_FIFTEEN_MINUTE = "pre_live_15m"


def _post_to_x(post: dict) -> bool:
    """If False, bot does not tweet or broadcast tweet URLs; claimer handles X manually."""
    return bool(post.get("post_to_x", 1))


def _post_to_discord(post: dict) -> bool:
    """Whether successful X links should reach configured live-link channels."""
    return bool(post.get("post_to_discord", 1))


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_gap_check_date: str | None = None
        self._alert_role_warning_logged = False

    async def cog_load(self):
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=60)
    async def tick(self):
        try:
            await self._check_go_live()
            reminder_now = int(time.time())
            await self._check_discord_deliveries(reminder_now)
            await self._check_pre_post_reminders(reminder_now)
            await self._check_unassigned_posts(reminder_now)
            await self._check_daily_gap()
        except Exception:
            log.exception("Error in scheduler tick")

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    def _get_alert_role(self) -> discord.Role | None:
        if ROTA_ALERT_ROLE_ID is None:
            return None

        guild = self.bot.get_guild(GUILD_ID)
        role = guild.get_role(ROTA_ALERT_ROLE_ID) if guild else None
        if role:
            self._alert_role_warning_logged = False
            return role

        if not self._alert_role_warning_logged:
            log.warning(
                "ROTA_ALERT_ROLE_ID=%s does not resolve to a role in configured guild %s; "
                "falling back to active-user alerts",
                ROTA_ALERT_ROLE_ID,
                GUILD_ID,
            )
            self._alert_role_warning_logged = True
        return None

    async def _team_alert_target(
        self, post_id: int | None = None
    ) -> tuple[str, discord.AllowedMentions] | None:
        role = self._get_alert_role()
        if role:
            return role.mention, discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[role],
                replied_user=False,
            )

        user_ids = (
            await get_available_active_user_ids(post_id)
            if post_id is not None
            else await get_active_user_ids()
        )
        return _user_alert_target(user_ids) if user_ids else None

    async def _send_user_notifications(
        self,
        channel,
        body: str,
        user_ids: list[str],
        *,
        embed: discord.Embed | None = None,
    ) -> bool:
        batches = _user_alert_batches(body, user_ids)
        if not batches:
            return False
        for content, allowed_mentions in batches:
            send_kwargs = {"allowed_mentions": allowed_mentions}
            if embed is not None:
                send_kwargs["embed"] = embed
            await channel.send(content, **send_kwargs)
        return True

    async def _send_team_notification(
        self,
        channel,
        body: str,
        post_id: int | None = None,
        *,
        embed: discord.Embed | None = None,
    ) -> bool:
        role = self._get_alert_role()
        if role:
            content = f"{body}\n\n{role.mention}"
            if len(content) > DISCORD_CONTENT_LIMIT:
                raise ValueError("role notification exceeds Discord's content limit")
            send_kwargs = {
                "allowed_mentions": discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[role],
                    replied_user=False,
                )
            }
            if embed is not None:
                send_kwargs["embed"] = embed
            await channel.send(content, **send_kwargs)
            return True

        user_ids = (
            await get_available_active_user_ids(post_id)
            if post_id is not None
            else await get_active_user_ids()
        )
        return await self._send_user_notifications(
            channel,
            body,
            user_ids,
            embed=embed,
        )

    async def _take_due_post(self, post_id: int, schedule_channel) -> dict | None:
        """Transition and remove the current schedule message under refresh lock."""
        async with get_schedule_refresh_lock(self.bot):
            post = await get_post_by_id(post_id)
            now = int(time.time())
            if (
                not post
                or post.get("status") != "scheduled"
                or post["scheduled_at"] > now
            ):
                return None

            message_id = str(post["discord_message_id"])
            transitioned = await transition_due_post_to_live(
                post_id,
                message_id,
                f"live_{post_id}",
                now,
            )
            if not transitioned:
                return None

            if schedule_channel:
                try:
                    message = await schedule_channel.fetch_message(int(message_id))
                    await message.delete()
                except (discord.NotFound, ValueError):
                    pass
                except discord.Forbidden:
                    log.warning("No permission to delete message %s", message_id)
                except discord.HTTPException:
                    log.exception("Failed to delete live schedule message %s", message_id)
            return post

    @staticmethod
    def _x_outcome_body(result: XPostResult) -> str:
        if result.status == X_POST_SUCCESS:
            return (
                "Your post just went live! Time to share the link and engage "
                f"with replies.\n\n{result.url}"
            )
        if result.status == X_POST_UNKNOWN:
            return (
                "**X posting outcome is unknown.** Check the X account before "
                "retrying; do not immediately republish because X may have "
                "accepted the request."
            )
        return (
            "**X auto-post failed.** No X post was created. The bot will not "
            "retry automatically; publish it manually."
        )

    async def _check_go_live(self):
        due_posts = await get_due_posts()
        if not due_posts:
            return

        schedule_channel = self.bot.get_channel(SCHEDULED_CHANNEL_ID)
        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)

        for candidate in due_posts:
            post = await self._take_due_post(candidate["id"], schedule_channel)
            if post is None:
                continue
            claimers = await get_claimers_for_post(post["id"])

            x_result: XPostResult | None = None
            if _post_to_x(post):
                if X_ENABLED:
                    loop = asyncio.get_running_loop()
                    x_result = await loop.run_in_executor(
                        None,
                        post_tweet_result,
                        post["content"],
                        post.get("image_path"),
                    )
                else:
                    x_result = XPostResult(
                        X_POST_FAILED,
                        detail="X credentials are disabled",
                    )

                if x_result.status == X_POST_SUCCESS and x_result.url:
                    published_at = int(time.time())
                    discord_channel_ids = (
                        X_LIVE_POST_LINK_CHANNEL_IDS
                        if _post_to_discord(post)
                        else []
                    )
                    await record_x_post_success(
                        post["id"],
                        x_result.url,
                        published_at,
                        discord_channel_ids,
                        int(post.get("discord_delay_minutes", 0)),
                    )
                elif x_result.status == X_POST_UNKNOWN:
                    log.error(
                        "X outcome is unknown for post %s; check X before retrying",
                        post["id"],
                    )
                else:
                    log.error(
                        "X auto-post failed for post %s before tweet creation: %s",
                        post["id"],
                        x_result.detail or "unspecified confirmed failure",
                    )

            if archive_channel:
                live_ts = int(time.time())
                if not _post_to_x(post):
                    archive_heading = (
                        f"**Manual X slot went live** <t:{live_ts}:F>\n"
                        "The bot did not post this to X."
                    )
                elif x_result and x_result.status == X_POST_SUCCESS:
                    archive_heading = f"**Post went live on X** <t:{live_ts}:F>"
                elif x_result and x_result.status == X_POST_UNKNOWN:
                    archive_heading = (
                        f"**X posting outcome unknown** <t:{live_ts}:F>\n"
                        "Check X before retrying; the request may have succeeded."
                    )
                else:
                    archive_heading = (
                        f"**X auto-post failed** <t:{live_ts}:F>\n"
                        "No X post was created; publish this manually."
                    )
                archive_text = (
                    f"{archive_heading}\n\n"
                    f"Originally scheduled for: <t:{post['scheduled_at']}:F>\n"
                    f"Scheduled by: <@{post['created_by']}>"
                )
                if x_result and x_result.status == X_POST_SUCCESS and x_result.url:
                    archive_text += f"\n\n{x_result.url}"
                file = _get_discord_file(post.get("image_path"))
                send_kwargs = {
                    "embed": build_post_embed(post["content"]),
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if file is not None:
                    send_kwargs["file"] = file
                await archive_channel.send(archive_text, **send_kwargs)

            notification_sent = False
            if reminders_channel and claimers:
                if _post_to_x(post):
                    reminder_body = self._x_outcome_body(x_result)
                else:
                    reminder_body = (
                        "**Your manual-X slot is live.** Open X, publish it "
                        "yourself, then share and engage."
                    )
                notification_sent = await self._send_user_notifications(
                    reminders_channel,
                    reminder_body,
                    claimers,
                    embed=build_post_embed(post["content"]),
                )
            elif reminders_channel and not post.get("skip_unclaimed_pings"):
                if _post_to_x(post):
                    reminder_body = self._x_outcome_body(x_result)
                else:
                    reminder_body = (
                        "A **manual-X slot is live but unclaimed**. Someone "
                        "needs to publish it on X."
                    )
                notification_sent = await self._send_team_notification(
                    reminders_channel,
                    reminder_body,
                    post["id"],
                    embed=build_post_embed(post["content"]),
                )

            if (
                reminders_channel
                and _post_to_x(post)
                and x_result
                and x_result.status != X_POST_SUCCESS
                and not notification_sent
            ):
                await reminders_channel.send(
                    self._x_outcome_body(x_result),
                    embed=build_post_embed(post["content"]),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            log.info("Post %s completed go-live processing", post["id"])

    async def _check_discord_deliveries(self, now: int | None = None):
        if now is None:
            now = int(time.time())
        deliveries = await get_due_discord_deliveries(now)
        for delivery in deliveries:
            channel_id = delivery["channel_id"]
            try:
                numeric_channel_id = int(channel_id)
            except (TypeError, ValueError):
                log.error(
                    "Invalid Discord live-link channel ID %r for post %s",
                    channel_id,
                    delivery["post_id"],
                )
                await record_discord_delivery_failure(
                    delivery["post_id"],
                    str(channel_id),
                    now,
                )
                continue

            channel = self.bot.get_channel(numeric_channel_id)
            if not channel:
                log.warning("X live link channel %s not found", numeric_channel_id)
                await record_discord_delivery_failure(
                    delivery["post_id"],
                    str(channel_id),
                    now,
                )
                continue

            try:
                message = await channel.send(
                    delivery["tweet_url"],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                log.warning(
                    "No permission to send X link in channel %s",
                    numeric_channel_id,
                )
                await record_discord_delivery_failure(
                    delivery["post_id"],
                    str(channel_id),
                    now,
                )
                continue
            except discord.HTTPException:
                log.exception(
                    "Failed to send X link to channel %s",
                    numeric_channel_id,
                )
                await record_discord_delivery_failure(
                    delivery["post_id"],
                    str(channel_id),
                    now,
                )
                continue

            await record_discord_delivery_success(
                delivery["post_id"],
                str(channel_id),
                now,
                str(message.id) if getattr(message, "id", None) is not None else None,
            )
            log.info(
                "Delivered X link for post %s to Discord channel %s",
                delivery["post_id"],
                numeric_channel_id,
            )

    async def _check_pre_post_reminders(self, now: int | None = None):
        if now is None:
            now = int(time.time())
        upcoming = await get_scheduled_posts_in_range(now, now + SECONDS_15_MIN)

        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)
        if not reminders_channel:
            return

        for post in upcoming:
            if await was_post_alert_delivered(
                post["id"],
                ALERT_KIND_FIFTEEN_MINUTE,
            ):
                continue

            claimers = await get_claimers_for_post(post["id"])
            notified = False
            if claimers:
                if _post_to_x(post):
                    reminder_body = (
                        f"Your post goes live <t:{post['scheduled_at']}:R> — get ready to engage!\n\n"
                    )
                else:
                    reminder_body = (
                        f"Your slot goes live <t:{post['scheduled_at']}:R> — **you** post it on X "
                        f"(the bot will not). Get ready to publish and engage.\n\n"
                    )
                notified = await self._send_user_notifications(
                    reminders_channel,
                    reminder_body.rstrip(),
                    claimers,
                    embed=build_post_embed(post["content"]),
                )
            elif not post.get("skip_unclaimed_pings"):
                msg_link = f"https://discord.com/channels/{GUILD_ID}/{SCHEDULED_CHANNEL_ID}/{post['discord_message_id']}"
                reminder_body = (
                    f"A post goes live <t:{post['scheduled_at']}:R> and "
                    "**still nobody has claimed it**!\n\n"
                    f"[Jump to post]({msg_link}) to claim it."
                )
                notified = await self._send_team_notification(
                    reminders_channel,
                    reminder_body,
                    post["id"],
                    embed=build_post_embed(post["content"]),
                )
            if notified:
                await record_post_alert_delivery(
                    post["id"],
                    ALERT_KIND_FIFTEEN_MINUTE,
                )

    async def _check_unassigned_posts(self, now: int | None = None):
        if now is None:
            now = int(time.time())
        unassigned = await get_posts_without_claims_in_range(
            now + SECONDS_15_MIN + 1,
            now + SECONDS_4_HOURS,
        )

        reminders_channel = self.bot.get_channel(REMINDERS_CHANNEL_ID)
        if not reminders_channel:
            return

        for post in unassigned:
            if await was_post_alert_delivered(
                post["id"],
                ALERT_KIND_FOUR_HOUR,
            ):
                continue

            if post.get("skip_unclaimed_pings"):
                continue

            msg_link = f"https://discord.com/channels/{GUILD_ID}/{SCHEDULED_CHANNEL_ID}/{post['discord_message_id']}"
            reminder_body = (
                f"This post goes live <t:{post['scheduled_at']}:R> and "
                "**nobody has claimed it**!\n\n"
                f"[Jump to post]({msg_link}) to claim it."
            )
            notified = await self._send_team_notification(
                reminders_channel,
                reminder_body,
                post["id"],
                embed=build_post_embed(post["content"]),
            )
            if notified:
                await record_post_alert_delivery(
                    post["id"],
                    ALERT_KIND_FOUR_HOUR,
                )

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

        count = len(upcoming)
        if count == 0:
            body = (
                "**No posts** are scheduled for the next 24 hours! We need at "
                f"least 2.\n\nUse `/schedule` in <#{SCHEDULED_CHANNEL_ID}> "
                "to add posts."
            )
        else:
            body = (
                f"Only **{count} post** is scheduled for the next 24 hours. "
                f"We need at least 2.\n\nUse `/schedule` in "
                f"<#{SCHEDULED_CHANNEL_ID}> to add more."
            )
        if not await self._send_team_notification(reminders_channel, body):
            return

        log.info(f"Gap detection alert: {count} posts in next 24h")


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
