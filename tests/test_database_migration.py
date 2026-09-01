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
                        "SELECT status, skip_unclaimed_pings FROM posts ORDER BY id"
                    )
                    self.assertEqual(
                        await cursor.fetchall(), [("scheduled", 1), ("live", 0)]
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


if __name__ == "__main__":
    unittest.main()
