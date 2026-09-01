import importlib
import os
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

x_client = types.ModuleType("x_client")
x_client.X_POST_FAILED = "failed"
x_client.X_POST_SUCCESS = "success"
x_client.X_POST_UNKNOWN = "unknown"


@dataclass(frozen=True)
class XPostResult:
    status: str
    url: str | None = None
    detail: str | None = None


x_client.XPostResult = XPostResult
x_client.post_tweet_result = Mock()
sys.modules.setdefault("x_client", x_client)

# scheduler imports x_client, so the stub must be installed before this import.
scheduler = importlib.import_module("cogs.scheduler")


class SchedulerTargetTests(unittest.IsolatedAsyncioTestCase):
    def test_user_mentions_allow_only_targeted_users(self):
        mentions, allowed = scheduler._user_alert_target(["123", "456"])

        self.assertEqual(mentions, "<@123> <@456>")
        self.assertEqual(allowed.to_dict(), {"users": [123, 456], "parse": []})

    async def test_configured_role_is_the_only_allowed_team_mention(self):
        role = SimpleNamespace(id=789, mention="<@&789>")
        guild = Mock()
        guild.get_role.return_value = role
        bot = Mock()
        bot.get_guild.return_value = guild
        cog = scheduler.SchedulerCog(bot)

        with (
            patch.object(scheduler, "ROTA_ALERT_ROLE_ID", 789),
            patch.object(
                scheduler, "get_available_active_user_ids", new=AsyncMock()
            ) as active_users,
        ):
            mentions, allowed = await cog._team_alert_target(post_id=1)

        self.assertEqual(mentions, "<@&789>")
        self.assertEqual(allowed.to_dict(), {"roles": [789], "parse": []})
        active_users.assert_not_awaited()

    async def test_unresolved_role_warns_and_falls_back_to_active_users(self):
        guild = Mock()
        guild.get_role.return_value = None
        bot = Mock()
        bot.get_guild.return_value = guild
        cog = scheduler.SchedulerCog(bot)

        with (
            patch.object(scheduler, "ROTA_ALERT_ROLE_ID", 789),
            patch.object(
                scheduler,
                "get_available_active_user_ids",
                new=AsyncMock(return_value=["123"]),
            ),
            self.assertLogs("rota-bot.scheduler", level="WARNING") as logs,
        ):
            mentions, allowed = await cog._team_alert_target(post_id=1)

        self.assertEqual(mentions, "<@123>")
        self.assertEqual(allowed.to_dict(), {"users": [123], "parse": []})
        self.assertIn("falling back to active-user alerts", logs.output[0])

    async def test_no_target_is_not_recorded_and_later_role_can_notify(self):
        post = {
            "id": 1,
            "content": "Post",
            "scheduled_at": 100,
            "discord_message_id": "10",
            "skip_unclaimed_pings": 0,
            "post_to_x": 1,
        }
        reminders = SimpleNamespace(send=AsyncMock())
        guild = Mock()
        role = SimpleNamespace(id=789, mention="<@&789>")
        guild.get_role.return_value = role
        bot = Mock()
        bot.get_channel.return_value = reminders
        bot.get_guild.return_value = guild
        cog = scheduler.SchedulerCog(bot)
        record = AsyncMock()

        with (
            patch.object(
                scheduler,
                "get_posts_without_claims_in_range",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                scheduler,
                "was_post_alert_delivered",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                scheduler,
                "get_available_active_user_ids",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                scheduler,
                "record_post_alert_delivery",
                new=record,
            ),
            patch.object(scheduler, "ROTA_ALERT_ROLE_ID", None),
        ):
            await cog._check_unassigned_posts()

        reminders.send.assert_not_awaited()
        record.assert_not_awaited()

        with (
            patch.object(
                scheduler,
                "get_posts_without_claims_in_range",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                scheduler,
                "was_post_alert_delivered",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                scheduler,
                "record_post_alert_delivery",
                new=record,
            ),
            patch.object(scheduler, "ROTA_ALERT_ROLE_ID", 789),
        ):
            await cog._check_unassigned_posts()

        reminders.send.assert_awaited_once()
        record.assert_awaited_once_with(
            1,
            scheduler.ALERT_KIND_FOUR_HOUR,
        )

    async def test_failed_discord_send_is_not_recorded(self):
        post = {
            "id": 1,
            "content": "Post",
            "scheduled_at": 100,
            "discord_message_id": "10",
            "skip_unclaimed_pings": 0,
            "post_to_x": 1,
        }
        reminders = SimpleNamespace(
            send=AsyncMock(side_effect=RuntimeError("send failed"))
        )
        bot = Mock()
        bot.get_channel.return_value = reminders
        record = AsyncMock()
        with (
            patch.object(
                scheduler,
                "get_posts_without_claims_in_range",
                new=AsyncMock(return_value=[post]),
            ),
            patch.object(
                scheduler,
                "was_post_alert_delivered",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                scheduler,
                "get_available_active_user_ids",
                new=AsyncMock(return_value=["123"]),
            ),
            patch.object(
                scheduler,
                "record_post_alert_delivery",
                new=record,
            ),
            patch.object(scheduler, "ROTA_ALERT_ROLE_ID", None),
        ):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await scheduler.SchedulerCog(bot)._check_unassigned_posts()

        record.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
