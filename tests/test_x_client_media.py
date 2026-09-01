import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("SCHEDULED_CHANNEL_ID", "2")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "3")
os.environ.setdefault("REMINDERS_CHANNEL_ID", "4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    __import__("tweepy")
except ModuleNotFoundError:
    tweepy_stub = types.ModuleType("tweepy")
    tweepy_stub.API = type("API", (), {})
    tweepy_stub.Client = type("Client", (), {})
    tweepy_stub.OAuth1UserHandler = type("OAuth1UserHandler", (), {})
    sys.modules["tweepy"] = tweepy_stub

X_CLIENT_PATH = Path(__file__).resolve().parents[1] / "src" / "x_client.py"
X_CLIENT_SPEC = importlib.util.spec_from_file_location(
    "phase4_x_client",
    X_CLIENT_PATH,
)
x_client = importlib.util.module_from_spec(X_CLIENT_SPEC)
X_CLIENT_SPEC.loader.exec_module(x_client)


SIGNATURES = {
    ".jpg": b"\xff\xd8\xff\xe0stored jpeg",
    ".png": b"\x89PNG\r\n\x1a\nstored png",
    ".webp": b"RIFF\x04\x00\x00\x00WEBPstored",
    ".gif": b"GIF89astored gif",
    ".mp4": b"\x00\x00\x00\x18ftypisomstored mp4",
}


def v2_client():
    client = Mock()
    client.create_tweet.return_value = SimpleNamespace(data={"id": "456"})
    client.get_me.return_value = SimpleNamespace(
        data=SimpleNamespace(username="rota")
    )
    return client


def real_image_bytes(image_format: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format=image_format)
    return output.getvalue()


class XMediaUploadTests(unittest.TestCase):
    def test_images_use_simple_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for extension in (".jpg", ".png", ".webp"):
                with self.subTest(extension=extension):
                    path = Path(temp_dir) / f"image{extension}"
                    path.write_bytes(SIGNATURES[extension])
                    api = Mock()
                    api.media_upload.return_value = SimpleNamespace(
                        media_id=123,
                    )
                    client = v2_client()

                    with (
                        patch.object(x_client, "X_ENABLED", True),
                        patch.object(
                            x_client,
                            "_get_v1_api",
                            return_value=api,
                        ),
                        patch.object(
                            x_client,
                            "_get_v2_client",
                            return_value=client,
                        ),
                    ):
                        result = x_client.post_tweet("Post", str(path))

                    self.assertEqual(
                        result,
                        "https://x.com/i/web/status/456",
                    )
                    api.media_upload.assert_called_once_with(
                        filename=str(path)
                    )
                    client.create_tweet.assert_called_once_with(
                        text="Post",
                        media_ids=[123],
                    )
                    client.get_me.assert_not_called()

    def test_gif_and_video_use_chunked_upload_categories(self):
        cases = (
            (".gif", "tweet_gif"),
            (".mp4", "tweet_video"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for extension, category in cases:
                with self.subTest(extension=extension):
                    path = Path(temp_dir) / f"media{extension}"
                    path.write_bytes(SIGNATURES[extension])
                    api = Mock()
                    api.media_upload.return_value = SimpleNamespace(
                        media_id=321,
                        processing_info=None,
                    )
                    client = v2_client()

                    with (
                        patch.object(x_client, "X_ENABLED", True),
                        patch.object(
                            x_client,
                            "_get_v1_api",
                            return_value=api,
                        ),
                        patch.object(
                            x_client,
                            "_get_v2_client",
                            return_value=client,
                        ),
                    ):
                        result = x_client.post_tweet("Post", str(path))

                    self.assertIsNotNone(result)
                    api.media_upload.assert_called_once_with(
                        filename=str(path),
                        chunked=True,
                        media_category=category,
                        wait_for_async_finalize=False,
                    )
                    api.get_media_upload_status.assert_not_called()
                    client.create_tweet.assert_called_once_with(
                        text="Post",
                        media_ids=[321],
                    )

    def test_pending_processing_is_polled_until_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(SIGNATURES[".mp4"])
            api = Mock()
            api.media_upload.return_value = SimpleNamespace(
                media_id=123,
                processing_info={
                    "state": "pending",
                    "check_after_secs": 1,
                },
            )
            api.get_media_upload_status.return_value = SimpleNamespace(
                media_id=123,
                processing_info={"state": "succeeded"},
            )
            client = v2_client()

            with (
                patch.object(x_client, "X_ENABLED", True),
                patch.object(x_client, "_get_v1_api", return_value=api),
                patch.object(x_client, "_get_v2_client", return_value=client),
                patch.object(x_client.time, "sleep") as sleep,
            ):
                result = x_client.post_tweet("Post", str(path))

            self.assertIsNotNone(result)
            sleep.assert_called_once_with(1)
            api.get_media_upload_status.assert_called_once_with(123)
            client.create_tweet.assert_called_once()

    def test_perpetually_pending_processing_stops_at_local_deadline(self):
        class FakeClock:
            def __init__(self):
                self.now = 0.0
                self.sleeps = []

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(SIGNATURES[".mp4"])
            pending = SimpleNamespace(
                media_id=123,
                processing_info={
                    "state": "pending",
                    "check_after_secs": 1,
                },
            )
            api = Mock()
            api.media_upload.return_value = pending
            api.get_media_upload_status.return_value = pending
            client = v2_client()
            clock = FakeClock()

            with (
                patch.object(x_client, "X_ENABLED", True),
                patch.object(
                    x_client,
                    "MEDIA_PROCESSING_TIMEOUT_SECONDS",
                    3,
                ),
                patch.object(x_client, "_get_v1_api", return_value=api),
                patch.object(x_client, "_get_v2_client", return_value=client),
                patch.object(
                    x_client.time,
                    "monotonic",
                    side_effect=clock.monotonic,
                ),
                patch.object(
                    x_client.time,
                    "sleep",
                    side_effect=clock.sleep,
                ),
                self.assertLogs("rota-bot.x-client", level="ERROR"),
            ):
                result = x_client.post_tweet("Post", str(path))

            self.assertIsNone(result)
            self.assertEqual(clock.sleeps, [1.0, 1.0, 1.0])
            self.assertEqual(api.get_media_upload_status.call_count, 2)
            client.create_tweet.assert_not_called()

    def test_status_success_after_deadline_times_out_with_bounded_request(self):
        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(SIGNATURES[".mp4"])
            pending = SimpleNamespace(
                media_id=123,
                processing_info={
                    "state": "pending",
                    "check_after_secs": 1,
                },
            )
            succeeded = SimpleNamespace(
                media_id=123,
                processing_info={"state": "succeeded"},
            )
            api = Mock()
            api.timeout = 60
            api.media_upload.return_value = pending
            client = v2_client()
            clock = FakeClock()
            request_timeouts = []
            remaining_at_request = []

            def delayed_status(_media_id):
                request_timeouts.append(api.timeout)
                remaining_at_request.append(5.0 - clock.now)
                clock.now = 6.0
                return succeeded

            api.get_media_upload_status.side_effect = delayed_status
            with (
                patch.object(x_client, "X_ENABLED", True),
                patch.object(
                    x_client,
                    "MEDIA_PROCESSING_TIMEOUT_SECONDS",
                    5,
                ),
                patch.object(x_client, "_get_v1_api", return_value=api),
                patch.object(x_client, "_get_v2_client", return_value=client),
                patch.object(
                    x_client.time,
                    "monotonic",
                    side_effect=clock.monotonic,
                ),
                patch.object(
                    x_client.time,
                    "sleep",
                    side_effect=clock.sleep,
                ),
                self.assertLogs("rota-bot.x-client", level="ERROR"),
            ):
                result = x_client.post_tweet("Post", str(path))

            self.assertIsNone(result)
            self.assertEqual(request_timeouts, [4.0])
            self.assertLessEqual(
                request_timeouts[0],
                remaining_at_request[0],
            )
            self.assertEqual(api.timeout, 60)
            api.get_media_upload_status.assert_called_once_with(123)
            client.create_tweet.assert_not_called()

    def test_failed_upload_or_processing_never_creates_tweet(self):
        failures = (
            RuntimeError("upload failed"),
            SimpleNamespace(
                media_id=123,
                processing_info={
                    "state": "failed",
                    "error": {"message": "unsupported video"},
                },
            ),
            SimpleNamespace(media_id=123, processing_info={}),
            SimpleNamespace(media_id=123, processing_info=[]),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(SIGNATURES[".mp4"])

            for failure in failures:
                with self.subTest(failure=failure):
                    api = Mock()
                    if isinstance(failure, Exception):
                        api.media_upload.side_effect = failure
                    else:
                        api.media_upload.return_value = failure
                    client = v2_client()

                    with (
                        patch.object(x_client, "X_ENABLED", True),
                        patch.object(
                            x_client,
                            "_get_v1_api",
                            return_value=api,
                        ),
                        patch.object(
                            x_client,
                            "_get_v2_client",
                            return_value=client,
                        ),
                        self.assertLogs(
                            "rota-bot.x-client",
                            level="ERROR",
                        ),
                    ):
                        result = x_client.post_tweet("Post", str(path))

                    self.assertIsNone(result)
                    client.create_tweet.assert_not_called()

    def test_legacy_jfif_and_extensionless_images_use_canonical_temp_names(self):
        cases = (
            ("legacy.jfif", real_image_bytes("JPEG"), ".jpg"),
            ("legacy", real_image_bytes("PNG"), ".png"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for filename, data, canonical_suffix in cases:
                with self.subTest(filename=filename):
                    path = Path(temp_dir) / filename
                    path.write_bytes(data)
                    api = Mock()
                    api.media_upload.return_value = SimpleNamespace(media_id=123)
                    client = v2_client()

                    with (
                        patch.object(x_client, "X_ENABLED", True),
                        patch.object(
                            x_client,
                            "_get_v1_api",
                            return_value=api,
                        ),
                        patch.object(
                            x_client,
                            "_get_v2_client",
                            return_value=client,
                        ),
                    ):
                        result = x_client.post_tweet("Post", str(path))

                    self.assertIsNotNone(result)
                    upload_name = Path(
                        api.media_upload.call_args.kwargs["filename"]
                    )
                    self.assertEqual(upload_name.suffix, canonical_suffix)
                    self.assertNotEqual(upload_name, path)
                    self.assertFalse(upload_name.exists())
                    client.create_tweet.assert_called_once()

    def test_legacy_spoofs_remain_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = (
                Path(temp_dir) / "spoofed.jfif",
                Path(temp_dir) / "extensionless",
            )
            paths[0].write_bytes(real_image_bytes("PNG"))
            paths[1].write_bytes(b"MZ executable")
            for path in paths:
                with self.subTest(path=path):
                    client = v2_client()
                    with (
                        patch.object(x_client, "X_ENABLED", True),
                        patch.object(
                            x_client,
                            "_get_v2_client",
                            return_value=client,
                        ),
                        self.assertLogs(
                            "rota-bot.x-client",
                            level="ERROR",
                        ),
                    ):
                        result = x_client.post_tweet("Post", str(path))
                    self.assertIsNone(result)
                    client.create_tweet.assert_not_called()

    def test_legacy_canonical_temp_file_is_removed_when_upload_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.jfif"
            path.write_bytes(SIGNATURES[".jpg"])
            api = Mock()
            api.media_upload.side_effect = RuntimeError("upload failed")
            client = v2_client()

            with (
                patch.object(x_client, "X_ENABLED", True),
                patch.object(x_client, "_get_v1_api", return_value=api),
                patch.object(x_client, "_get_v2_client", return_value=client),
                self.assertLogs("rota-bot.x-client", level="ERROR"),
            ):
                result = x_client.post_tweet("Post", str(path))

            self.assertIsNone(result)
            upload_name = Path(api.media_upload.call_args.kwargs["filename"])
            self.assertFalse(upload_name.exists())
            client.create_tweet.assert_not_called()

    def test_missing_or_spoofed_requested_media_never_creates_text_only_tweet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = (
                Path(temp_dir) / "missing.mp4",
                Path(temp_dir) / "spoofed.mp4",
            )
            paths[1].write_bytes(SIGNATURES[".png"])

            for path in paths:
                with self.subTest(path=path):
                    client = v2_client()
                    with (
                        patch.object(x_client, "X_ENABLED", True),
                        patch.object(
                            x_client,
                            "_get_v2_client",
                            return_value=client,
                        ),
                        self.assertLogs(
                            "rota-bot.x-client",
                            level="ERROR",
                        ),
                    ):
                        result = x_client.post_tweet("Post", str(path))
                    self.assertIsNone(result)
                    client.create_tweet.assert_not_called()

    def test_no_media_preserves_text_only_posting(self):
        client = v2_client()
        with (
            patch.object(x_client, "X_ENABLED", True),
            patch.object(x_client, "_get_v2_client", return_value=client),
        ):
            result = x_client.post_tweet("Text only")
        self.assertIsNotNone(result)
        client.create_tweet.assert_called_once_with(
            text="Text only",
            media_ids=None,
        )

    def test_direct_call_over_25000_code_points_never_calls_x(self):
        api = Mock()
        client = v2_client()
        content = "😀" * (x_client.MAX_X_POST_CODEPOINTS + 1)

        with (
            patch.object(x_client, "X_ENABLED", True),
            patch.object(x_client, "_get_v1_api", return_value=api) as get_v1,
            patch.object(
                x_client,
                "_get_v2_client",
                return_value=client,
            ) as get_v2,
            self.assertLogs("rota-bot.x-client", level="ERROR") as logs,
        ):
            result = x_client.post_tweet(content)

        self.assertIsNone(result)
        get_v1.assert_not_called()
        get_v2.assert_not_called()
        client.create_tweet.assert_not_called()
        self.assertIn("25,000", logs.output[0])

    def test_create_id_succeeds_without_profile_lookup(self):
        client = v2_client()
        client.get_me.side_effect = RuntimeError("must not be called")
        with (
            patch.object(x_client, "X_ENABLED", True),
            patch.object(x_client, "_get_v2_client", return_value=client),
        ):
            result = x_client.post_tweet_result("Post")

        self.assertEqual(result.status, x_client.X_POST_SUCCESS)
        self.assertEqual(result.url, "https://x.com/i/web/status/456")
        client.get_me.assert_not_called()

    def test_malformed_success_response_is_unknown(self):
        for response in (
            SimpleNamespace(data=None),
            SimpleNamespace(data={}),
            SimpleNamespace(data={"id": ""}),
            SimpleNamespace(data={"id": "not-an-id"}),
            SimpleNamespace(data={"id": "1" * 31}),
        ):
            with self.subTest(response=response):
                client = v2_client()
                client.create_tweet.return_value = response
                with (
                    patch.object(x_client, "X_ENABLED", True),
                    patch.object(
                        x_client,
                        "_get_v2_client",
                        return_value=client,
                    ),
                    self.assertLogs(
                        "rota-bot.x-client",
                        level="ERROR",
                    ),
                ):
                    result = x_client.post_tweet_result("Post")
                self.assertEqual(result.status, x_client.X_POST_UNKNOWN)
                self.assertIsNone(result.url)
                client.get_me.assert_not_called()

    def test_create_exception_is_unknown_but_media_failure_is_known(self):
        client = v2_client()
        client.create_tweet.side_effect = TimeoutError("connection dropped")
        with (
            patch.object(x_client, "X_ENABLED", True),
            patch.object(x_client, "_get_v2_client", return_value=client),
            self.assertLogs("rota-bot.x-client", level="ERROR"),
        ):
            unknown = x_client.post_tweet_result("Post")
        self.assertEqual(unknown.status, x_client.X_POST_UNKNOWN)

        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.mp4"
            with (
                patch.object(x_client, "X_ENABLED", True),
                self.assertLogs("rota-bot.x-client", level="ERROR"),
            ):
                failed = x_client.post_tweet_result("Post", str(missing))
        self.assertEqual(failed.status, x_client.X_POST_FAILED)


if __name__ == "__main__":
    unittest.main()
