import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from PIL import Image

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cogs import schedule


class TrackedMessage:
    def __init__(self, channel, message_id, author, content, send_kwargs=None):
        self.channel = channel
        self.id = message_id
        self.author = author
        self.content = content
        self.send_kwargs = send_kwargs or {}

    async def delete(self):
        if self in self.channel.messages:
            self.channel.messages.remove(self)


class TrackingChannel:
    def __init__(self, bot_user, fail_once_on_send=None):
        self.id = schedule.SCHEDULED_CHANNEL_ID
        self.bot_user = bot_user
        self.messages = []
        self.send_count = 0
        self.fail_once_on_send = fail_once_on_send

    def history(self, *, limit):
        async def messages():
            for message in list(self.messages)[:limit]:
                yield message

        return messages()

    async def send(self, content, **kwargs):
        self.send_count += 1
        await asyncio.sleep(0)
        if self.fail_once_on_send == self.send_count:
            raise RuntimeError("transient send failure")
        message = TrackedMessage(
            self,
            1000 + self.send_count,
            self.bot_user,
            content,
            kwargs,
        )
        self.messages.append(message)
        return message


def scheduled_posts():
    return [
        {
            "id": 1,
            "content": "Later post",
            "scheduled_at": 200,
            "created_by": "1",
            "image_path": None,
            "skip_unclaimed_pings": 1,
            "post_to_x": 1,
        },
        {
            "id": 2,
            "content": "Sooner post",
            "scheduled_at": 100,
            "created_by": "2",
            "image_path": None,
            "skip_unclaimed_pings": 1,
            "post_to_x": 1,
        },
    ]


