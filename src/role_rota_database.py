from dataclasses import dataclass
from typing import Optional

import aiosqlite

import database as main_database


ROLES = [
    "Main Scheduler",
    "Content Curators",
    "Sniping & Raid Replies",
    "Research",
    "Live Response",
]

STATUS_ACTIVE = "active"
STATUS_BACKUP = "backup"
VALID_STATUSES = {STATUS_ACTIVE, STATUS_BACKUP}


@dataclass(frozen=True)
class Assignment:
    user_id: int
    role_name: str
    status: str


async def init_role_rota_db() -> None:
    """Create the CTO X role-rota tables in the main bot database."""
    main_database.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cto_team_members (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS cto_assignments (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_name)
            );

            CREATE TABLE IF NOT EXISTS cto_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                rota_message_id INTEGER,
                team_notifications_enabled INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cto_shortage_alert_state (
                guild_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                alerted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, role_name)
            );
            """
        )
        await db.commit()


async def upsert_member(guild_id: int, user_id: int, display_name: str) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO cto_team_members (guild_id, user_id, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET display_name = excluded.display_name
            """,
            (guild_id, user_id, display_name),
        )
        await db.commit()


async def get_member(
    guild_id: int, user_id: int
) -> Optional[tuple[int, str, bool]]:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT user_id, display_name, notifications_enabled
            FROM cto_team_members
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1]), bool(row[2])


async def is_member(guild_id: int, user_id: int) -> bool:
    return await get_member(guild_id, user_id) is not None


async def get_members(guild_id: int) -> list[tuple[int, str, bool]]:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT user_id, display_name, notifications_enabled
            FROM cto_team_members
            WHERE guild_id = ?
            ORDER BY LOWER(display_name), user_id
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return [(int(row[0]), str(row[1]), bool(row[2])) for row in rows]


async def remove_member(guild_id: int, user_id: int) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            "DELETE FROM cto_assignments WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await db.execute(
            "DELETE FROM cto_team_members WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await db.commit()


async def set_assignment(
    guild_id: int,
    user_id: int,
    role_name: str,
    status: str,
) -> None:
    if role_name not in ROLES or status not in VALID_STATUSES:
        raise ValueError("Invalid role or status")

    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO cto_assignments (guild_id, user_id, role_name, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, role_name)
            DO UPDATE SET status = excluded.status
            """,
            (guild_id, user_id, role_name, status),
        )
        await db.commit()


async def remove_assignment(
    guild_id: int,
    user_id: int,
    role_name: str,
) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM cto_assignments
            WHERE guild_id = ? AND user_id = ? AND role_name = ?
            """,
            (guild_id, user_id, role_name),
        )
        await db.commit()


async def get_assignments(
    guild_id: int,
    user_id: Optional[int] = None,
) -> list[Assignment]:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        if user_id is None:
            cursor = await db.execute(
                """
                SELECT user_id, role_name, status
                FROM cto_assignments
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
        else:
            cursor = await db.execute(
                """
                SELECT user_id, role_name, status
                FROM cto_assignments
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
        rows = await cursor.fetchall()
        return [
            Assignment(int(row[0]), str(row[1]), str(row[2]))
            for row in rows
        ]


async def set_notifications(
    guild_id: int,
    user_id: int,
    enabled: bool,
) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            UPDATE cto_team_members
            SET notifications_enabled = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (1 if enabled else 0, guild_id, user_id),
        )
        await db.commit()


async def notifications_enabled(guild_id: int, user_id: int) -> bool:
    member = await get_member(guild_id, user_id)
    return bool(member[2]) if member else False


async def set_rota_message(
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO cto_guild_settings (
                guild_id, channel_id, rota_message_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET channel_id = excluded.channel_id,
                          rota_message_id = excluded.rota_message_id
            """,
            (guild_id, channel_id, message_id),
        )
        await db.commit()


async def get_rota_message(guild_id: int) -> Optional[tuple[int, int]]:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT channel_id, rota_message_id
            FROM cto_guild_settings
            WHERE guild_id = ?
              AND channel_id IS NOT NULL
              AND rota_message_id IS NOT NULL
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        return (int(row[0]), int(row[1])) if row else None


async def set_team_notifications(guild_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO cto_guild_settings (
                guild_id, team_notifications_enabled
            )
            VALUES (?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                team_notifications_enabled = excluded.team_notifications_enabled
            """,
            (guild_id, 1 if enabled else 0),
        )
        if not enabled:
            await db.execute(
                """
                UPDATE cto_shortage_alert_state
                SET alerted = 0
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
        await db.commit()


async def team_notifications_enabled(guild_id: int) -> bool:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT team_notifications_enabled
            FROM cto_guild_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row else False


async def get_shortage_alerted(guild_id: int, role_name: str) -> bool:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT alerted
            FROM cto_shortage_alert_state
            WHERE guild_id = ? AND role_name = ?
            """,
            (guild_id, role_name),
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row else False


async def set_shortage_alerted(
    guild_id: int,
    role_name: str,
    alerted: bool,
) -> None:
    async with aiosqlite.connect(main_database.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO cto_shortage_alert_state (
                guild_id, role_name, alerted
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, role_name)
            DO UPDATE SET alerted = excluded.alerted
            """,
            (guild_id, role_name, 1 if alerted else 0),
        )
        await db.commit()
