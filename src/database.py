import aiosqlite
import time
from pathlib import Path

DB_PATH = Path("/app/data/rota.db")
IMAGES_DIR = Path("/app/data/images")


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
                image_path TEXT
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


async def insert_post(discord_message_id: str, content: str, scheduled_at: int, created_by: str, image_path: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO posts (discord_message_id, content, scheduled_at, created_by, created_at, image_path) VALUES (?, ?, ?, ?, ?, ?)",
            (discord_message_id, content, scheduled_at, created_by, int(time.time()), image_path),
        )
        await db.commit()
        return cursor.lastrowid


async def delete_post_by_message_id(discord_message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM posts WHERE discord_message_id = ?", (str(discord_message_id),))
        await db.commit()


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


async def update_post_skip_unclaimed_pings(post_id: int, skip: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET skip_unclaimed_pings = ? WHERE id = ?",
            (1 if skip else 0, post_id),
        )
        await db.commit()


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
