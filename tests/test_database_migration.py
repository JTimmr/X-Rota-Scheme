import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import database


class OptionalClaimingMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_posts_migrate_once_and_new_posts_are_optional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
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
                            created_at INTEGER NOT NULL,
                            image_path TEXT,
                            tweet_url TEXT,
                            post_to_x INTEGER NOT NULL DEFAULT 1
                        )
                        """
                    )
                    await db.executemany(
                        """
                        INSERT INTO posts (
                            discord_message_id, content, scheduled_at, created_by,
                            status, created_at
                        ) VALUES (?, ?, 1, '1', ?, 1)
                        """,
                        [("scheduled", "scheduled", "scheduled"), ("live", "live", "live")],
                    )
                    await db.commit()

                await database.init_db()

                async with aiosqlite.connect(database.DB_PATH) as db:
                    cursor = await db.execute(
                        """
                        SELECT
                            status,
                            skip_unclaimed_pings,
                            post_to_discord,
                            discord_delay_minutes,
                            x_published_at
                        FROM posts
                        ORDER BY id
                        """
                    )
                    self.assertEqual(
                        await cursor.fetchall(),
                        [
                            ("scheduled", 1, 1, 0, None),
                            ("live", 0, 1, 0, None),
                        ],
                    )
                    await db.execute(
                        "UPDATE posts SET skip_unclaimed_pings = 0 WHERE status = 'scheduled'"
                    )
                    await db.commit()

                await database.init_db()
                new_post_id = await database.insert_post(
                    discord_message_id="new",
                    content="new",
                    scheduled_at=2,
                    created_by="1",
                )

                async with aiosqlite.connect(database.DB_PATH) as db:
                    cursor = await db.execute(
                        "SELECT skip_unclaimed_pings FROM posts WHERE id = 1"
                    )
                    self.assertEqual((await cursor.fetchone())[0], 0)
                    cursor = await db.execute(
                        "SELECT skip_unclaimed_pings FROM posts WHERE id = ?",
                        (new_post_id,),
                    )
                    self.assertEqual((await cursor.fetchone())[0], 1)
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                        (database.OPTIONAL_CLAIMING_MIGRATION,),
                    )
                    self.assertEqual((await cursor.fetchone())[0], 1)
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir

    async def test_alert_delivery_table_is_idempotent_unique_and_cascades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                await database.init_db()
                post_id = await database.insert_post(
                    discord_message_id="1",
                    content="Post",
                    scheduled_at=1,
                    created_by="1",
                )

                self.assertTrue(
                    await database.record_post_alert_delivery(post_id, "4h")
                )
                self.assertFalse(
                    await database.record_post_alert_delivery(post_id, "4h")
                )
                self.assertTrue(
                    await database.was_post_alert_delivered(post_id, "4h")
                )

                await database.delete_post_by_id(post_id)
                async with aiosqlite.connect(database.DB_PATH) as db:
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM post_alert_deliveries"
                    )
                    self.assertEqual((await cursor.fetchone())[0], 0)
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir


class DiscordDeliveryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_queues_due_channels_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                post_id = await database.insert_post(
                    discord_message_id="77",
                    content="Delayed link",
                    scheduled_at=1,
                    created_by="1",
                    discord_delay_minutes=15,
                )
                self.assertTrue(
                    await database.transition_due_post_to_live(
                        post_id,
                        "77",
                        f"live_{post_id}",
                        2,
                    )
                )
                await database.record_x_post_success(
                    post_id,
                    "https://x.com/i/web/status/123",
                    1_000,
                    [100, 200, 100],
                    15,
                )

                self.assertEqual(
                    await database.get_due_discord_deliveries(1_899),
                    [],
                )
                due = await database.get_due_discord_deliveries(1_900)
                self.assertEqual(
                    [delivery["channel_id"] for delivery in due],
                    ["100", "200"],
                )

                self.assertTrue(
                    await database.record_discord_delivery_success(
                        post_id,
                        "100",
                        1_900,
                        "500",
                    )
                )
                await database.record_discord_delivery_failure(
                    post_id,
                    "200",
                    1_900,
                )

                await database.init_db()
                remaining = await database.get_due_discord_deliveries(1_901)
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0]["channel_id"], "200")
                self.assertEqual(remaining[0]["attempt_count"], 1)
                post = await database.get_post_by_id(post_id)
                self.assertEqual(post["x_published_at"], 1_000)
                self.assertEqual(
                    post["tweet_url"],
                    "https://x.com/i/web/status/123",
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir

    async def test_discord_settings_only_change_scheduled_posts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                post_id = await database.insert_post(
                    discord_message_id="88",
                    content="Settings",
                    scheduled_at=1,
                    created_by="1",
                )
                self.assertTrue(
                    await database.update_scheduled_post_discord_settings(
                        post_id,
                        False,
                        60,
                    )
                )
                post = await database.get_post_by_id(post_id)
                self.assertEqual(post["post_to_discord"], 0)
                self.assertEqual(post["discord_delay_minutes"], 60)

                self.assertTrue(
                    await database.transition_due_post_to_live(
                        post_id,
                        "88",
                        f"live_{post_id}",
                        2,
                    )
                )
                self.assertFalse(
                    await database.update_scheduled_post_discord_settings(
                        post_id,
                        True,
                        0,
                    )
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir


class CancellationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_archives_and_reschedules_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_path = database.DB_PATH
            original_images_dir = database.IMAGES_DIR
            database.DB_PATH = Path(temp_dir) / "rota.db"
            database.IMAGES_DIR = Path(temp_dir) / "images"
            try:
                await database.init_db()
                post_id = await database.insert_post(
                    discord_message_id="500",
                    content="Recoverable post",
                    scheduled_at=2_000,
                    created_by="42",
                )
                await database.add_claim(post_id, "7")
                await database.add_unavailable(post_id, "8")
                await database.record_post_alert_delivery(post_id, "4h")

                cancellation = (
                    await database.cancel_scheduled_post_by_message_id(
                        "500",
                        1_000,
                    )
                )

                self.assertIsNotNone(cancellation)
                cancellation_id = cancellation["cancellation_id"]
                post = await database.get_post_by_id(post_id)
                self.assertEqual(post["status"], "cancelled")
                self.assertEqual(
                    await database.get_claimers_for_post(post_id),
                    ["7"],
                )
                self.assertEqual(
                    await database.get_unavailable_for_post(post_id),
                    ["8"],
                )
                async with aiosqlite.connect(database.DB_PATH) as db:
                    cursor = await db.execute("SELECT COUNT(*) FROM posts")
                    self.assertEqual((await cursor.fetchone())[0], 1)

                self.assertTrue(
                    await database.record_cancellation_archive_message(
                        cancellation_id,
                        "900",
                    )
                )
                open_cancellations = (
                    await database.get_open_cancellations_with_archive()
                )
                self.assertEqual(len(open_cancellations), 1)
                self.assertEqual(
                    open_cancellations[0]["cancelled_scheduled_at"],
                    2_000,
                )

                self.assertTrue(
                    await database.reschedule_cancelled_post(
                        cancellation_id,
                        3_000,
                        "99",
                        1_100,
                    )
                )
                self.assertFalse(
                    await database.reschedule_cancelled_post(
                        cancellation_id,
                        4_000,
                        "99",
                        1_200,
                    )
                )

                post = await database.get_post_by_id(post_id)
                self.assertEqual(post["status"], "scheduled")
                self.assertEqual(post["scheduled_at"], 3_000)
                self.assertFalse(
                    await database.was_post_alert_delivered(post_id, "4h")
                )
                cancellation = await database.get_cancellation_with_post(
                    cancellation_id
                )
                self.assertEqual(cancellation["rescheduled_at"], 1_100)
                self.assertEqual(cancellation["rescheduled_by"], "99")
                self.assertEqual(cancellation["cancelled_scheduled_at"], 2_000)
                self.assertEqual(
                    await database.get_open_cancellations_with_archive(),
                    [],
                )
            finally:
                database.DB_PATH = original_db_path
                database.IMAGES_DIR = original_images_dir


if __name__ == "__main__":
    unittest.main()
