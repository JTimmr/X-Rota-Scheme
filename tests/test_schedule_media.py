import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cogs import schedule


def video_attachment(filename: str = "clip.mp4", content_type: str | None = None):
    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        size=1024,
    )


def command_interaction():
    return SimpleNamespace(
        channel_id=schedule.SCHEDULED_CHANNEL_ID,
        user=SimpleNamespace(id=123),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
        ),
    )


class DiscordMediaEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_composer_accepts_and_rejects_video_via_shared_validator(self):
        valid = video_attachment()
        modal = schedule.SchedulePostModal(Mock(), user_id=123)
        modal.content_input._value = "Video post"
        modal.file_upload._values = [valid]
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        picker = Mock()
        picker._status_text.return_value = "Pick time"

        with (
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(return_value=("data/images/video.mp4", None)),
            ) as save_media,
            patch.object(schedule, "ScheduleView", return_value=picker),
        ):
            await modal.on_submit(interaction)

        save_media.assert_awaited_once_with(valid)
        interaction.followup.send.assert_awaited_once()

        rejected = video_attachment(filename="clip.mov", content_type="video/mp4")
        modal = schedule.SchedulePostModal(Mock(), user_id=123)
        modal.content_input._value = "Bad video"
        modal.file_upload._values = [rejected]
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with (
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(
                    return_value=(None, schedule.SUPPORTED_IMAGE_ERROR)
                ),
            ),
            patch.object(schedule, "ScheduleView") as schedule_view,
        ):
            await modal.on_submit(interaction)
        self.assertIn(
            "not supported",
            interaction.followup.send.await_args.args[0],
        )
        schedule_view.assert_not_called()

    async def test_schedule_command_accepts_mp4_and_rejects_non_mp4(self):
        cog = schedule.ScheduleCog(Mock())
        accepted = command_interaction()
        await schedule.ScheduleCog.schedule.callback(
            cog,
            accepted,
            media=video_attachment(content_type="video/mp4"),
            post_to_x=True,
        )
        accepted.response.send_modal.assert_awaited_once()
        accepted.response.send_message.assert_not_awaited()

        rejected = command_interaction()
        await schedule.ScheduleCog.schedule.callback(
            cog,
            rejected,
            media=video_attachment(
                filename="clip.mov",
                content_type="video/quicktime",
            ),
            post_to_x=True,
        )
        rejected.response.send_modal.assert_not_awaited()
        rejected.response.send_message.assert_awaited_once()
        self.assertTrue(
            rejected.response.send_message.await_args.kwargs["ephemeral"]
        )

    async def test_schedule_command_forwards_discord_delivery_settings(self):
        cog = schedule.ScheduleCog(Mock())
        interaction = command_interaction()

        await schedule.ScheduleCog.schedule.callback(
            cog,
            interaction,
            media=None,
            post_to_x=True,
            post_to_discord=False,
            discord_delay_minutes=60,
        )

        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, schedule.PostContentModal)
        self.assertFalse(modal.post_to_discord)
        self.assertEqual(modal.discord_delay_minutes, 60)

    async def test_updatemedia_accepts_mp4_and_rejects_non_mp4(self):
        cog = schedule.ScheduleCog(Mock())
        posts = [
            {
                "id": 1,
                "content": "Post",
                "scheduled_at": 100,
                "image_path": None,
            }
        ]
        accepted = command_interaction()
        with patch.object(
            schedule,
            "get_all_scheduled_posts",
            new=AsyncMock(return_value=posts),
        ):
            await schedule.ScheduleCog.updatemedia.callback(
                cog,
                accepted,
                media=video_attachment(content_type=None),
            )
        accepted.response.send_message.assert_awaited_once()
        call = accepted.response.send_message.await_args
        self.assertIsInstance(call.kwargs["view"], schedule.MediaPostPicker)
        self.assertIn("media", call.args[0])

        rejected = command_interaction()
        with patch.object(
            schedule,
            "get_all_scheduled_posts",
            new=AsyncMock(),
        ) as get_posts:
            await schedule.ScheduleCog.updatemedia.callback(
                cog,
                rejected,
                media=video_attachment(
                    filename="clip.avi",
                    content_type="video/x-msvideo",
                ),
            )
        rejected.response.send_message.assert_awaited_once()
        get_posts.assert_not_awaited()

    async def test_updatemedia_picker_validates_before_replacing(self):
        attachment = video_attachment(content_type="video/mp4")
        post = {
            "id": 1,
            "content": "Post",
            "scheduled_at": 100,
            "image_path": None,
        }
        interaction = SimpleNamespace(
            data={"values": ["1"]},
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        update = AsyncMock()
        with (
            patch.object(
                schedule,
                "get_post_by_id",
                new=AsyncMock(return_value=post),
            ),
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(
                    return_value=("data/images/new-video.mp4", None)
                ),
            ),
            patch.object(schedule, "update_post_image", new=update),
            patch.object(
                schedule,
                "repost_all_scheduled",
                new=AsyncMock(),
            ),
        ):
            await schedule.MediaPostPicker(
                Mock(),
                [post],
                attachment,
            )._on_select(interaction)
        update.assert_awaited_once_with(1, "data/images/new-video.mp4")

        with tempfile.TemporaryDirectory() as temp_dir:
            old_media = Path(temp_dir) / "old.png"
            old_media.write_bytes(b"old media")
            post_with_media = {**post, "image_path": str(old_media)}
            interaction = SimpleNamespace(
                data={"values": ["1"]},
                response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock(),
            )
            update = AsyncMock()
            with (
                patch.object(
                    schedule,
                    "get_post_by_id",
                    new=AsyncMock(return_value=post_with_media),
                ),
                patch.object(
                    schedule,
                    "save_validated_media_attachment",
                    new=AsyncMock(
                        return_value=(None, "MP4 video codec must be H.264.")
                    ),
                ),
                patch.object(schedule, "update_post_image", new=update),
            ):
                await schedule.MediaPostPicker(
                    Mock(),
                    [post_with_media],
                    attachment,
                )._on_select(interaction)
            update.assert_not_awaited()
            self.assertTrue(old_media.is_file())
            self.assertIn(
                "H.264",
                interaction.edit_original_response.await_args.kwargs[
                    "content"
                ],
            )


class MediaPickerPaginationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def posts(count: int) -> list[dict]:
        return [
            {
                "id": index + 1,
                "content": f"Post {index + 1}",
                "scheduled_at": 100 + index,
                "image_path": None,
            }
            for index in range(count)
        ]

    async def test_zero_posts_is_handled_without_a_picker(self):
        interaction = command_interaction()
        with patch.object(
            schedule,
            "get_all_scheduled_posts",
            new=AsyncMock(return_value=[]),
        ):
            await schedule.ScheduleCog.updatemedia.callback(
                schedule.ScheduleCog(Mock()),
                interaction,
                media=video_attachment(content_type="video/mp4"),
            )

        call = interaction.response.send_message.await_args
        self.assertIn("No scheduled posts", call.args[0])
        self.assertNotIn("view", call.kwargs)

    def test_one_and_twenty_five_posts_fit_one_page(self):
        attachment = video_attachment(content_type="video/mp4")
        for count in (1, 25):
            with self.subTest(count=count):
                picker = schedule.MediaPostPicker(
                    Mock(),
                    self.posts(count),
                    attachment,
                )
                self.assertEqual(picker.page_count, 1)
                self.assertEqual(len(picker.children), 1)
                self.assertEqual(len(picker.children[0].options), count)
                self.assertIs(picker.attachment, attachment)
                self.assertIn(f"posts 1-{count} of {count}", picker.status_text())

    async def test_twenty_six_posts_navigate_to_single_item_page(self):
        attachment = video_attachment(content_type="video/mp4")
        picker = schedule.MediaPostPicker(
            Mock(),
            self.posts(26),
            attachment,
        )
        self.assertEqual(len(picker.children[0].options), 25)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )

        await picker._on_next(interaction)

        self.assertEqual(picker.page_index, 1)
        self.assertEqual(len(picker.children[0].options), 1)
        self.assertEqual(picker.children[0].options[0].value, "26")
        self.assertIn("Page **2/2**", picker.status_text())
        self.assertIs(picker.attachment, attachment)

    async def test_sixty_posts_navigate_all_three_pages(self):
        picker = schedule.MediaPostPicker(
            Mock(),
            self.posts(60),
            video_attachment(content_type="video/mp4"),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )

        await picker._on_next(interaction)
        self.assertEqual(picker.page_index, 1)
        self.assertEqual(len(picker.children[0].options), 25)
        self.assertEqual(picker.children[0].options[0].value, "26")

        await picker._on_next(interaction)
        self.assertEqual(picker.page_index, 2)
        self.assertEqual(len(picker.children[0].options), 10)
        self.assertEqual(picker.children[0].options[0].value, "51")
        self.assertTrue(picker.children[2].disabled)

        await picker._on_previous(interaction)
        self.assertEqual(picker.page_index, 1)
        self.assertIn("Page **2/3**", picker.status_text())


class ScheduledEditMentionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_unavailable_and_toggle_edits_disable_all_mentions(self):
        post = {
            "id": 1,
            "content": "<@123> <@&456> @everyone",
            "scheduled_at": 100,
            "created_by": "789",
            "skip_unclaimed_pings": 1,
            "post_to_x": 1,
        }
        actions = ("claim", "unavailable", "toggle")

        for action in actions:
            with self.subTest(action=action):
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=999),
                    response=SimpleNamespace(
                        edit_message=AsyncMock(),
                        send_message=AsyncMock(),
                    ),
                )
                view = schedule.PostButtonView(Mock(), post_id=1)
                with (
                    patch.object(
                        schedule,
                        "get_post_by_id",
                        new=AsyncMock(return_value=post),
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
                        "add_claim",
                        new=AsyncMock(),
                    ),
                    patch.object(
                        schedule,
                        "add_unavailable",
                        new=AsyncMock(),
                    ),
                    patch.object(
                        schedule,
                        "update_post_skip_unclaimed_pings",
                        new=AsyncMock(),
                    ),
                ):
                    if action == "claim":
                        await view._on_claim(interaction)
                    elif action == "unavailable":
                        await view._on_unavailable(interaction)
                    else:
                        await view._on_toggle_skip_unclaimed_pings(
                            interaction
                        )

                edit_call = interaction.response.edit_message.await_args
                self.assertEqual(
                    edit_call.kwargs["embed"].description,
                    "<@123> <@&456> @everyone",
                )
                self.assertEqual(
                    edit_call.kwargs["allowed_mentions"].to_dict(),
                    discord.AllowedMentions.none().to_dict(),
                )

    async def test_scheduled_post_can_toggle_and_delay_discord_links(self):
        bot = Mock()
        off_view = schedule.PostButtonView(
            bot,
            post_id=1,
            post_to_discord=False,
            discord_delay_minutes=60,
        )
        discord_button = next(
            item
            for item in off_view.children
            if isinstance(item, discord.ui.Button)
            and item.custom_id == "postdiscord:1"
        )
        delay_button = next(
            item
            for item in off_view.children
            if isinstance(item, discord.ui.Button)
            and item.custom_id == "changedelay:1"
        )
        cancel_button = next(
            item
            for item in off_view.children
            if isinstance(item, discord.ui.Button)
            and item.custom_id == "cancelpost:1"
        )
        self.assertIn("off", discord_button.label)
        self.assertTrue(delay_button.disabled)
        self.assertEqual(delay_button.label, "Change delay")
        self.assertEqual(cancel_button.label, "Cancel post")

        post = {
            "id": 1,
            "content": "Delayed",
            "scheduled_at": 100,
            "created_by": "789",
            "status": "scheduled",
            "skip_unclaimed_pings": 1,
            "post_to_x": 1,
            "post_to_discord": 1,
            "discord_delay_minutes": 0,
        }
        delayed = {**post, "discord_delay_minutes": 60}
        schedule_message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(
            data={"values": ["60"]},
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
            message=SimpleNamespace(),
        )
        update = AsyncMock(return_value=True)
        with (
            patch.object(
                schedule,
                "get_post_by_id",
                new=AsyncMock(side_effect=[post, delayed]),
            ),
            patch.object(
                schedule,
                "update_scheduled_post_discord_settings",
                new=update,
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
        ):
            await schedule.DiscordDelayPickerView(
                bot,
                post_id=1,
                user_id=999,
                schedule_message=schedule_message,
                current_delay_minutes=0,
            )._on_select(interaction)

        update.assert_awaited_once_with(1, True, 60)
        edited = schedule_message.edit.await_args
        self.assertIn("1 hour", edited.kwargs["content"])


if __name__ == "__main__":
    unittest.main()