class SchedulePanelTests(unittest.IsolatedAsyncioTestCase):
    def test_panel_view_is_stable_and_persistent(self):
        view = schedule.SchedulePanelView(Mock())

        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.children), 1)
        button = view.children[0]
        self.assertIsInstance(button, discord.ui.Button)
        self.assertEqual(button.label, "Schedule post")
        self.assertEqual(button.custom_id, schedule.SCHEDULE_PANEL_CUSTOM_ID)

    async def test_cog_load_registers_persistent_panel_view(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)

        with patch.object(
            schedule,
            "get_open_cancellations_with_archive",
            new=AsyncMock(return_value=[]),
        ):
            await cog.cog_load()

        bot.add_view.assert_called_once()
        registered_view = bot.add_view.call_args.args[0]
        self.assertIsInstance(registered_view, schedule.SchedulePanelView)
        self.assertTrue(registered_view.is_persistent())

    async def test_cog_load_restores_open_cancellation_reschedule_button(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)
        cancellation = {
            "cancellation_id": 12,
            "cancellation_archive_message_id": "700",
        }

        with patch.object(
            schedule,
            "get_open_cancellations_with_archive",
            new=AsyncMock(return_value=[cancellation]),
        ):
            await cog.cog_load()

        self.assertEqual(bot.add_view.call_count, 2)
        restored_call = bot.add_view.call_args_list[1]
        self.assertIsInstance(
            restored_call.args[0],
            schedule.CancelledPostView,
        )
        self.assertEqual(restored_call.kwargs["message_id"], 700)

    async def test_panel_rejects_interactions_outside_scheduled_channel(self):
        interaction = SimpleNamespace(
            channel_id=schedule.SCHEDULED_CHANNEL_ID + 1,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        view = schedule.SchedulePanelView(Mock())

        allowed = await view.interaction_check(interaction)

        self.assertFalse(allowed)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_send_panel_uses_no_mentions_and_refuses_other_channels(self):
        bot = Mock()
        sent_message = object()
        channel = SimpleNamespace(
            id=schedule.SCHEDULED_CHANNEL_ID,
            send=AsyncMock(return_value=sent_message),
        )

        result = await schedule.send_schedule_panel(channel, bot)

        self.assertIs(result, sent_message)
        channel.send.assert_awaited_once()
        send_call = channel.send.await_args
        self.assertEqual(send_call.args[0], schedule.SCHEDULE_PANEL_TEXT)
        self.assertEqual(
            send_call.kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        self.assertIsInstance(send_call.kwargs["view"], schedule.SchedulePanelView)

        wrong_channel = SimpleNamespace(
            id=schedule.SCHEDULED_CHANNEL_ID + 1,
            send=AsyncMock(),
        )
        with self.assertLogs("rota-bot.schedule", level="WARNING"):
            self.assertIsNone(await schedule.send_schedule_panel(wrong_channel, bot))
        wrong_channel.send.assert_not_awaited()

    async def test_refresh_deletes_old_bot_messages_and_sends_one_panel_last(self):
        bot_user = object()
        old_post = SimpleNamespace(
            id=10, author=bot_user, delete=AsyncMock()
        )
        old_panel = SimpleNamespace(
            id=11, author=bot_user, delete=AsyncMock()
        )
        human_message = SimpleNamespace(
            id=12, author=object(), delete=AsyncMock()
        )

        async def history():
            for message in (old_post, old_panel, human_message):
                yield message

        sent_messages = [
            SimpleNamespace(id=101),
            SimpleNamespace(id=102),
            SimpleNamespace(id=103),
        ]
        channel = SimpleNamespace(
            id=schedule.SCHEDULED_CHANNEL_ID,
            history=lambda *, limit: history(),
            send=AsyncMock(side_effect=sent_messages),
        )
        bot = Mock()
        bot.user = bot_user
        bot.get_channel.return_value = channel
        posts = scheduled_posts()

        with (
            patch.object(
                schedule,
                "get_all_scheduled_posts",
                new=AsyncMock(return_value=posts),
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
                new=AsyncMock(),
            ),
        ):
            result = await schedule.repost_all_scheduled(bot)

        self.assertTrue(result)
        old_post.delete.assert_awaited_once()
        old_panel.delete.assert_awaited_once()
        human_message.delete.assert_not_awaited()
        self.assertEqual(channel.send.await_count, 3)
        send_calls = channel.send.await_args_list
        self.assertEqual(
            send_calls[0].kwargs["embed"].description,
            "Later post",
        )
        self.assertEqual(
            send_calls[1].kwargs["embed"].description,
            "Sooner post",
        )
        self.assertEqual(send_calls[2].args[0], schedule.SCHEDULE_PANEL_TEXT)
        self.assertEqual(
            sum(
                call.args[0] == schedule.SCHEDULE_PANEL_TEXT for call in send_calls
            ),
            1,
        )

    async def test_simultaneous_refreshes_leave_one_complete_schedule(self):
        bot_user = object()
        channel = TrackingChannel(bot_user)
        bot = Mock()
        bot.user = bot_user
        bot.get_channel.return_value = channel
        posts = scheduled_posts()

        async def query_posts():
            await asyncio.sleep(0)
            return posts

        with (
            patch.object(
                schedule,
                "get_all_scheduled_posts",
                new=AsyncMock(side_effect=query_posts),
            ) as get_posts,
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
                new=AsyncMock(),
            ),
        ):
            results = await asyncio.gather(
                schedule.repost_all_scheduled(bot),
                schedule.repost_all_scheduled(bot),
            )

        self.assertEqual(results, [True, True])
        self.assertEqual(get_posts.await_count, 2)
        contents = [message.content for message in channel.messages]
        embed_contents = [
            message.send_kwargs["embed"].description
            for message in channel.messages
            if "embed" in message.send_kwargs
        ]
        self.assertEqual(
            embed_contents.count("Later post"),
            1,
        )
        self.assertEqual(
            embed_contents.count("Sooner post"),
            1,
        )
        self.assertEqual(contents.count(schedule.SCHEDULE_PANEL_TEXT), 1)
        self.assertEqual(contents[-1], schedule.SCHEDULE_PANEL_TEXT)

    async def test_partial_failure_is_recoverable_on_later_refresh(self):
        bot_user = object()
        channel = TrackingChannel(bot_user, fail_once_on_send=2)
        bot = Mock()
        bot.user = bot_user
        bot.get_channel.return_value = channel
        update_message_id = AsyncMock()

        with (
            patch.object(
                schedule,
                "get_all_scheduled_posts",
                new=AsyncMock(return_value=scheduled_posts()),
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
                new=update_message_id,
            ),
            self.assertLogs("rota-bot.schedule", level="ERROR") as logs,
        ):
            first_result = await schedule.repost_all_scheduled(bot)
            second_result = await schedule.repost_all_scheduled(bot)

        self.assertFalse(first_result)
        self.assertTrue(second_result)
        self.assertIn("later refresh will rebuild", "\n".join(logs.output))
        contents = [message.content for message in channel.messages]
        embed_contents = [
            message.send_kwargs["embed"].description
            for message in channel.messages
            if "embed" in message.send_kwargs
        ]
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[-1], schedule.SCHEDULE_PANEL_TEXT)
        self.assertEqual(
            embed_contents.count("Later post"),
            1,
        )
        self.assertEqual(
            embed_contents.count("Sooner post"),
            1,
        )
        update_calls = [
            call.args for call in update_message_id.await_args_list
        ]
        self.assertIn((1, "reposting_1"), update_calls)
        self.assertIn((2, "reposting_2"), update_calls)
        self.assertTrue(
            any(post_id == 1 and message_id.isdigit()
                for post_id, message_id in update_calls)
        )
        self.assertTrue(
            any(post_id == 2 and message_id.isdigit()
                for post_id, message_id in update_calls)
        )

    async def test_startup_retries_once_and_only_marks_success(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)

        with (
            patch.object(
                schedule,
                "repost_all_scheduled",
                new=AsyncMock(side_effect=[False, True]),
            ) as refresh,
            patch.object(
                schedule.asyncio,
                "sleep",
                new=AsyncMock(),
            ) as sleep,
            patch.object(
                schedule,
                "archive_pending_cancellations",
                new=AsyncMock(return_value=True),
            ) as archive_pending,
        ):
            await cog.on_ready()

        self.assertTrue(cog._startup_done)
        self.assertEqual(refresh.await_count, 2)
        sleep.assert_awaited_once_with(schedule.STARTUP_REFRESH_RETRY_SECONDS)
        archive_pending.assert_awaited_once_with(bot)

    async def test_startup_failure_stays_retryable_without_tight_loop(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)

        with (
            patch.object(
                schedule,
                "repost_all_scheduled",
                new=AsyncMock(return_value=False),
            ) as refresh,
            patch.object(
                schedule.asyncio,
                "sleep",
                new=AsyncMock(),
            ) as sleep,
            self.assertLogs("rota-bot.schedule", level="ERROR"),
        ):
            await cog.on_ready()

        self.assertFalse(cog._startup_done)
        self.assertFalse(cog._startup_refreshing)
        self.assertEqual(refresh.await_count, 2)
        sleep.assert_awaited_once()

    async def test_deleted_panel_is_not_interpreted_as_a_post(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)
        payload = SimpleNamespace(
            channel_id=schedule.SCHEDULED_CHANNEL_ID,
            message_id=999,
        )

        with (
            patch.object(
                schedule,
                "get_post_by_message_id",
                new=AsyncMock(return_value=None),
            ) as get_post,
        ):
            await cog.on_raw_message_delete(payload)

        get_post.assert_awaited_once_with("999")

    async def test_refresh_deletion_event_is_never_treated_as_cancellation(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)
        payload = SimpleNamespace(
            channel_id=schedule.SCHEDULED_CHANNEL_ID,
            message_id=123,
        )
        schedule._get_internal_deleted_message_ids(bot).add("123")

        with patch.object(
            schedule,
            "get_post_by_message_id",
            new=AsyncMock(),
        ) as get_post:
            await cog.on_raw_message_delete(payload)

        get_post.assert_not_awaited()
        self.assertNotIn(
            "123",
            schedule._get_internal_deleted_message_ids(bot),
        )

    async def test_external_deletion_never_cancels_the_database_post(self):
        bot = Mock()
        cog = schedule.ScheduleCog(bot)
        payload = SimpleNamespace(
            channel_id=schedule.SCHEDULED_CHANNEL_ID,
            message_id=321,
        )
        post = {
            "id": 8,
            "status": "scheduled",
        }

        with (
            patch.object(
                schedule,
                "get_post_by_message_id",
                new=AsyncMock(return_value=post),
            ) as get_post,
            patch.object(
                schedule,
                "cancel_scheduled_post_by_message_id",
                new=AsyncMock(),
            ) as cancel_post,
            self.assertLogs("rota-bot.schedule", level="WARNING"),
        ):
            await cog.on_raw_message_delete(payload)

        get_post.assert_awaited_once_with("321")
        cancel_post.assert_not_awaited()

    async def test_explicit_cancel_button_soft_cancels_and_archives(self):
        bot = Mock()
        schedule_message = SimpleNamespace(id=321, delete=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        post = {
            "id": 8,
            "status": "scheduled",
            "discord_message_id": "321",
        }
        cancellation = {
            "id": 8,
            "cancellation_id": 12,
        }
        view = schedule.CancelPostConfirmationView(
            bot,
            post_id=8,
            user_id=99,
            schedule_message=schedule_message,
        )

        with (
            patch.object(
                schedule,
                "get_post_by_id",
                new=AsyncMock(return_value=post),
            ),
            patch.object(
                schedule,
                "cancel_scheduled_post_by_message_id",
                new=AsyncMock(return_value=cancellation),
            ) as cancel_post,
            patch.object(
                schedule,
                "archive_cancellation",
                new=AsyncMock(return_value=True),
            ) as archive,
        ):
            await view._on_confirm(interaction)

        cancel_post.assert_awaited_once()
        self.assertEqual(cancel_post.await_args.args[0], "321")
        archive.assert_awaited_once_with(bot, cancellation)
        schedule_message.delete.assert_awaited_once()
        self.assertIn(
            "moved to the archive",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_cancellation_archive_has_persistent_reschedule_button(self):
        archive_message = SimpleNamespace(id=700)
        archive_channel = SimpleNamespace(
            send=AsyncMock(return_value=archive_message)
        )
        bot = Mock()
        bot.get_channel.return_value = archive_channel
        cancellation = {
            "id": 8,
            "cancellation_id": 12,
            "cancelled_at": 1_000,
            "cancelled_scheduled_at": 2_000,
            "created_by": "42",
            "content": "Cancelled content",
            "image_path": None,
            "status": "cancelled",
            "cancellation_archive_message_id": None,
            "rescheduled_at": None,
            "rescheduled_by": None,
        }

        with patch.object(
            schedule,
            "record_cancellation_archive_message",
            new=AsyncMock(return_value=True),
        ) as record:
            self.assertTrue(
                await schedule.archive_cancellation(bot, cancellation)
            )

        sent = archive_channel.send.await_args
        self.assertIn("Scheduled post cancelled", sent.args[0])
        view = sent.kwargs["view"]
        self.assertIsInstance(view, schedule.CancelledPostView)
        self.assertTrue(view.is_persistent())
        self.assertEqual(view.children[0].label, "Reschedule")
        record.assert_awaited_once_with(12, "700")

    async def test_rescheduling_updates_archive_and_refreshes_schedule(self):
        bot = Mock()
        archive_message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        cancellation = {
            "id": 8,
            "cancellation_id": 12,
            "cancelled_at": 1_000,
            "cancelled_scheduled_at": 2_000,
            "created_by": "42",
            "scheduled_at": 3_000,
            "rescheduled_at": 1_100,
            "rescheduled_by": "99",
        }
        view = schedule.RescheduleCancelledView(
            bot,
            cancellation_id=12,
            user_id=99,
            archive_message=archive_message,
        )

        with (
            patch.object(schedule.time, "time", return_value=1_100),
            patch.object(
                schedule,
                "reschedule_cancelled_post",
                new=AsyncMock(return_value=True),
            ) as reschedule,
            patch.object(
                schedule,
                "get_cancellation_with_post",
                new=AsyncMock(return_value=cancellation),
            ),
            patch.object(
                schedule,
                "repost_all_scheduled",
                new=AsyncMock(return_value=True),
            ) as repost,
        ):
            await view._finalize_time(
                interaction,
                3_000,
                schedule.datetime.now(tz=schedule.UTC),
            )

        reschedule.assert_awaited_once_with(12, 3_000, "99", 1_100)
        archive_message.edit.assert_awaited_once()
        self.assertIsNone(archive_message.edit.await_args.kwargs["view"])
        repost.assert_awaited_once_with(bot)


class ScheduleComposerTests(unittest.TestCase):
    def test_modal_contains_required_text_and_optional_single_upload(self):
        modal = schedule.SchedulePostModal(Mock(), user_id=123)

        self.assertEqual(len(modal.children), 3)
        content_label, upload_label, delivery_label = modal.children
        self.assertIsInstance(content_label, discord.ui.Label)
        self.assertIsInstance(upload_label, discord.ui.Label)
        self.assertIsInstance(delivery_label, discord.ui.Label)

        content_input = content_label.component
        self.assertIsInstance(content_input, discord.ui.TextInput)
        self.assertTrue(content_input.required)
        self.assertEqual(content_input.max_length, 4000)

        upload = upload_label.component
        self.assertIsInstance(upload, discord.ui.FileUpload)
        self.assertFalse(upload.required)
        self.assertEqual(upload.min_values, 0)
        self.assertEqual(upload.max_values, 1)

        delivery = delivery_label.component
        self.assertIsInstance(delivery, discord.ui.Select)
        self.assertTrue(delivery.required)
        self.assertEqual(delivery.options[0].value, "off")
        self.assertEqual(
            [int(option.value) for option in delivery.options[1:]],
            [minutes for _, minutes in schedule.DISCORD_DELAY_CHOICES],
        )

    def test_filename_and_mime_precheck_rejects_spoof_mismatches(self):
        valid = SimpleNamespace(filename="photo.JPEG", content_type="image/jpeg")
        self.assertEqual(schedule.precheck_image_attachment(valid), "jpeg")

        missing_mime = SimpleNamespace(filename="photo.png", content_type=None)
        self.assertEqual(
            schedule.precheck_image_attachment(missing_mime),
            "png",
        )

        mismatches = (
            SimpleNamespace(filename="payload.exe", content_type="image/png"),
            SimpleNamespace(filename="photo.png", content_type="video/mp4"),
            SimpleNamespace(filename="photo", content_type=None),
        )
        for attachment in mismatches:
            with self.subTest(
                filename=attachment.filename,
                content_type=attachment.content_type,
            ):
                with self.assertRaises(schedule.ImageValidationError):
                    schedule.precheck_image_attachment(attachment)

    def test_supported_content_signatures_are_detected(self):
        signatures = {
            b"\xff\xd8\xff\xe0jpeg": "jpeg",
            b"\x89PNG\r\n\x1a\npng": "png",
            b"GIF87agif": "gif",
            b"GIF89agif": "gif",
            b"RIFF\x04\x00\x00\x00WEBPdata": "webp",
        }
        for data, expected in signatures.items():
            with self.subTest(expected=expected):
                self.assertEqual(schedule.detect_image_format(data), expected)

        self.assertIsNone(schedule.detect_image_format(b"MZ executable"))


class ImageStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_content_without_mime_is_saved_with_detected_extension(self):
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(output, format="PNG")
        png_data = output.getvalue()
        attachment = SimpleNamespace(
            filename="upload.PNG",
            content_type=None,
            read=AsyncMock(return_value=png_data),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(schedule, "IMAGES_DIR", Path(temp_dir)):
                image_path, error = (
                    await schedule.save_validated_image_attachment(attachment)
                )

            self.assertIsNone(error)
            saved_path = Path(image_path)
            self.assertEqual(saved_path.suffix, ".png")
            self.assertEqual(saved_path.read_bytes(), png_data)

    async def test_jpeg_extension_is_normalized_from_detected_content(self):
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(output, format="JPEG")
        jpeg_data = output.getvalue()
        attachment = SimpleNamespace(
            filename="upload.jpeg",
            content_type="image/jpeg",
            read=AsyncMock(return_value=jpeg_data),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(schedule, "IMAGES_DIR", Path(temp_dir)):
                image_path, error = (
                    await schedule.save_validated_image_attachment(attachment)
                )

            self.assertIsNone(error)
            self.assertEqual(Path(image_path).suffix, ".jpg")

    async def test_spoofed_or_mismatched_content_is_rejected(self):
        cases = (
            ("payload.png", "image/png", b"MZ executable content"),
            (
                "payload.exe",
                "image/png",
                b"\x89PNG\r\n\x1a\nvalid PNG with unsafe filename",
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(schedule, "IMAGES_DIR", Path(temp_dir)):
                for filename, content_type, data in cases:
                    with self.subTest(
                        filename=filename,
                        content_type=content_type,
                    ):
                        attachment = SimpleNamespace(
                            filename=filename,
                            content_type=content_type,
                            read=AsyncMock(return_value=data),
                        )
                        image_path, error = (
                            await schedule.save_validated_image_attachment(
                                attachment
                            )
                        )
                        self.assertIsNone(image_path)
                        self.assertIsNotNone(error)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])


class ScheduleComposerSubmissionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interaction():
        return SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_rejected_upload_stops_before_saving_or_time_picker(self):
        modal = schedule.SchedulePostModal(Mock(), user_id=123)
        modal.content_input._value = "Post text"
        modal.file_upload._values = [
            SimpleNamespace(filename="payload.exe", content_type="video/mp4")
        ]
        interaction = self._interaction()

        with (
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(
                    return_value=(None, schedule.SUPPORTED_IMAGE_ERROR)
                ),
            ) as save_attachment,
            patch.object(schedule, "ScheduleView") as schedule_view,
        ):
            await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True
        )
        interaction.followup.send.assert_awaited_once_with(
            schedule.SUPPORTED_IMAGE_ERROR,
            ephemeral=True,
        )
        save_attachment.assert_awaited_once()
        schedule_view.assert_not_called()

    async def test_save_failure_stops_before_time_picker(self):
        modal = schedule.SchedulePostModal(Mock(), user_id=123)
        modal.content_input._value = "Post text"
        attachment = SimpleNamespace(filename="post.png", content_type="image/png")
        modal.file_upload._values = [attachment]
        interaction = self._interaction()

        with (
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(
                    return_value=(None, schedule.IMAGE_SAVE_ERROR)
                ),
            ) as save_attachment,
            patch.object(schedule, "ScheduleView") as schedule_view,
        ):
            await modal.on_submit(interaction)

        save_attachment.assert_awaited_once_with(attachment)
        interaction.followup.send.assert_awaited_once_with(
            schedule.IMAGE_SAVE_ERROR,
            ephemeral=True,
        )
        schedule_view.assert_not_called()

    async def test_saved_upload_launches_existing_time_picker(self):
        bot = Mock()
        modal = schedule.SchedulePostModal(bot, user_id=123)
        modal.content_input._value = "Post text"
        attachment = SimpleNamespace(filename="post.gif", content_type="image/gif")
        modal.file_upload._values = [attachment]
        interaction = self._interaction()
        view = Mock()
        view._status_text.return_value = "Pick a time"

        with (
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(
                    return_value=("data/images/post.gif", None)
                ),
            ),
            patch.object(
                schedule,
                "ScheduleView",
                return_value=view,
            ) as schedule_view,
        ):
            await modal.on_submit(interaction)

        schedule_view.assert_called_once_with(
            bot,
            "Post text",
            123,
            "data/images/post.gif",
            post_to_x=True,
            post_to_discord=True,
            discord_delay_minutes=0,
        )
        interaction.followup.send.assert_awaited_once()
        followup_call = interaction.followup.send.await_args
        self.assertEqual(followup_call.kwargs["content"], "Pick a time")
        self.assertIs(followup_call.kwargs["view"], view)
        self.assertTrue(followup_call.kwargs["ephemeral"])

    async def test_no_attachment_launches_time_picker_without_saving(self):
        bot = Mock()
        modal = schedule.SchedulePostModal(bot, user_id=123)
        modal.content_input._value = "Text only"
        modal.file_upload._values = []
        interaction = self._interaction()
        view = Mock()
        view._status_text.return_value = "Pick a time"

        with (
            patch.object(
                schedule,
                "save_validated_media_attachment",
                new=AsyncMock(),
            ) as save_attachment,
            patch.object(
                schedule,
                "ScheduleView",
                return_value=view,
            ) as schedule_view,
        ):
            await modal.on_submit(interaction)

        save_attachment.assert_not_awaited()
        schedule_view.assert_called_once_with(
            bot,
            "Text only",
            123,
            None,
            post_to_x=True,
            post_to_discord=True,
            discord_delay_minutes=0,
        )
        interaction.followup.send.assert_awaited_once()

    async def test_delivery_selection_is_forwarded_to_time_picker(self):
        bot = Mock()
        cases = (
            ("off", False, 0),
            ("60", True, 60),
        )
        for selected, post_to_discord, delay in cases:
            with self.subTest(selected=selected):
                modal = schedule.SchedulePostModal(bot, user_id=123)
                modal.content_input._value = "Delivery post"
                modal.file_upload._values = []
                modal.discord_delivery_select._values = [selected]
                interaction = self._interaction()
                view = Mock()
                view._status_text.return_value = "Pick a time"

                with patch.object(
                    schedule,
                    "ScheduleView",
                    return_value=view,
                ) as schedule_view:
                    await modal.on_submit(interaction)

                schedule_view.assert_called_once_with(
                    bot,
                    "Delivery post",
                    123,
                    None,
                    post_to_x=True,
                    post_to_discord=post_to_discord,
                    discord_delay_minutes=delay,
                )


if __name__ == "__main__":
    unittest.main()
