import importlib
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

schedule = importlib.import_module("cogs.schedule")


def interaction(value: str | int | None = None):
    data = {"values": [str(value)]} if value is not None else {}
    return SimpleNamespace(
        data=data,
        response=SimpleNamespace(
            edit_message=AsyncMock(),
            send_modal=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


class EphemeralPickerResponse:
    """Single-use response adapter for a component-origin modal submit."""

    def __init__(self, interaction):
        self.interaction = interaction
        self.action = None
        self.content = None
        self.view = None
        self.edit_message = AsyncMock(side_effect=self._edit_message)

    async def _edit_message(self, *, content=None, view=None, **kwargs):
        if self.action is not None:
            raise AssertionError("An interaction can only be responded to once")
        if not self.interaction.message.flags.ephemeral:
            raise AssertionError("Expected an originating ephemeral picker")
        if (
            view is not None
            and view is not self.interaction.originating_view
        ):
            raise AssertionError("Response replaced the wrong picker")
        self.action = "edit_message"
        self.content = content
        self.view = view

    def is_done(self):
        return self.action is not None


class EphemeralPickerModalInteraction:
    """Minimal modal interaction carrying its originating ephemeral message."""

    def __init__(self, originating_view):
        self.originating_view = originating_view
        self.message = SimpleNamespace(
            flags=SimpleNamespace(ephemeral=True),
        )
        self.response = EphemeralPickerResponse(self)


def modal_interaction(view):
    return EphemeralPickerModalInteraction(view)


async def finalize_ephemeral_picker(interaction, unix_ts, local_dt):
    await interaction.response.edit_message(
        content=f"Scheduled for {unix_ts}",
        view=None,
    )


def picker_views():
    return (
        ("create", schedule.ScheduleView(Mock(), "Post", user_id=1)),
        ("edit", schedule.EditTimeView(Mock(), post_id=7)),
    )


class ScheduleParserTests(unittest.TestCase):
    def test_compact_and_colon_times(self):
        self.assertEqual(schedule.parse_schedule_time("1236"), (12, 36))
        self.assertEqual(schedule.parse_schedule_time("0037"), (0, 37))
        self.assertEqual(schedule.parse_schedule_time("12:36"), (12, 36))

    def test_malformed_and_out_of_range_times_are_rejected(self):
        malformed = (
            "",
            "123",
            "12345",
            "12x6",
            "12-36",
            "1:36",
            "12:3",
            "１２３６",
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(
                schedule.ScheduleDateTimeError
            ):
                schedule.parse_schedule_time(value)

        with self.assertRaisesRegex(
            schedule.ScheduleDateTimeError, "00 and 23"
        ):
            schedule.parse_schedule_time("2400")
        with self.assertRaisesRegex(
            schedule.ScheduleDateTimeError, "00 and 59"
        ):
            schedule.parse_schedule_time("1260")

    def test_iso_dates_include_real_leap_days_only(self):
        self.assertEqual(
            schedule.parse_schedule_date("2024-02-29"),
            date(2024, 2, 29),
        )
        for value in ("2023-02-29", "2024-2-29", "29-02-2024"):
            with self.subTest(value=value), self.assertRaises(
                schedule.ScheduleDateTimeError
            ):
                schedule.parse_schedule_date(value)

    def test_sixty_day_horizon_is_inclusive_by_local_calendar_date(self):
        now = datetime(2026, 1, 15, 12, tzinfo=ZoneInfo("UTC"))
        allowed = (now.date() + timedelta(days=60)).isoformat()
        too_late = (now.date() + timedelta(days=61)).isoformat()

        result = schedule.validate_scheduled_datetime(
            allowed,
            23,
            59,
            "UTC",
            now=now,
        )
        self.assertEqual(result.date().isoformat(), allowed)

        with self.assertRaisesRegex(
            schedule.ScheduleDateTimeError, "60 calendar days"
        ):
            schedule.validate_scheduled_datetime(
                too_late,
                0,
                0,
                "UTC",
                now=now,
            )

    def test_past_present_and_dst_conversion(self):
        london = ZoneInfo("Europe/London")
        now = datetime(2026, 3, 1, 12, tzinfo=ZoneInfo("UTC"))
        winter = schedule.validate_scheduled_datetime(
            "2026-03-28", 12, 0, "Europe/London", now=now
        )
        summer = schedule.validate_scheduled_datetime(
            "2026-03-30", 12, 0, "Europe/London", now=now
        )
        self.assertEqual(
            winter.astimezone(ZoneInfo("UTC")),
            datetime(2026, 3, 28, 12, tzinfo=ZoneInfo("UTC")),
        )
        self.assertEqual(
            summer.astimezone(ZoneInfo("UTC")),
            datetime(2026, 3, 30, 11, tzinfo=ZoneInfo("UTC")),
        )

        transition_now = datetime(2026, 3, 28, 0, tzinfo=london)
        with self.assertRaisesRegex(
            schedule.ScheduleDateTimeError, "does not exist"
        ):
            schedule.validate_scheduled_datetime(
                "2026-03-29",
                1,
                30,
                "Europe/London",
                now=transition_now,
            )

        fallback = schedule.validate_scheduled_datetime(
            "2026-10-25",
            1,
            30,
            "Europe/London",
            now=datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")),
        )
        self.assertEqual(fallback.fold, 0)
        self.assertEqual(
            fallback.astimezone(ZoneInfo("UTC")),
            datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("UTC")),
        )

        present = datetime(2026, 5, 10, 12, tzinfo=ZoneInfo("UTC"))
        for hour, minute in ((11, 59), (12, 0)):
            with self.subTest(
                hour=hour,
                minute=minute,
            ), self.assertRaisesRegex(
                schedule.ScheduleDateTimeError, "past or present"
            ):
                schedule.validate_scheduled_datetime(
                    "2026-05-10",
                    hour,
                    minute,
                    "UTC",
                    now=present,
                )


class SchedulePickerLayoutTests(unittest.TestCase):
    def test_create_and_edit_use_full_quick_ranges_and_five_rows(self):
        for name, view in picker_views():
            with self.subTest(view=name):
                self.assertEqual(len(view.children), 5)
                timezone, day, hour, minute, exact = view.children

                self.assertIsInstance(timezone, discord.ui.Button)
                self.assertIsInstance(day, discord.ui.Select)
                self.assertEqual(
                    len(day.options),
                    schedule.QUICK_DATE_OPTION_COUNT,
                )
                first_day = date.fromisoformat(day.options[0].value)
                last_day = date.fromisoformat(day.options[-1].value)
                self.assertEqual(last_day - first_day, timedelta(days=24))

                self.assertEqual(
                    [int(option.value) for option in hour.options],
                    list(
                        range(
                            schedule.QUICK_HOUR_START,
                            schedule.QUICK_HOUR_END + 1,
                        )
                    ),
                )
                self.assertEqual(
                    [int(option.value) for option in minute.options],
                    list(schedule.QUICK_MINUTES),
                )
                self.assertIsInstance(exact, discord.ui.Button)
                self.assertEqual(exact.label, "Enter exact date/time")
                self.assertEqual(exact.row, 4)

    def test_exact_modal_has_optional_fields_and_unique_supported_ids(self):
        view = schedule.ScheduleView(Mock(), "Post", user_id=1)
        first = schedule.ExactDateTimeModal(view)
        second = schedule.ExactDateTimeModal(view)

        self.assertFalse(first.date_input.required)
        self.assertFalse(first.time_input.required)
        self.assertEqual(first.date_input.max_length, 10)
        self.assertEqual(first.time_input.max_length, 5)
        self.assertTrue(
            first.custom_id.startswith(
                schedule.EXACT_TIME_MODAL_CUSTOM_ID_PREFIX
            )
        )
        self.assertNotEqual(first.custom_id, second.custom_id)

    def test_quick_dates_follow_the_current_selected_timezone(self):
        timezone = "America/Los_Angeles"
        before = datetime.now(tz=ZoneInfo(timezone)).date()
        for name, view in picker_views():
            with self.subTest(view=name):
                view.timezone = timezone
                view._build_selects()
                first_option_day = date.fromisoformat(
                    view.children[1].options[0].value
                )
                after = datetime.now(tz=ZoneInfo(timezone)).date()
                self.assertIn(first_option_day, (before, after))


class SchedulePickerWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_buttons_open_modals_bound_to_each_picker(self):
        for name, view in picker_views():
            with self.subTest(view=name):
                button_interaction = interaction()
                await view._on_exact_time(button_interaction)

                modal = (
                    button_interaction.response.send_modal.await_args.args[0]
                )
                self.assertIsInstance(modal, schedule.ExactDateTimeModal)
                self.assertIs(modal.picker, view)

    async def test_dropdown_date_then_manual_time_in_both_views(self):
        target_day = (
            datetime.now(tz=ZoneInfo(schedule.DEFAULT_TZ)).date()
            + timedelta(days=10)
        ).isoformat()

        for name, view in picker_views():
            with self.subTest(view=name):
                view._finalize_time = AsyncMock(
                    side_effect=finalize_ephemeral_picker
                )
                await view._on_day_select(interaction(target_day))
                modal = schedule.ExactDateTimeModal(view)
                modal.date_input._value = ""
                modal.time_input._value = "0037"
                submit_interaction = modal_interaction(view)

                await modal.on_submit(submit_interaction)

                self.assertEqual(view.selected_day, target_day)
                self.assertEqual(view.selected_hour, 0)
                self.assertEqual(view.selected_minute, 37)
                view._finalize_time.assert_awaited_once()
                self.assertEqual(
                    submit_interaction.response.action,
                    "edit_message",
                )
                self.assertIsNone(submit_interaction.response.view)
                self.assertTrue(submit_interaction.response.is_done())

    async def test_selected_complete_time_then_manual_date_in_both_views(self):
        target_day = (
            datetime.now(tz=ZoneInfo(schedule.DEFAULT_TZ)).date()
            + timedelta(days=10)
        ).isoformat()

        for name, view in picker_views():
            with self.subTest(view=name):
                view.selected_hour = 12
                view.selected_minute = 36
                view._finalize_time = AsyncMock(
                    side_effect=finalize_ephemeral_picker
                )
                modal = schedule.ExactDateTimeModal(view)
                modal.date_input._value = target_day
                modal.time_input._value = ""
                submit_interaction = modal_interaction(view)

                await modal.on_submit(submit_interaction)

                self.assertEqual(view.selected_day, target_day)
                self.assertEqual(view.selected_hour, 12)
                self.assertEqual(view.selected_minute, 36)
                view._finalize_time.assert_awaited_once()
                self.assertEqual(
                    submit_interaction.response.action,
                    "edit_message",
                )
                self.assertIsNone(submit_interaction.response.view)

    async def test_later_manual_or_dropdown_choice_wins(self):
        today = datetime.now(tz=ZoneInfo(schedule.DEFAULT_TZ)).date()
        first_day = (today + timedelta(days=5)).isoformat()
        second_day = (today + timedelta(days=6)).isoformat()

        for name, view in picker_views():
            with self.subTest(view=name):
                view._finalize_time = AsyncMock(
                    side_effect=finalize_ephemeral_picker
                )
                await view._on_day_select(interaction(first_day))
                await view._on_day_select(interaction(second_day))
                self.assertEqual(view.selected_day, second_day)

                await view._on_hour_select(interaction(8))
                modal = schedule.ExactDateTimeModal(view)
                modal.date_input._value = ""
                modal.time_input._value = "1236"
                await modal.on_submit(modal_interaction(view))
                self.assertEqual(
                    (view.selected_hour, view.selected_minute),
                    (12, 36),
                )

    async def test_fresh_blank_date_with_valid_time_is_rejected(self):
        with (
            patch.object(schedule, "insert_post", new=AsyncMock()) as insert,
            patch.object(
                schedule,
                "update_post_scheduled_at",
                new=AsyncMock(),
            ) as update,
        ):
            for name, view in picker_views():
                with self.subTest(view=name):
                    view._finalize_time = AsyncMock()
                    modal = schedule.ExactDateTimeModal(view)
                    modal.date_input._value = ""
                    modal.time_input._value = "1236"
                    submit_interaction = modal_interaction(view)

                    await modal.on_submit(submit_interaction)

                    self.assertEqual(
                        (
                            view.selected_day,
                            view.selected_hour,
                            view.selected_minute,
                        ),
                        (None, None, None),
                    )
                    self.assertFalse(view.submitted)
                    view._finalize_time.assert_not_awaited()
                    self.assertIn(
                        "Date is required",
                        submit_interaction.response.content,
                    )
                    self.assertEqual(
                        submit_interaction.response.action,
                        "edit_message",
                    )
                    self.assertIs(submit_interaction.response.view, view)

            insert.assert_not_awaited()
            update.assert_not_awaited()

    async def test_fresh_valid_date_with_blank_time_is_rejected(self):
        target_day = (
            datetime.now(tz=ZoneInfo(schedule.DEFAULT_TZ)).date()
            + timedelta(days=10)
        ).isoformat()
        with (
            patch.object(schedule, "insert_post", new=AsyncMock()) as insert,
            patch.object(
                schedule,
                "update_post_scheduled_at",
                new=AsyncMock(),
            ) as update,
        ):
            for name, view in picker_views():
                with self.subTest(view=name):
                    view._finalize_time = AsyncMock()
                    modal = schedule.ExactDateTimeModal(view)
                    modal.date_input._value = target_day
                    modal.time_input._value = ""
                    submit_interaction = modal_interaction(view)

                    await modal.on_submit(submit_interaction)

                    self.assertEqual(
                        (
                            view.selected_day,
                            view.selected_hour,
                            view.selected_minute,
                        ),
                        (None, None, None),
                    )
                    self.assertFalse(view.submitted)
                    view._finalize_time.assert_not_awaited()
                    self.assertIn(
                        "Time is required",
                        submit_interaction.response.content,
                    )
                    self.assertEqual(
                        submit_interaction.response.action,
                        "edit_message",
                    )
                    self.assertIs(submit_interaction.response.view, view)

            insert.assert_not_awaited()
            update.assert_not_awaited()

    async def test_modal_failures_preserve_state_and_do_not_submit(self):
        today = datetime.now(tz=ZoneInfo(schedule.DEFAULT_TZ)).date()
        valid_day = (today + timedelta(days=10)).isoformat()
        too_late = (
            today + timedelta(days=schedule.SCHEDULE_HORIZON_DAYS + 1)
        ).isoformat()

        for name, view in picker_views():
            with self.subTest(view=name):
                view.selected_day = valid_day
                view.selected_hour = 12
                view.selected_minute = 30
                view._finalize_time = AsyncMock()
                original = (
                    view.selected_day,
                    view.selected_hour,
                    view.selected_minute,
                )

                bad_time = schedule.ExactDateTimeModal(view)
                bad_time.date_input._value = ""
                bad_time.time_input._value = "2400"
                bad_time_interaction = modal_interaction(view)
                await bad_time.on_submit(bad_time_interaction)

                self.assertEqual(
                    (
                        view.selected_day,
                        view.selected_hour,
                        view.selected_minute,
                    ),
                    original,
                )
                self.assertFalse(view.submitted)
                view._finalize_time.assert_not_awaited()
                self.assertIn(
                    "00 and 23",
                    bad_time_interaction.response.content,
                )
                self.assertIs(bad_time_interaction.response.view, view)

                bad_date = schedule.ExactDateTimeModal(view)
                bad_date.date_input._value = too_late
                bad_date.time_input._value = ""
                bad_date_interaction = modal_interaction(view)
                await bad_date.on_submit(bad_date_interaction)

                self.assertEqual(view.selected_day, valid_day)
                self.assertFalse(view.submitted)
                view._finalize_time.assert_not_awaited()
                self.assertIn(
                    "60 calendar days",
                    bad_date_interaction.response.content,
                )
                self.assertIs(bad_date_interaction.response.view, view)


class ScheduleFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_edit_reject_same_past_and_horizon_states(self):
        now = datetime(2026, 5, 10, 12, tzinfo=ZoneInfo("UTC"))
        invalid_states = (
            ("2026-05-10", 12, 0, "past or present"),
            (
                (now.date() + timedelta(days=61)).isoformat(),
                12,
                1,
                "60 calendar days",
            ),
        )

        with (
            patch.object(schedule, "_utc_now", return_value=now),
            patch.object(schedule, "insert_post", new=AsyncMock()) as insert,
            patch.object(
                schedule,
                "update_post_scheduled_at",
                new=AsyncMock(),
            ) as update,
        ):
            for view_name, view_factory in (
                (
                    "create",
                    lambda: schedule.ScheduleView(Mock(), "Post", user_id=1),
                ),
                (
                    "edit",
                    lambda: schedule.EditTimeView(Mock(), post_id=7),
                ),
            ):
                for day, hour, minute, error in invalid_states:
                    with self.subTest(view=view_name, error=error):
                        view = view_factory()
                        view.timezone = "UTC"
                        view.selected_day = day
                        view.selected_hour = hour
                        view.selected_minute = minute
                        picker_interaction = interaction()

                        await view._try_submit(picker_interaction)

                        self.assertFalse(view.submitted)
                        self.assertIn(
                            error,
                            picker_interaction.response.edit_message.await_args.kwargs[
                                "content"
                            ],
                        )

            insert.assert_not_awaited()
            update.assert_not_awaited()

    async def test_create_and_edit_store_identical_dst_aware_unix_time(self):
        now = datetime(2026, 6, 1, 0, tzinfo=ZoneInfo("UTC"))
        expected = int(
            datetime(
                2026,
                7,
                1,
                12,
                36,
                tzinfo=ZoneInfo("Europe/London"),
            ).timestamp()
        )
        bot = Mock()

        create = schedule.ScheduleView(bot, "Post", user_id=1)
        create.selected_day = "2026-07-01"
        create.selected_hour = 12
        create.selected_minute = 36

        edit = schedule.EditTimeView(bot, post_id=7)
        edit.selected_day = "2026-07-01"
        edit.selected_hour = 12
        edit.selected_minute = 36

        with (
            patch.object(schedule, "_utc_now", return_value=now),
            patch.object(schedule, "insert_post", new=AsyncMock()) as insert,
            patch.object(
                schedule,
                "update_post_scheduled_at",
                new=AsyncMock(),
            ) as update,
            patch.object(
                schedule,
                "repost_all_scheduled",
                new=AsyncMock(),
            ),
        ):
            await create._try_submit(interaction())
            await edit._try_submit(interaction())

        self.assertEqual(insert.await_args.kwargs["scheduled_at"], expected)
        update.assert_awaited_once_with(7, expected)


if __name__ == "__main__":
    unittest.main()
