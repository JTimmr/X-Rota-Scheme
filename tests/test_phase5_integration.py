import asyncio
import io
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch
from zoneinfo import ZoneInfo

import aiosqlite
from PIL import Image

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    __import__("tweepy")
except ModuleNotFoundError:
    tweepy_stub = types.ModuleType("tweepy")
    tweepy_stub.API = type("API", (), {})
    tweepy_stub.Client = type("Client", (), {})
    tweepy_stub.OAuth1UserHandler = type("OAuth1UserHandler", (), {})
    sys.modules["tweepy"] = tweepy_stub

import database
from cogs import schedule, scheduler


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


class LockTestMessage:
    def __init__(self, channel, message_id, author, content="", **send_kwargs):
        self.channel = channel
        self.id = message_id
        self.author = author
        self.content = content
        self.send_kwargs = send_kwargs

    async def delete(self):
        if self in self.channel.messages:
            self.channel.messages.remove(self)


class LockTestChannel:
    def __init__(self, bot_user):
        self.id = schedule.SCHEDULED_CHANNEL_ID
        self.bot_user = bot_user
        self.messages = [
            LockTestMessage(self, 10, bot_user, "old scheduled message")
        ]
        self.next_id = 100

    def history(self, *, limit):
        async def iterate():
            for message in list(self.messages)[:limit]:
                yield message

        return iterate()

    async def send(self, content, **kwargs):
        message = LockTestMessage(
            self,
            self.next_id,
            self.bot_user,
            content,
            **kwargs,
        )
        self.next_id += 1
        self.messages.append(message)
        return message

    async def fetch_message(self, message_id):
        for message in self.messages:
            if message.id == message_id:
                return message
        raise AssertionError(f"message {message_id} was not found")


class PanelDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_upload_and_exact_time_create_complete_scheduled_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                attachment = SimpleNamespace(
                    filename="campaign.png",
                    content_type="image/png",
                    size=len(png_bytes()),
                    read=AsyncMock(return_value=png_bytes()),
                )
                modal = schedule.SchedulePostModal(Mock(), user_id=123)
                modal.content_input._value = "Panel integration post"
                modal.file_upload._values = [attachment]
                compose_interaction = SimpleNamespace(
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )

                with (
                    patch.object(schedule, "IMAGES_DIR", database.IMAGES_DIR),
                    patch.object(
                        schedule,
                        "repost_all_scheduled",
                        new=AsyncMock(return_value=True),
                    ) as refresh,
                ):
                    await modal.on_submit(compose_interaction)
                    picker = compose_interaction.followup.send.await_args.kwargs[
                        "view"
                    ]
                    self.assertIsInstance(picker, schedule.ScheduleView)

                    target = datetime.now(
                        tz=ZoneInfo(schedule.DEFAULT_TZ)
                    ) + timedelta(days=2)
                    exact = schedule.ExactDateTimeModal(picker)
                    exact.date_input._value = target.date().isoformat()
                    exact.time_input._value = "12:36"
                    exact_interaction = SimpleNamespace(
                        response=SimpleNamespace(edit_message=AsyncMock())
                    )
                    await exact.on_submit(exact_interaction)

                rows = await database.get_all_scheduled_posts()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["content"], "Panel integration post")
                self.assertEqual(row["created_by"], "123")
                self.assertEqual(row["post_to_x"], 1)
                self.assertEqual(row["post_to_discord"], 1)
                self.assertEqual(row["discord_delay_minutes"], 0)
                self.assertEqual(row["skip_unclaimed_pings"], 1)
                self.assertEqual(Path(row["image_path"]).suffix, ".png")
                self.assertTrue(Path(row["image_path"]).is_file())
                self.assertEqual(
                    datetime.fromtimestamp(
                        row["scheduled_at"],
                        tz=ZoneInfo(schedule.DEFAULT_TZ),
                    ).strftime("%H:%M"),
                    "12:36",
                )
                refresh.assert_awaited_once()
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir

    async def test_rejected_panel_media_leaves_no_file_or_database_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                attachment = SimpleNamespace(
                    filename="spoofed.png",
                    content_type="image/png",
                    size=13,
                    read=AsyncMock(return_value=b"MZ executable"),
                )
                modal = schedule.SchedulePostModal(Mock(), user_id=123)
                modal.content_input._value = "Must not be inserted"
                modal.file_upload._values = [attachment]
                interaction = SimpleNamespace(
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )

                with patch.object(
                    schedule,
                    "IMAGES_DIR",
                    database.IMAGES_DIR,
                ):
                    await modal.on_submit(interaction)

                self.assertEqual(await database.get_all_scheduled_posts(), [])
                self.assertEqual(list(database.IMAGES_DIR.iterdir()), [])
                self.assertIn(
                    "do not match",
                    interaction.followup.send.await_args.args[0],
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir


class ClaimNotificationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_hour_and_fifteen_minute_windows_do_not_overlap(self):
        cases = (
            ("ten minutes", 10 * 60, scheduler.ALERT_KIND_FIFTEEN_MINUTE),
            (
                "exactly fifteen minutes",
                scheduler.SECONDS_15_MIN,
                scheduler.ALERT_KIND_FIFTEEN_MINUTE,
            ),
            (
                "just over fifteen minutes",
                scheduler.SECONDS_15_MIN + 1,
                scheduler.ALERT_KIND_FOUR_HOUR,
            ),
        )
        for label, offset, expected_kind in cases:
            with self.subTest(boundary=label), tempfile.TemporaryDirectory() as temp_dir:
                original_db_path = database.DB_PATH
                original_images_dir = database.IMAGES_DIR
                database.DB_PATH = Path(temp_dir) / "rota.db"
                database.IMAGES_DIR = Path(temp_dir) / "images"
                try:
                    await database.init_db()
                    now = int(time.time())
                    post_id = await database.insert_post(
                        discord_message_id="55",
                        content="Newly required post",
                        scheduled_at=now + offset,
                        created_by="42",
                    )
                    # New posts start optional; requiring a claimer must still
                    # produce only the alert for the applicable cadence window.
                    await database.update_post_skip_unclaimed_pings(
                        post_id,
                        False,
                    )
                    reminders = SimpleNamespace(send=AsyncMock())
                    bot = Mock()
                    bot.get_channel.return_value = reminders
                    cog = scheduler.SchedulerCog(bot)

                    with patch.object(
                        scheduler,
                        "ROTA_ALERT_ROLE_ID",
                        None,
                    ):
                        await cog._check_pre_post_reminders(now)
                        await cog._check_unassigned_posts(now)

                    reminders.send.assert_awaited_once()
                    self.assertTrue(
                        await database.was_post_alert_delivered(
                            post_id,
                            expected_kind,
                        )
                    )
                    other_kind = (
                        scheduler.ALERT_KIND_FOUR_HOUR
                        if expected_kind
                        == scheduler.ALERT_KIND_FIFTEEN_MINUTE
                        else scheduler.ALERT_KIND_FIFTEEN_MINUTE
                    )
                    self.assertFalse(
                        await database.was_post_alert_delivered(
                            post_id,
                            other_kind,
                        )
                    )
                finally:
                    database.DB_PATH = original_db_path
                    database.IMAGES_DIR = original_images_dir

    async def test_tick_shares_timestamp_when_clock_crosses_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                now = int(time.time())
                post_id = await database.insert_post(
                    discord_message_id="56",
                    content="Boundary race post",
                    scheduled_at=now + scheduler.SECONDS_15_MIN + 1,
                    created_by="42",
                )
                await database.update_post_skip_unclaimed_pings(
                    post_id,
                    False,
                )
                reminders = SimpleNamespace(send=AsyncMock())
                bot = Mock()
                bot.get_channel.return_value = reminders
                cog = scheduler.SchedulerCog(bot)
                advancing_clock = Mock(side_effect=[now, now + 2])

                with (
                    patch.object(scheduler, "ROTA_ALERT_ROLE_ID", None),
                    patch.object(
                        scheduler,
                        "time",
                        SimpleNamespace(time=advancing_clock),
                    ),
                    patch.object(cog, "_check_go_live", new=AsyncMock()),
                    patch.object(
                        cog,
                        "_check_discord_deliveries",
                        new=AsyncMock(),
                    ),
                    patch.object(cog, "_check_daily_gap", new=AsyncMock()),
                ):
                    await scheduler.SchedulerCog.tick.coro(cog)

                advancing_clock.assert_called_once_with()
                reminders.send.assert_awaited_once()
                self.assertTrue(
                    await database.was_post_alert_delivered(
                        post_id,
                        scheduler.ALERT_KIND_FOUR_HOUR,
                    )
                )
                self.assertFalse(
                    await database.was_post_alert_delivered(
                        post_id,
                        scheduler.ALERT_KIND_FIFTEEN_MINUTE,
                    )
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir

    async def test_migrated_optional_post_alerts_after_require_toggle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                now = int(time.time())
                async with aiosqlite.connect(database.DB_PATH) as db:
                    await db.execute(
                        """
                        CREATE TABLE posts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            discord_message_id TEXT NOT NULL,
                            content TEXT NOT NULL,
                            scheduled_at INTEGER NOT NULL,
                            created_by TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'scheduled',
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    await db.execute(
                        """
                        INSERT INTO posts (
                            discord_message_id, content, scheduled_at,
                            created_by, status, created_at
                        ) VALUES ('55', 'Migrated post', ?, '42', 'scheduled', ?)
                        """,
                        (now + 60 * 60, now),
                    )
                    await db.commit()

                await database.init_db()
                migrated = await database.get_post_by_id(1)
                self.assertEqual(migrated["skip_unclaimed_pings"], 1)

                reminders = SimpleNamespace(send=AsyncMock())
                bot = Mock()
                bot.get_channel.return_value = reminders
                cog = scheduler.SchedulerCog(bot)

                with patch.object(scheduler, "ROTA_ALERT_ROLE_ID", None):
                    await cog._check_unassigned_posts()
                    reminders.send.assert_not_awaited()
                    self.assertFalse(
                        await database.was_post_alert_delivered(
                            1,
                            scheduler.ALERT_KIND_FOUR_HOUR,
                        )
                    )

                    interaction = SimpleNamespace(
                        user=SimpleNamespace(id=99),
                        response=SimpleNamespace(
                            edit_message=AsyncMock(),
                            send_message=AsyncMock(),
                        ),
                    )
                    await schedule.PostButtonView(
                        bot,
                        post_id=1,
                    )._on_toggle_skip_unclaimed_pings(interaction)
                    await cog._check_unassigned_posts()

                    restarted = scheduler.SchedulerCog(bot)
                    await restarted._check_unassigned_posts()

                    # Required -> optional -> required and claim transitions
                    # cannot resend an alert kind already persisted.
                    await database.update_post_skip_unclaimed_pings(1, True)
                    await database.update_post_skip_unclaimed_pings(1, False)
                    await database.add_claim(1, "99")
                    await database.remove_claim(1, "99")
                    await restarted._check_unassigned_posts()

                reminders.send.assert_awaited_once()
                alert = reminders.send.await_args
                self.assertEqual(
                    alert.kwargs["embed"].description,
                    "Migrated post",
                )
                self.assertEqual(
                    alert.kwargs["allowed_mentions"].to_dict(),
                    {"users": [42], "parse": []},
                )
                toggled = await database.get_post_by_id(1)
                self.assertEqual(toggled["skip_unclaimed_pings"], 0)
                self.assertTrue(
                    await database.was_post_alert_delivered(
                        1,
                        scheduler.ALERT_KIND_FOUR_HOUR,
                    )
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir

    async def test_fifteen_minute_delivery_survives_restart_and_unclaim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                post_id = await database.insert_post(
                    discord_message_id="77",
                    content="Claimed post",
                    scheduled_at=int(time.time()) + 5 * 60,
                    created_by="42",
                )
                await database.add_claim(post_id, "42")
                reminders = SimpleNamespace(send=AsyncMock())
                bot = Mock()
                bot.get_channel.return_value = reminders

                first = scheduler.SchedulerCog(bot)
                await first._check_pre_post_reminders()
                reminders.send.assert_awaited_once()

                await database.remove_claim(post_id, "42")
                await database.update_post_skip_unclaimed_pings(
                    post_id,
                    False,
                )
                restarted = scheduler.SchedulerCog(bot)
                await restarted._check_pre_post_reminders()

                reminders.send.assert_awaited_once()
                self.assertTrue(
                    await database.was_post_alert_delivered(
                        post_id,
                        scheduler.ALERT_KIND_FIFTEEN_MINUTE,
                    )
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir


class GoLiveRefreshConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def state() -> dict:
        return {
            "id": 1,
            "discord_message_id": "10",
            "content": "Concurrent post",
            "scheduled_at": 0,
            "created_by": "1",
            "image_path": None,
            "post_to_x": 0,
            "skip_unclaimed_pings": 1,
            "status": "scheduled",
        }

    async def _run_order(self, refresh_first: bool):
        state = self.state()
        bot_user = object()
        channel = LockTestChannel(bot_user)
        bot = Mock()
        bot.user = bot_user
        bot.get_channel.return_value = channel
        entered = asyncio.Event()
        release = asyncio.Event()

        async def get_posts():
            if refresh_first:
                entered.set()
                await release.wait()
            return [dict(state)] if state["status"] == "scheduled" else []

        async def update_message_id(_post_id, message_id):
            state["discord_message_id"] = str(message_id)

        async def get_post(_post_id):
            if not refresh_first:
                entered.set()
                await release.wait()
            return dict(state)

        async def transition(_post_id, expected_id, live_id, _now):
            if (
                state["status"] != "scheduled"
                or state["discord_message_id"] != str(expected_id)
            ):
                return False
            state["status"] = "live"
            state["discord_message_id"] = live_id
            return True

        with (
            patch.object(
                schedule,
                "get_all_scheduled_posts",
                new=AsyncMock(side_effect=get_posts),
            ),
            patch.object(
                schedule,
                "get_claimers_for_post",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                schedule,
                "get_unavailable_for_post",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                schedule,
                "update_post_message_id",
                new=AsyncMock(side_effect=update_message_id),
            ),
            patch.object(
                scheduler,
                "get_post_by_id",
                new=AsyncMock(side_effect=get_post),
            ),
            patch.object(
                scheduler,
                "transition_due_post_to_live",
                new=AsyncMock(side_effect=transition),
            ),
        ):
            cog = scheduler.SchedulerCog(bot)
            if refresh_first:
                first = asyncio.create_task(schedule.repost_all_scheduled(bot))
                await entered.wait()
                second = asyncio.create_task(cog._take_due_post(1, channel))
            else:
                first = asyncio.create_task(cog._take_due_post(1, channel))
                await entered.wait()
                second = asyncio.create_task(schedule.repost_all_scheduled(bot))

            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(state["status"], "live")
        self.assertFalse(
            any("embed" in message.send_kwargs for message in channel.messages)
        )
        self.assertEqual(
            [message.content for message in channel.messages],
            [schedule.SCHEDULE_PANEL_TEXT],
        )

    async def test_refresh_then_go_live_leaves_no_ghost(self):
        await self._run_order(refresh_first=True)

    async def test_go_live_then_refresh_leaves_no_ghost(self):
        await self._run_order(refresh_first=False)


class GoLiveDispatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_go_live_dispatches_each_media_path_and_skips_manual_x(self):
        posts = [
            {
                "id": 1,
                "discord_message_id": "1",
                "content": "Text",
                "scheduled_at": 1,
                "created_by": "10",
                "image_path": None,
                "post_to_x": 1,
                "skip_unclaimed_pings": 1,
            },
            {
                "id": 2,
                "discord_message_id": "2",
                "content": "Image",
                "scheduled_at": 1,
                "created_by": "10",
                "image_path": "image.png",
                "post_to_x": 1,
                "skip_unclaimed_pings": 1,
            },
            {
                "id": 3,
                "discord_message_id": "3",
                "content": "GIF",
                "scheduled_at": 1,
                "created_by": "10",
                "image_path": "animation.gif",
                "post_to_x": 1,
                "skip_unclaimed_pings": 1,
            },
            {
                "id": 4,
                "discord_message_id": "4",
                "content": "Video",
                "scheduled_at": 1,
                "created_by": "10",
                "image_path": "video.mp4",
                "post_to_x": 1,
                "skip_unclaimed_pings": 1,
            },
            {
                "id": 5,
                "discord_message_id": "5",
                "content": "Manual video",
                "scheduled_at": 1,
                "created_by": "10",
                "image_path": "manual.mp4",
                "post_to_x": 0,
                "skip_unclaimed_pings": 1,
            },
        ]
        bot = Mock()
        bot.get_channel.return_value = None
        post_tweet_result = Mock(
            side_effect=[
                scheduler.XPostResult(
                    scheduler.X_POST_SUCCESS,
                    "https://x.com/i/web/status/1",
                ),
                scheduler.XPostResult(
                    scheduler.X_POST_SUCCESS,
                    "https://x.com/i/web/status/2",
                ),
                scheduler.XPostResult(
                    scheduler.X_POST_SUCCESS,
                    "https://x.com/i/web/status/3",
                ),
                scheduler.XPostResult(
                    scheduler.X_POST_SUCCESS,
                    "https://x.com/i/web/status/4",
                ),
            ]
        )

        with (
            patch.object(
                scheduler,
                "get_due_posts",
                new=AsyncMock(return_value=posts),
            ),
            patch.object(
                scheduler,
                "get_claimers_for_post",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                scheduler.SchedulerCog,
                "_take_due_post",
                new=AsyncMock(side_effect=posts),
            ),
            patch.object(
                scheduler,
                "record_x_post_success",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler,
                "post_tweet_result",
                new=post_tweet_result,
            ),
            patch.object(scheduler, "X_ENABLED", True),
            patch.object(scheduler, "X_LIVE_POST_LINK_CHANNEL_IDS", []),
        ):
            await scheduler.SchedulerCog(bot)._check_go_live()

        self.assertEqual(
            post_tweet_result.call_args_list,
            [
                call("Text", None),
                call("Image", "image.png"),
                call("GIF", "animation.gif"),
                call("Video", "video.mp4"),
            ],
        )

    async def test_discord_off_records_x_success_without_link_deliveries(self):
        post = {
            "id": 1,
            "discord_message_id": "1",
            "content": "X only",
            "scheduled_at": 1,
            "created_by": "10",
            "image_path": None,
            "post_to_x": 1,
            "post_to_discord": 0,
            "discord_delay_minutes": 60,
            "skip_unclaimed_pings": 1,
        }
        bot = Mock()
        bot.get_channel.return_value = None
        record_success = AsyncMock()
        with (
            patch.object(
                scheduler,
                "get_due_posts",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                scheduler,
                "get_claimers_for_post",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                scheduler.SchedulerCog,
                "_take_due_post",
                new=AsyncMock(return_value=post),
            ),
            patch.object(
                scheduler,
                "post_tweet_result",
                return_value=scheduler.XPostResult(
                    scheduler.X_POST_SUCCESS,
                    "https://x.com/i/web/status/1",
                ),
            ),
            patch.object(
                scheduler,
                "record_x_post_success",
                new=record_success,
            ),
            patch.object(scheduler, "X_ENABLED", True),
            patch.object(
                scheduler,
                "X_LIVE_POST_LINK_CHANNEL_IDS",
                [100, 200],
            ),
            patch.object(
                scheduler,
                "time",
                SimpleNamespace(time=Mock(return_value=1_000)),
            ),
        ):
            await scheduler.SchedulerCog(bot)._check_go_live()

        record_success.assert_awaited_once_with(
            1,
            "https://x.com/i/web/status/1",
            1_000,
            [],
            60,
        )


class DiscordLinkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_delivery_sends_once_and_records_message(self):
        channel = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=900)),
        )
        bot = Mock()
        bot.get_channel.return_value = channel
        delivery = {
            "post_id": 1,
            "channel_id": "100",
            "due_at": 1_000,
            "attempt_count": 0,
            "tweet_url": "https://x.com/i/web/status/1",
        }
        with (
            patch.object(
                scheduler,
                "get_due_discord_deliveries",
                new=AsyncMock(return_value=[delivery]),
            ),
            patch.object(
                scheduler,
                "record_discord_delivery_success",
                new=AsyncMock(return_value=True),
            ) as record_success,
            patch.object(
                scheduler,
                "record_discord_delivery_failure",
                new=AsyncMock(),
            ) as record_failure,
        ):
            await scheduler.SchedulerCog(bot)._check_discord_deliveries(1_000)

        bot.get_channel.assert_called_once_with(100)
        channel.send.assert_awaited_once()
        sent = channel.send.await_args
        self.assertEqual(
            sent.args[0],
            "https://x.com/i/web/status/1",
        )
        self.assertEqual(
            sent.kwargs["allowed_mentions"].to_dict(),
            schedule.discord.AllowedMentions.none().to_dict(),
        )
        record_success.assert_awaited_once_with(
            1,
            "100",
            1_000,
            "900",
        )
        record_failure.assert_not_awaited()

    async def test_missing_channel_stays_pending_for_retry(self):
        bot = Mock()
        bot.get_channel.return_value = None
        delivery = {
            "post_id": 1,
            "channel_id": "100",
            "due_at": 1_000,
            "attempt_count": 0,
            "tweet_url": "https://x.com/i/web/status/1",
        }
        with (
            patch.object(
                scheduler,
                "get_due_discord_deliveries",
                new=AsyncMock(return_value=[delivery]),
            ),
            patch.object(
                scheduler,
                "record_discord_delivery_success",
                new=AsyncMock(),
            ) as record_success,
            patch.object(
                scheduler,
                "record_discord_delivery_failure",
                new=AsyncMock(),
            ) as record_failure,
            self.assertLogs("rota-bot.scheduler", level="WARNING"),
        ):
            await scheduler.SchedulerCog(bot)._check_discord_deliveries(1_000)

        record_success.assert_not_awaited()
        record_failure.assert_awaited_once_with(1, "100", 1_000)


class GoLiveOutcomeNotificationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def post(*, optional: bool = True) -> dict:
        return {
            "id": 1,
            "discord_message_id": "10",
            "content": "<@123> operational content",
            "scheduled_at": 1,
            "created_by": "10",
            "image_path": "video.mp4",
            "post_to_x": 1,
            "skip_unclaimed_pings": 1 if optional else 0,
            "status": "scheduled",
        }

    async def test_optional_unclaimed_known_failure_sends_one_unpinged_notice(self):
        post = self.post()
        reminders = SimpleNamespace(send=AsyncMock())
        bot = Mock()
        bot.get_channel.side_effect = (
            lambda channel_id: reminders
            if channel_id == scheduler.REMINDERS_CHANNEL_ID
            else None
        )
        cog = scheduler.SchedulerCog(bot)
        with (
            patch.object(
                scheduler,
                "get_due_posts",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                scheduler.SchedulerCog,
                "_take_due_post",
                new=AsyncMock(side_effect=[post, None]),
            ),
            patch.object(
                scheduler,
                "get_claimers_for_post",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                scheduler,
                "post_tweet_result",
                return_value=scheduler.XPostResult(
                    scheduler.X_POST_FAILED,
                    detail="media upload failed",
                ),
            ),
            patch.object(scheduler, "X_ENABLED", True),
            patch.object(scheduler, "X_LIVE_POST_LINK_CHANNEL_IDS", []),
        ):
            await cog._check_go_live()
            await cog._check_go_live()

        reminders.send.assert_awaited_once()
        notice = reminders.send.await_args
        self.assertIn("No X post was created", notice.args[0])
        self.assertEqual(
            notice.kwargs["allowed_mentions"].to_dict(),
            schedule.discord.AllowedMentions.none().to_dict(),
        )
        self.assertEqual(
            notice.kwargs["embed"].description,
            post["content"],
        )

    async def test_claimed_unknown_outcome_uses_one_targeted_check_notice(self):
        post = self.post(optional=False)
        reminders = SimpleNamespace(send=AsyncMock())
        archive = SimpleNamespace(send=AsyncMock())
        bot = Mock()
        bot.get_channel.side_effect = lambda channel_id: {
            scheduler.REMINDERS_CHANNEL_ID: reminders,
            scheduler.ARCHIVE_CHANNEL_ID: archive,
        }.get(channel_id)
        with (
            patch.object(
                scheduler,
                "get_due_posts",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                scheduler.SchedulerCog,
                "_take_due_post",
                new=AsyncMock(return_value=post),
            ),
            patch.object(
                scheduler,
                "get_claimers_for_post",
                new=AsyncMock(return_value=["42"]),
            ),
            patch.object(
                scheduler,
                "post_tweet_result",
                return_value=scheduler.XPostResult(
                    scheduler.X_POST_UNKNOWN,
                    detail="create response malformed",
                ),
            ),
            patch.object(scheduler, "X_ENABLED", True),
            patch.object(scheduler, "X_LIVE_POST_LINK_CHANNEL_IDS", []),
        ):
            await scheduler.SchedulerCog(bot)._check_go_live()

        reminders.send.assert_awaited_once()
        notice = reminders.send.await_args
        self.assertIn("outcome is unknown", notice.args[0])
        self.assertIn("Check the X account before retrying", notice.args[0])
        self.assertNotIn("publish it manually", notice.args[0])
        self.assertEqual(
            notice.kwargs["allowed_mentions"].to_dict(),
            {"users": [42], "parse": []},
        )
        archive.send.assert_awaited_once()
        archive_text = archive.send.await_args.args[0]
        self.assertIn("outcome unknown", archive_text)
        self.assertNotIn("Post went live on X", archive_text)


class DiscordPayloadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_4000_body_and_large_metadata_fit_send_and_edit_limits(self):
        post = {
            "id": 1,
            "discord_message_id": "old",
            "content": "x" * 4000,
            "scheduled_at": 100,
            "created_by": "999",
            "image_path": None,
            "post_to_x": 1,
            "skip_unclaimed_pings": 0,
        }
        claimers = [str(index) for index in range(1, 101)]
        unavailable = [str(index) for index in range(101, 201)]

        async def empty_history():
            if False:
                yield None

        channel = SimpleNamespace(
            id=schedule.SCHEDULED_CHANNEL_ID,
            history=lambda *, limit: empty_history(),
            send=AsyncMock(
                side_effect=[
                    SimpleNamespace(id=500),
                    SimpleNamespace(id=501),
                ]
            ),
        )
        bot = Mock()
        bot.user = object()
        bot.get_channel.return_value = channel
        with (
            patch.object(
                schedule,
                "get_all_scheduled_posts",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                schedule,
                "get_claimers_for_post",
                new=AsyncMock(return_value=claimers),
            ),
            patch.object(
                schedule,
                "get_unavailable_for_post",
                new=AsyncMock(return_value=unavailable),
            ),
            patch.object(
                schedule,
                "update_post_message_id",
                new=AsyncMock(),
            ),
        ):
            self.assertTrue(await schedule.repost_all_scheduled(bot))

        sent = channel.send.await_args_list[0]
        self.assertLessEqual(len(sent.args[0]), 2000)
        self.assertEqual(len(sent.kwargs["embed"].description), 4000)
        self.assertIn("+80 more", sent.args[0])
        self.assertEqual(
            sent.kwargs["allowed_mentions"].to_dict(),
            schedule.discord.AllowedMentions.none().to_dict(),
        )

        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            )
        )
        with (
            patch.object(
                schedule,
                "get_post_by_id",
                new=AsyncMock(return_value=post),
            ),
            patch.object(
                schedule,
                "get_claimers_for_post",
                new=AsyncMock(return_value=claimers),
            ),
            patch.object(
                schedule,
                "get_unavailable_for_post",
                new=AsyncMock(return_value=unavailable),
            ),
        ):
            await schedule.PostButtonView(bot, 1)._update_message(interaction)

        edited = interaction.response.edit_message.await_args
        self.assertLessEqual(len(edited.kwargs["content"]), 2000)
        self.assertEqual(len(edited.kwargs["embed"].description), 4000)
        self.assertIn("+80 more", edited.kwargs["content"])
        self.assertEqual(
            edited.kwargs["allowed_mentions"].to_dict(),
            schedule.discord.AllowedMentions.none().to_dict(),
        )

    async def test_large_claimer_notification_batches_every_user_safely(self):
        channel = SimpleNamespace(send=AsyncMock())
        cog = scheduler.SchedulerCog(Mock())
        user_ids = [str(index) for index in range(1, 151)]
        embed = schedule.build_post_embed("x" * 4000)

        sent = await cog._send_user_notifications(
            channel,
            "Reminder body",
            user_ids,
            embed=embed,
        )

        self.assertTrue(sent)
        self.assertEqual(channel.send.await_count, 2)
        notified = []
        for send_call in channel.send.await_args_list:
            self.assertLessEqual(len(send_call.args[0]), 2000)
            self.assertLessEqual(
                len(send_call.kwargs["embed"].description),
                4096,
            )
            allowed = send_call.kwargs["allowed_mentions"].to_dict()
            self.assertNotIn("everyone", allowed.get("parse", []))
            notified.extend(str(user_id) for user_id in allowed["users"])
        self.assertEqual(notified, user_ids)


class ComposerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_view_timeout_only_deletes_unsubmitted_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            abandoned = Path(temp_dir) / "abandoned.png"
            abandoned.write_bytes(b"media")
            view = schedule.ScheduleView(
                Mock(),
                "Post",
                user_id=1,
                media_path=str(abandoned),
            )
            await view.on_timeout()
            self.assertFalse(abandoned.exists())

            committed = Path(temp_dir) / "committed.png"
            committed.write_bytes(b"media")
            view = schedule.ScheduleView(
                Mock(),
                "Post",
                user_id=1,
                media_path=str(committed),
            )
            view.submitted = True
            await view.on_timeout()
            self.assertTrue(committed.exists())

    async def test_invalid_exact_input_retains_active_view_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "active.png"
            media_path.write_bytes(b"media")
            view = schedule.ScheduleView(
                Mock(),
                "Post",
                user_id=1,
                media_path=str(media_path),
            )
            modal = schedule.ExactDateTimeModal(view)
            modal.date_input._value = "not-a-date"
            modal.time_input._value = "12:00"
            interaction = SimpleNamespace(
                response=SimpleNamespace(edit_message=AsyncMock())
            )

            await modal.on_submit(interaction)

            self.assertTrue(media_path.exists())
            self.assertFalse(view.submitted)
            self.assertEqual(view.media_path, str(media_path))

    async def test_successful_insert_transfers_media_ownership_to_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "committed.png"
            media_path.write_bytes(b"media")
            view = schedule.ScheduleView(
                Mock(),
                "Post",
                user_id=1,
                media_path=str(media_path),
            )
            view.submitted = True
            interaction = SimpleNamespace(
                response=SimpleNamespace(edit_message=AsyncMock())
            )
            with (
                patch.object(schedule, "insert_post", new=AsyncMock()),
                patch.object(
                    schedule,
                    "repost_all_scheduled",
                    new=AsyncMock(return_value=True),
                ),
            ):
                await view._finalize_time(
                    interaction,
                    123,
                    datetime.now(tz=ZoneInfo("UTC")),
                )
                await view.on_timeout()

            self.assertTrue(media_path.exists())
            self.assertIsNone(view.media_path)

    async def test_failed_picker_delivery_cleans_panel_and_slash_media(self):
        for modal_factory in (
            lambda: schedule.SchedulePostModal(Mock(), user_id=1),
            lambda: schedule.PostContentModal(
                Mock(),
                user_id=1,
                attachment=SimpleNamespace(),
            ),
        ):
            with self.subTest(modal=modal_factory):
                with tempfile.TemporaryDirectory() as temp_dir:
                    media_path = Path(temp_dir) / "saved.png"
                    media_path.write_bytes(b"media")
                    modal = modal_factory()
                    modal.content_input._value = "Post"
                    if isinstance(modal, schedule.SchedulePostModal):
                        modal.file_upload._values = [SimpleNamespace()]
                    interaction = SimpleNamespace(
                        response=SimpleNamespace(defer=AsyncMock()),
                        followup=SimpleNamespace(
                            send=AsyncMock(
                                side_effect=RuntimeError("Discord failed")
                            )
                        ),
                    )
                    with patch.object(
                        schedule,
                        "save_validated_media_attachment",
                        new=AsyncMock(return_value=(str(media_path), None)),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "Discord failed",
                        ):
                            await modal.on_submit(interaction)
                    self.assertFalse(media_path.exists())

    async def test_post_content_modal_timeout_cleans_owned_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "presaved.png"
            media_path.write_bytes(b"media")
            modal = schedule.PostContentModal(
                Mock(),
                user_id=1,
                attachment=None,
            )
            modal._saved_media_path = str(media_path)

            await modal.on_timeout()

            self.assertFalse(media_path.exists())


class PremiumContentIntegrationTests(unittest.TestCase):
    def test_281_to_4000_characters_are_accepted_as_premium_long_posts(self):
        self.assertIsNone(schedule.premium_long_post_status("x" * 280))

        for length in (281, 4000):
            with self.subTest(length=length):
                content = "x" * length
                status = schedule.ScheduleView(
                    Mock(),
                    content,
                    user_id=1,
                )._status_text()
                self.assertIn("X Premium long post", status)
                self.assertIn(f"{length:,} characters (accepted)", status)
                self.assertEqual(
                    schedule.build_post_embed(content).description,
                    content,
                )
                self.assertLess(
                    len(schedule.format_scheduled_message(content, 1, "1")),
                    2000,
                )


if __name__ == "__main__":
    unittest.main()
