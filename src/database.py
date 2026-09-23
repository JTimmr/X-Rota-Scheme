import time
from pathlib import Path

import aiosqlite

DB_PATH = Path("/app/data/rota.db")
IMAGES_DIR = Path("/app/data/images")
OPTIONAL_CLAIMING_MIGRATION = "phase_1_optional_claiming_default"


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT NOT NULL,
                content TEXT NOT NULL,
                scheduled_at INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at INTEGER NOT NULL,
                image_path TEXT,
                tweet_url TEXT,
                skip_unclaimed_pings INTEGER NOT NULL DEFAULT 1,
                post_to_x INTEGER NOT NULL DEFAULT 1,
                post_to_discord INTEGER NOT NULL DEFAULT 1,
                discord_delay_minutes INTEGER NOT NULL DEFAULT 0,
                x_published_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                UNIQUE(post_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS unavailable (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                UNIQUE(post_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS post_alert_deliveries (
                post_id INTEGER NOT NULL,
                alert_kind TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (post_id, alert_kind),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS post_discord_deliveries (
                post_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                due_at INTEGER NOT NULL,
                delivered_at INTEGER,
                discord_message_id TEXT,
                last_attempt_at INTEGER,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (post_id, channel_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS post_cancellations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                cancelled_at INTEGER NOT NULL,
                scheduled_at INTEGER NOT NULL,
                archive_message_id TEXT,
                rescheduled_at INTEGER,
                rescheduled_by TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_post_discord_deliveries_due
            ON post_discord_deliveries (delivered_at, due_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_post_cancellations_open
            ON post_cancellations (rescheduled_at, archive_message_id)
        """)
        await db.commit()

        # Migrate from old schema: rename reactions to claims if needed
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reactions'")
        if await cursor.fetchone():
            await db.execute("INSERT OR IGNORE INTO claims (post_id, user_id, created_at) SELECT post_id, user_id, reacted_at FROM reactions")
            await db.execute("DROP TABLE reactions")
            await db.commit()

        # Migrate: add columns if missing
        cursor = await db.execute("PRAGMA table_info(posts)")
        columns = [row[1] async for row in cursor]
        if "image_path" not in columns:
            await db.execute("ALTER TABLE posts ADD COLUMN image_path TEXT")
            await db.commit()
        if "tweet_url" not in columns:
            await db.execute("ALTER TABLE posts ADD COLUMN tweet_url TEXT")
            await db.commit()
        if "skip_unclaimed_pings" not in columns:
            await db.execute(
                "ALTER TABLE posts ADD COLUMN skip_unclaimed_pings INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        if "post_to_x" not in columns:
            await db.execute(
                "ALTER TABLE posts ADD COLUMN post_to_x INTEGER NOT NULL DEFAULT 1"
            )
            await db.commit()
        if "post_to_discord" not in columns:
            await db.execute(
                "ALTER TABLE posts ADD COLUMN post_to_discord INTEGER NOT NULL DEFAULT 1"
            )
            await db.commit()
        if "discord_delay_minutes" not in columns:
            await db.execute(
                "ALTER TABLE posts ADD COLUMN discord_delay_minutes INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        if "x_published_at" not in columns:
            await db.execute("ALTER TABLE posts ADD COLUMN x_published_at INTEGER")
            await db.commit()

        # Phase 1: make all posts scheduled at upgrade time optional exactly once.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
        """)
        await db.execute(
            """
            UPDATE posts
            SET skip_unclaimed_pings = 1
            WHERE status = 'scheduled'
              AND NOT EXISTS (
                  SELECT 1 FROM schema_migrations WHERE name = ?
              )
            """,
            (OPTIONAL_CLAIMING_MIGRATION,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (OPTIONAL_CLAIMING_MIGRATION, int(time.time())),
        )
        await db.commit()


async def insert_post(
    discord_message_id: str,
    content: str,
    scheduled_at: int,
    created_by: str,
    image_path: str | None = None,
    post_to_x: bool = True,
    post_to_discord: bool = True,
    discord_delay_minutes: int = 0,
) -> int:
    if discord_delay_minutes < 0:
        raise ValueError("discord_delay_minutes cannot be negative")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO posts (
                discord_message_id, content, scheduled_at, created_by, created_at,
                image_path, post_to_x, post_to_discord, discord_delay_minutes,
                skip_unclaimed_pings
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discord_message_id,
                content,
                scheduled_at,
                created_by,
                int(time.time()),
                image_path,
                1 if post_to_x else 0,
                1 if post_to_discord else 0,
                discord_delay_minutes,
                1,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def cancel_scheduled_post_by_message_id(
    discord_message_id: str,
    cancelled_at: int,
) -> dict | None:
    """Soft-cancel one scheduled post and retain its complete related history."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT *
            FROM posts
            WHERE discord_message_id = ?
              AND status = 'scheduled'
            """,
            (str(discord_message_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            return None

        post = dict(row)
        cursor = await db.execute(
            """
            UPDATE posts
            SET status = 'cancelled'
            WHERE id = ?
              AND discord_message_id = ?
              AND status = 'scheduled'
            """,
            (post["id"], str(discord_message_id)),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return None

        cursor = await db.execute(
            """
            INSERT INTO post_cancellations (
                post_id, cancelled_at, scheduled_at
            ) VALUES (?, ?, ?)
            """,
            (post["id"], cancelled_at, post["scheduled_at"]),
        )
        await db.commit()
        post["status"] = "cancelled"
        post["cancellation_id"] = cursor.lastrowid
        post["cancelled_at"] = cancelled_at
        post["cancelled_scheduled_at"] = post["scheduled_at"]
        post["cancellation_archive_message_id"] = None
        post["rescheduled_at"] = None
        post["rescheduled_by"] = None
        return post


async def get_cancellation_with_post(cancellation_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                c.id AS cancellation_id,
                c.cancelled_at,
                c.scheduled_at AS cancelled_scheduled_at,
                c.archive_message_id AS cancellation_archive_message_id,
                c.rescheduled_at,
                c.rescheduled_by
            FROM post_cancellations c
            JOIN posts p ON p.id = c.post_id
            WHERE c.id = ?
            """,
            (cancellation_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_cancellations_pending_archive() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                c.id AS cancellation_id,
                c.cancelled_at,
                c.scheduled_at AS cancelled_scheduled_at,
                c.archive_message_id AS cancellation_archive_message_id,
                c.rescheduled_at,
                c.rescheduled_by
            FROM post_cancellations c
            JOIN posts p ON p.id = c.post_id
            WHERE c.archive_message_id IS NULL
            ORDER BY c.cancelled_at, c.id
            """
        )
        return [dict(row) async for row in cursor]


async def get_open_cancellations_with_archive() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                c.id AS cancellation_id,
                c.cancelled_at,
                c.scheduled_at AS cancelled_scheduled_at,
                c.archive_message_id AS cancellation_archive_message_id,
                c.rescheduled_at,
                c.rescheduled_by
            FROM post_cancellations c
            JOIN posts p ON p.id = c.post_id
            WHERE c.archive_message_id IS NOT NULL
              AND c.rescheduled_at IS NULL
              AND p.status = 'cancelled'
            ORDER BY c.cancelled_at, c.id
            """
        )
        return [dict(row) async for row in cursor]


async def record_cancellation_archive_message(
    cancellation_id: int,
    archive_message_id: str,
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE post_cancellations
            SET archive_message_id = ?
            WHERE id = ?
              AND archive_message_id IS NULL
            """,
            (str(archive_message_id), cancellation_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def reschedule_cancelled_post(
    cancellation_id: int,
    scheduled_at: int,
    rescheduled_by: str,
    rescheduled_at: int,
) -> bool:
    """Restore a cancelled post while preserving its cancellation event."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT c.post_id
            FROM post_cancellations c
            JOIN posts p ON p.id = c.post_id
            WHERE c.id = ?
              AND c.rescheduled_at IS NULL
              AND p.status = 'cancelled'
            """,
            (cancellation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            return False

        post_id = row["post_id"]
        cursor = await db.execute(
            """
            UPDATE posts
            SET status = 'scheduled',
                scheduled_at = ?,
                discord_message_id = ?
            WHERE id = ?
              AND status = 'cancelled'
            """,
            (
                scheduled_at,
                f"rescheduling_{post_id}_{rescheduled_at}",
                post_id,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return False

        cursor = await db.execute(
            """
            UPDATE post_cancellations
            SET rescheduled_at = ?,
                rescheduled_by = ?
            WHERE id = ?
              AND rescheduled_at IS NULL
            """,
            (rescheduled_at, str(rescheduled_by), cancellation_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return False

        # Alert deliveries from the previous time must not suppress alerts for
        # the newly selected schedule.
        await db.execute(
            "DELETE FROM post_alert_deliveries WHERE post_id = ?",
            (post_id,),
        )
        await db.commit()
        return True


async def delete_post_by_id(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        await db.commit()


async def get_post_by_id(post_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_post_by_message_id(discord_message_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE discord_message_id = ?", (str(discord_message_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_scheduled_posts() -> list[dict]:
    """All scheduled posts ordered by scheduled_at DESC (furthest first, soonest last = bottom of chat)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' ORDER BY scheduled_at DESC"
        )
        return [dict(row) async for row in cursor]


async def get_due_posts() -> list[dict]:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE scheduled_at <= ? AND status = 'scheduled'", (now,)
        )
        return [dict(row) async for row in cursor]


async def mark_post_live(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET status = 'live' WHERE id = ?", (post_id,))
        await db.commit()


async def transition_due_post_to_live(
    post_id: int,
    expected_message_id: str,
    live_message_id: str,
    now: int,
) -> bool:
    """Atomically claim one still-due scheduled post for go-live processing."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE posts
            SET status = 'live', discord_message_id = ?
            WHERE id = ?
              AND discord_message_id = ?
              AND status = 'scheduled'
              AND scheduled_at <= ?
            """,
            (
                live_message_id,
                post_id,
                str(expected_message_id),
                now,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_post_content(post_id: int, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET content = ? WHERE id = ?", (content, post_id))
        await db.commit()


async def update_post_scheduled_at(post_id: int, scheduled_at: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET scheduled_at = ? WHERE id = ?", (scheduled_at, post_id))
        await db.commit()


async def update_post_image(post_id: int, image_path: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET image_path = ? WHERE id = ?", (image_path, post_id))
        await db.commit()


async def update_post_tweet_url(post_id: int, tweet_url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET tweet_url = ? WHERE id = ?", (tweet_url, post_id))
        await db.commit()


async def record_x_post_success(
    post_id: int,
    tweet_url: str,
    published_at: int,
    discord_channel_ids: list[int],
    discord_delay_minutes: int,
):
    """Persist X success and enqueue each configured Discord link delivery."""
    due_at = published_at + discord_delay_minutes * 60
    channel_ids = list(dict.fromkeys(str(channel_id) for channel_id in discord_channel_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            """
            UPDATE posts
            SET tweet_url = ?, x_published_at = ?
            WHERE id = ?
            """,
            (tweet_url, published_at, post_id),
        )
        if channel_ids:
            await db.executemany(
                """
                INSERT OR IGNORE INTO post_discord_deliveries (
                    post_id, channel_id, due_at
                ) VALUES (?, ?, ?)
                """,
                [(post_id, channel_id, due_at) for channel_id in channel_ids],
            )
        await db.commit()


async def get_due_discord_deliveries(now: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                d.post_id,
                d.channel_id,
                d.due_at,
                d.attempt_count,
                p.tweet_url
            FROM post_discord_deliveries d
            JOIN posts p ON p.id = d.post_id
            WHERE d.delivered_at IS NULL
              AND d.due_at <= ?
              AND p.status = 'live'
              AND p.tweet_url IS NOT NULL
            ORDER BY d.due_at, d.post_id, d.channel_id
            """,
            (now,),
        )
        return [dict(row) async for row in cursor]


async def record_discord_delivery_success(
    post_id: int,
    channel_id: str,
    delivered_at: int,
    discord_message_id: str | None,
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE post_discord_deliveries
            SET delivered_at = ?,
                discord_message_id = ?,
                last_attempt_at = ?,
                attempt_count = attempt_count + 1
            WHERE post_id = ?
              AND channel_id = ?
              AND delivered_at IS NULL
            """,
            (
                delivered_at,
                discord_message_id,
                delivered_at,
                post_id,
                str(channel_id),
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def record_discord_delivery_failure(
    post_id: int,
    channel_id: str,
    attempted_at: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE post_discord_deliveries
            SET last_attempt_at = ?,
                attempt_count = attempt_count + 1
            WHERE post_id = ?
              AND channel_id = ?
              AND delivered_at IS NULL
            """,
            (attempted_at, post_id, str(channel_id)),
        )
        await db.commit()


async def update_post_skip_unclaimed_pings(post_id: int, skip: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET skip_unclaimed_pings = ? WHERE id = ?",
            (1 if skip else 0, post_id),
        )
        await db.commit()


async def update_scheduled_post_discord_settings(
    post_id: int,
    post_to_discord: bool,
    discord_delay_minutes: int,
) -> bool:
    if discord_delay_minutes < 0:
        raise ValueError("discord_delay_minutes cannot be negative")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE posts
            SET post_to_discord = ?, discord_delay_minutes = ?
            WHERE id = ? AND status = 'scheduled'
            """,
            (
                1 if post_to_discord else 0,
                discord_delay_minutes,
                post_id,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_post_message_id(post_id: int, new_message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE posts SET discord_message_id = ? WHERE id = ?", (new_message_id, post_id))
        await db.commit()


async def get_scheduled_posts_in_range(start: int, end: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE scheduled_at >= ? AND scheduled_at <= ? AND status = 'scheduled' ORDER BY scheduled_at",
            (start, end),
        )
        return [dict(row) async for row in cursor]


# --- Persisted alert delivery dedupe ---

async def was_post_alert_delivered(post_id: int, alert_kind: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT 1
            FROM post_alert_deliveries
            WHERE post_id = ? AND alert_kind = ?
            """,
            (post_id, alert_kind),
        )
        return await cursor.fetchone() is not None


async def record_post_alert_delivery(post_id: int, alert_kind: str) -> bool:
    """Record successful delivery once; False means another writer won."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO post_alert_deliveries (
                post_id, alert_kind, delivered_at
            ) VALUES (?, ?, ?)
            """,
            (post_id, alert_kind, int(time.time())),
        )
        await db.commit()
        return cursor.rowcount == 1


# --- Claims (assigned to) ---

async def add_claim(post_id: int, user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            "INSERT OR IGNORE INTO claims (post_id, user_id, created_at) VALUES (?, ?, ?)",
            (post_id, user_id, int(time.time())),
        )
        await db.execute("DELETE FROM unavailable WHERE post_id = ? AND user_id = ?", (post_id, user_id))
        await db.commit()


async def remove_claim(post_id: int, user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM claims WHERE post_id = ? AND user_id = ?", (post_id, user_id))
        await db.commit()


async def get_claimers_for_post(post_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM claims WHERE post_id = ?", (post_id,))
        return [row[0] async for row in cursor]


# --- Unavailable ---

async def add_unavailable(post_id: int, user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            "INSERT OR IGNORE INTO unavailable (post_id, user_id, created_at) VALUES (?, ?, ?)",
            (post_id, user_id, int(time.time())),
        )
        await db.execute("DELETE FROM claims WHERE post_id = ? AND user_id = ?", (post_id, user_id))
        await db.commit()


async def remove_unavailable(post_id: int, user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM unavailable WHERE post_id = ? AND user_id = ?", (post_id, user_id))
        await db.commit()


async def get_unavailable_for_post(post_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM unavailable WHERE post_id = ?", (post_id,))
        return [row[0] async for row in cursor]


# --- Unclaimed posts ---

async def get_posts_without_claims_in_range(start: int, end: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT p.* FROM posts p
            LEFT JOIN claims c ON p.id = c.post_id
            WHERE p.scheduled_at >= ? AND p.scheduled_at <= ?
              AND p.status = 'scheduled'
            GROUP BY p.id
            HAVING COUNT(c.id) = 0
            ORDER BY p.scheduled_at
            """,
            (start, end),
        )
        return [dict(row) async for row in cursor]


# --- Active / available members ---

async def get_active_user_ids() -> list[str]:
    """Users who claimed, marked unavailable, or created a post in the last 7 days."""
    seven_days_ago = int(time.time()) - 7 * 24 * 60 * 60
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM claims WHERE created_at >= ?
                UNION
                SELECT user_id FROM unavailable WHERE created_at >= ?
                UNION
                SELECT created_by AS user_id FROM posts WHERE created_at >= ?
            )
            """,
            (seven_days_ago, seven_days_ago, seven_days_ago),
        )
        return [row[0] async for row in cursor]


async def get_available_active_user_ids(post_id: int) -> list[str]:
    """Active users who have NOT marked themselves unavailable for this specific post."""
    active = await get_active_user_ids()
    unavailable = set(await get_unavailable_for_post(post_id))
    return [uid for uid in active if uid not in unavailable]
