import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import database
import role_rota_database as rota_db
from cogs import role_rota
from role_rota_image import render_rota_image


class RoleRotaDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "rota.db"
        await rota_db.init_role_rota_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_team_settings_can_exist_before_rota_panel(self):
        await rota_db.set_team_notifications(1, True)

        self.assertTrue(await rota_db.team_notifications_enabled(1))
        self.assertIsNone(await rota_db.get_rota_message(1))

        await rota_db.set_rota_message(1, 10, 20)
        self.assertEqual(await rota_db.get_rota_message(1), (10, 20))

    async def test_disabling_team_alerts_rearms_shortage_state(self):
        await rota_db.set_team_notifications(1, True)
        await rota_db.set_shortage_alerted(1, "Main Scheduler", True)

        await rota_db.set_team_notifications(1, False)

        self.assertFalse(await rota_db.team_notifications_enabled(1))
        self.assertFalse(
            await rota_db.get_shortage_alerted(1, "Main Scheduler")
        )


class RoleRotaNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "rota.db"
        await rota_db.init_role_rota_db()
        role_rota._notification_locks.clear()

        await rota_db.upsert_member(1, 101, "Mentioned member")
        await rota_db.upsert_member(1, 202, "Quiet member")
        await rota_db.set_notifications(1, 202, False)
        await rota_db.set_team_notifications(1, True)
        await rota_db.set_assignment(
            1,
            101,
            "Sniping & Raid Replies",
            rota_db.STATUS_ACTIVE,
        )
        await rota_db.set_assignment(
            1,
            101,
            "Live Response",
            rota_db.STATUS_ACTIVE,
        )

        self.channel = SimpleNamespace(send=AsyncMock())
        self.bot = Mock()
        self.bot.get_channel.return_value = self.channel
        self.bot.fetch_channel = AsyncMock()

    async def asyncTearDown(self):
        role_rota._notification_locks.clear()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_shortage_alert_posts_once_with_opted_in_mentions(self):
        await role_rota.send_shortage_notifications(self.bot, 1)
        await role_rota.send_shortage_notifications(self.bot, 1)

        self.channel.send.assert_awaited_once()
        content = self.channel.send.await_args.args[0]
        allowed = self.channel.send.await_args.kwargs["allowed_mentions"]
        self.assertIn("Main Scheduler", content)
        self.assertIn("<@101>", content)
        self.assertNotIn("<@202>", content)
        self.assertEqual(allowed.to_dict(), {"users": [101], "parse": []})
        self.bot.fetch_channel.assert_not_awaited()

    async def test_recovered_shortage_can_alert_again(self):
        await role_rota.send_shortage_notifications(self.bot, 1)
        await rota_db.set_assignment(
            1,
            101,
            "Main Scheduler",
            rota_db.STATUS_ACTIVE,
        )
        await role_rota.send_shortage_notifications(self.bot, 1)
        await rota_db.set_assignment(
            1,
            101,
            "Main Scheduler",
            rota_db.STATUS_BACKUP,
        )
        await role_rota.send_shortage_notifications(self.bot, 1)

        self.assertEqual(self.channel.send.await_count, 2)

    async def test_failed_delivery_is_not_marked_as_alerted(self):
        with patch.object(
            role_rota,
            "_send_shortage_alert",
            new=AsyncMock(return_value=False),
        ):
            await role_rota.send_shortage_notifications(self.bot, 1)

        self.assertFalse(
            await rota_db.get_shortage_alerted(1, "Main Scheduler")
        )

    async def test_alert_without_opted_in_members_still_posts(self):
        await rota_db.set_notifications(1, 101, False)

        await role_rota.send_shortage_notifications(self.bot, 1)

        self.channel.send.assert_awaited_once()
        self.assertNotIn("<@", self.channel.send.await_args.args[0])
        allowed = self.channel.send.await_args.kwargs["allowed_mentions"]
        self.assertEqual(allowed.to_dict(), {"parse": []})


class RoleRotaCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_load_initializes_database_and_persistent_view(self):
        bot = Mock()
        bot.add_view = Mock()
        cog = role_rota.RoleRotaCog(bot)

        with patch.object(
            role_rota.rota_db,
            "init_role_rota_db",
            new=AsyncMock(),
        ) as init_db:
            await cog.cog_load()

        init_db.assert_awaited_once()
        bot.add_view.assert_called_once()
        self.assertIsInstance(
            bot.add_view.call_args.args[0],
            role_rota.MainRotaView,
        )

    def test_empty_rota_renders_as_png(self):
        output = render_rota_image([], [])

        self.assertEqual(output.read(8), b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
