import aiosqlite
import time
from pathlib import Path

DB_PATH = Path("/app/data/rota.db")


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                scheduled_at INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                reacted_at INTEGER NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                UNIQUE(post_id, user_id)
            )
        """)
        await db.commit()


async def insert_post(discord_message_id: str, content: str, scheduled_at: int, created_by: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO posts (discord_message_id, content, scheduled_at, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (discord_message_id, content, scheduled_at, created_by, int(time.time())),
        )
        await db.commit()
        return cursor.lastrowid


async def delete_post_by_message_id(discord_message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM posts WHERE discord_message_id = ?", (str(discord_message_id),))
        await db.commit()


async def get_post_by_message_id(discord_message_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE discord_message_id = ?", (str(discord_message_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_due_posts() -> list[dict]:
    """Posts where scheduled_at <= now and status is still 'scheduled'."""
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


async def get_scheduled_posts_in_range(start: int, end: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE scheduled_at >= ? AND scheduled_at <= ? AND status = 'scheduled' ORDER BY scheduled_at",
            (start, end),
        )
        return [dict(row) async for row in cursor]


async def add_reaction(post_id: int, user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reactions (post_id, user_id, reacted_at) VALUES (?, ?, ?)",
            (post_id, user_id, int(time.time())),
        )
        await db.commit()


async def remove_reaction(post_id: int, user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reactions WHERE post_id = ? AND user_id = ?", (post_id, user_id)
        )
        await db.commit()


async def get_reactions_for_post(post_id: int) -> list[str]:
    """Returns list of user IDs who reacted to a post."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM reactions WHERE post_id = ?", (post_id,)
        )
        return [row[0] async for row in cursor]


async def get_posts_without_reactions_in_range(start: int, end: int) -> list[dict]:
    """Scheduled posts in the time range that have zero reactions."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT p.* FROM posts p
            LEFT JOIN reactions r ON p.id = r.post_id
            WHERE p.scheduled_at >= ? AND p.scheduled_at <= ?
              AND p.status = 'scheduled'
            GROUP BY p.id
            HAVING COUNT(r.id) = 0
            ORDER BY p.scheduled_at
            """,
            (start, end),
        )
        return [dict(row) async for row in cursor]


async def get_active_user_ids() -> list[str]:
    """Users who reacted to a scheduled post in the last 7 days or created a post in the last 7 days."""
    seven_days_ago = int(time.time()) - 7 * 24 * 60 * 60
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT user_id FROM (
                SELECT r.user_id FROM reactions r
                JOIN posts p ON r.post_id = p.id
                WHERE r.reacted_at >= ?
                UNION
                SELECT created_by AS user_id FROM posts
                WHERE created_at >= ?
            )
            """,
            (seven_days_ago, seven_days_ago),
        )
        return [row[0] async for row in cursor]
