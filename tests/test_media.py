import copy
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import media


def image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (8, 8),
    frames: int = 1,
) -> bytes:
    output = io.BytesIO()
    images = [Image.new("RGB", size, (index % 255, 0, 0)) for index in range(frames)]
    save_kwargs = {}
    if frames > 1:
        save_kwargs = {
            "save_all": True,
            "append_images": images[1:],
            "duration": 20,
            "loop": 0,
        }
    images[0].save(output, format=image_format, **save_kwargs)
    return output.getvalue()


def attachment(
    filename: str,
    content_type: str | None,
    data: bytes,
    *,
    reported_size: int | None = None,
):
    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        size=len(data) if reported_size is None else reported_size,
        read=AsyncMock(return_value=data),
    )


def valid_probe(*, audio: bool = False) -> dict:
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "60/1",
            "duration": "140.0",
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "140.0",
            "tags": {"major_brand": "isom"},
        },
        "streams": streams,
    }


class MediaMetadataTests(unittest.TestCase):
    def test_supported_formats_and_mime_absent_file(self):
        cases = (
            ("photo.jpeg", "image/jpeg", "jpeg"),
            ("photo.PNG", None, "png"),
            ("photo.webp", "application/octet-stream", "webp"),
            ("animation.gif", "image/gif", "gif"),
            ("video.mp4", "video/mp4; charset=binary", "mp4"),
            ("iphone.mp4", "video/quicktime", "mp4"),
            ("iphone-export.mp4", "video/x-m4v", "mp4"),
        )
        for filename, content_type, expected in cases:
            with self.subTest(filename=filename):
                candidate = SimpleNamespace(
                    filename=filename,
                    content_type=content_type,
                    size=1,
                )
                self.assertEqual(
                    media.precheck_media_attachment(candidate),
                    expected,
                )

    def test_unsupported_and_metadata_mismatches_are_rejected(self):
        cases = (
            ("payload.exe", "image/png"),
            ("photo.jpg", "image/png"),
            ("photo.png", "video/mp4"),
            ("clip.mov", "video/quicktime"),
            ("no-extension", None),
        )
        for filename, content_type in cases:
            with self.subTest(filename=filename), self.assertRaises(
                media.MediaValidationError
            ):
                media.precheck_media_attachment(
                    SimpleNamespace(
                        filename=filename,
                        content_type=content_type,
                        size=1,
                    )
                )

    def test_reported_size_is_checked_before_download(self):
        candidate = SimpleNamespace(
            filename="large.mp4",
            content_type="video/mp4",
            size=media.VIDEO_MAX_BYTES + 1,
            read=AsyncMock(),
        )
        with self.assertRaisesRegex(media.MediaValidationError, "512 MiB"):
            media.precheck_media_attachment(candidate)
        candidate.read.assert_not_called()

    def test_each_file_size_boundary_is_inclusive(self):
        cases = (
            ("image.jpg", "image/jpeg", media.STILL_IMAGE_MAX_BYTES),
            ("animation.gif", "image/gif", media.GIF_MAX_BYTES),
            ("video.mp4", "video/mp4", media.VIDEO_MAX_BYTES),
        )
        for filename, content_type, limit in cases:
            with self.subTest(filename=filename):
                accepted = SimpleNamespace(
                    filename=filename,
                    content_type=content_type,
                    size=limit,
                )
                media.precheck_media_attachment(accepted)

                rejected = SimpleNamespace(
                    filename=filename,
                    content_type=content_type,
                    size=limit + 1,
                )
                with self.assertRaises(media.MediaValidationError):
                    media.precheck_media_attachment(rejected)

    def test_signatures_and_stored_classification_must_match_extension(self):
        signatures = {
            "jpeg": b"\xff\xd8\xff\xe0jpeg",
            "png": b"\x89PNG\r\n\x1a\npng",
            "webp": b"RIFF\x04\x00\x00\x00WEBPdata",
            "gif": b"GIF89agif",
            "mp4": b"\x00\x00\x00\x18ftypisommp4",
        }
        for expected, signature in signatures.items():
            with self.subTest(expected=expected):
                self.assertEqual(media.detect_media_format(signature), expected)

        with tempfile.TemporaryDirectory() as temp_dir:
            valid = Path(temp_dir) / "legacy.jpeg"
            valid.write_bytes(signatures["jpeg"])
            self.assertEqual(media.classify_stored_media(valid), "jpeg")

            spoofed = Path(temp_dir) / "spoofed.gif"
            spoofed.write_bytes(signatures["png"])
            self.assertIsNone(media.classify_stored_media(spoofed))


class MediaStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_formats_save_with_normalized_names(self):
        cases = (
            ("upload.JPEG", None, image_bytes("JPEG"), ".jpg"),
            ("upload.PNG", "image/png", image_bytes("PNG"), ".png"),
            ("upload.WEBP", "image/webp", image_bytes("WEBP"), ".webp"),
            ("upload.GIF", "image/gif", image_bytes("GIF", frames=2), ".gif"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for filename, content_type, data, extension in cases:
                with self.subTest(filename=filename):
                    saved, error = await media.save_validated_media_attachment(
                        attachment(filename, content_type, data),
                        storage_dir=temp_dir,
                    )
                    self.assertIsNone(error)
                    self.assertEqual(Path(saved).suffix, extension)
                    self.assertEqual(Path(saved).read_bytes(), data)
                    Path(saved).unlink()

    async def test_corrupt_truncated_and_spoofed_files_are_cleaned_up(self):
        valid_png = image_bytes("PNG", size=(64, 64))
        cases = (
            ("corrupt.png", "image/png", valid_png[:30]),
            ("spoofed.png", "image/png", b"MZ executable"),
            ("mismatch.jpg", "image/jpeg", valid_png),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for filename, content_type, data in cases:
                with self.subTest(filename=filename):
                    saved, error = await media.save_validated_media_attachment(
                        attachment(filename, content_type, data),
                        storage_dir=temp_dir,
                    )
                    self.assertIsNone(saved)
                    self.assertIsNotNone(error)
                    self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_actual_size_over_limit_is_removed(self):
        candidate = attachment(
            "large.png",
            "image/png",
            b"x" * 20,
            reported_size=1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(media, "STILL_IMAGE_MAX_BYTES", 10):
                saved, error = await media.save_validated_media_attachment(
                    candidate,
                    storage_dir=temp_dir,
                )
            self.assertIsNone(saved)
            self.assertIn("5 MiB", error)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_probe_validation_failure_removes_temporary_mp4(self):
        candidate = attachment(
            "clip.mp4",
            None,
            b"\x00\x00\x00\x18ftypisommp4",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                media,
                "probe_and_validate_mp4",
                new=AsyncMock(
                    side_effect=media.MediaValidationError(
                        media.FFPROBE_INSPECTION_ERROR
                    )
                ),
            ):
                saved, error = await media.save_validated_media_attachment(
                    candidate,
                    storage_dir=temp_dir,
                )
            self.assertIsNone(saved)
            self.assertIn("could not inspect", error)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_iphone_mime_mp4_reaches_content_validation_and_is_saved(self):
        video_data = b"\x00\x00\x00\x18ftypisommp4"
        candidate = attachment(
            "iphone.mp4",
            "video/quicktime",
            video_data,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                media,
                "probe_and_validate_mp4",
                new=AsyncMock(),
            ) as validate_mp4:
                saved, error = await media.save_validated_media_attachment(
                    candidate,
                    storage_dir=temp_dir,
                )

            self.assertIsNone(error)
            self.assertEqual(Path(saved).suffix, ".mp4")
            self.assertEqual(Path(saved).read_bytes(), video_data)
            validate_mp4.assert_awaited_once()

    async def test_download_failure_removes_temporary_file(self):
        candidate = attachment("image.png", "image/png", b"unused")
        candidate.read.side_effect = OSError("download failed")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertLogs("rota-bot.media", level="ERROR"):
                saved, error = await media.save_validated_media_attachment(
                    candidate,
                    storage_dir=temp_dir,
                )
            self.assertIsNone(saved)
            self.assertEqual(error, media.MEDIA_SAVE_ERROR)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_highly_compressed_png_over_decoded_pixel_cap_is_rejected(self):
        output = io.BytesIO()
        Image.new("1", (10_000, 4_001)).save(output, format="PNG")
        png_data = output.getvalue()
        self.assertLess(len(png_data), media.STILL_IMAGE_MAX_BYTES)

        with tempfile.TemporaryDirectory() as temp_dir:
            saved, error = await media.save_validated_media_attachment(
                attachment("oversized.png", "image/png", png_data),
                storage_dir=temp_dir,
            )
            self.assertIsNone(saved)
            self.assertIn("40,000,000 decoded pixels", error)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_pillow_decompression_warning_is_treated_as_error(self):
        png_data = image_bytes("PNG", size=(11, 10))
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(Image, "MAX_IMAGE_PIXELS", 100):
                saved, error = await media.save_validated_media_attachment(
                    attachment("warning.png", "image/png", png_data),
                    storage_dir=temp_dir,
                )
            self.assertIsNone(saved)
            self.assertEqual(error, media.IMAGE_PIXEL_LIMIT_ERROR)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_truncated_gif_index_error_is_specific_and_cleaned_up(self):
        opened = [
            FakeImage((8, 8), 1),
            FakeImage((8, 8), 1, load_error=IndexError("truncated")),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(media.Image, "open", side_effect=opened):
                saved, error = await media.save_validated_media_attachment(
                    attachment(
                        "truncated.gif",
                        "image/gif",
                        b"GIF89a incomplete frame data",
                    ),
                    storage_dir=temp_dir,
                )
            self.assertIsNone(saved)
            self.assertEqual(error, media.CORRUPT_IMAGE_ERROR)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])


class FakeImage:
    format = "GIF"

    def __init__(
        self,
        size: tuple[int, int],
        frame_count: int,
        *,
        load_error: Exception | None = None,
    ):
        self.size = size
        self.n_frames = frame_count
        self.load_error = load_error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def verify(self):
        return None

    def seek(self, _index):
        return None

    def load(self):
        if self.load_error:
            raise self.load_error
        return None


class GifLimitTests(unittest.TestCase):
    def assert_gif_rejected(
        self,
        size: tuple[int, int],
        frames: int,
        criterion: str,
    ):
        opened = [FakeImage(size, frames), FakeImage(size, frames)]
        with (
            patch.object(media.Image, "open", side_effect=opened),
            self.assertRaisesRegex(media.MediaValidationError, criterion),
        ):
            media._validate_image_file(Path("unused.gif"), "gif")

    def test_dimensions_limit(self):
        self.assert_gif_rejected((1281, 100), 1, "1280x1080")
        self.assert_gif_rejected((100, 1081), 1, "1280x1080")

    def test_frame_limit(self):
        self.assert_gif_rejected((10, 10), 351, "350 frames")

    def test_total_pixel_budget(self):
        self.assert_gif_rejected(
            (1000, 1000),
            301,
            "300,000,000",
        )


class FfprobeResultTests(unittest.TestCase):
    def test_valid_no_audio_and_aac_audio_mp4(self):
        media.validate_ffprobe_result(valid_probe())
        media.validate_ffprobe_result(valid_probe(audio=True))
        portrait = valid_probe()
        portrait["streams"][0]["width"] = 1080
        portrait["streams"][0]["height"] = 1920
        media.validate_ffprobe_result(portrait)

    def test_each_video_criterion_is_rejected(self):
        cases = []

        not_mp4 = valid_probe()
        not_mp4["format"]["format_name"] = "matroska,webm"
        cases.append((not_mp4, "MP4 container"))

        three_gp = valid_probe()
        three_gp["format"]["tags"]["major_brand"] = "3gp6"
        cases.append((three_gp, "MP4 container"))

        bad_video_codec = valid_probe()
        bad_video_codec["streams"][0]["codec_name"] = "hevc"
        cases.append((bad_video_codec, "H.264"))

        bad_audio_codec = valid_probe(audio=True)
        bad_audio_codec["streams"][1]["codec_name"] = "mp3"
        cases.append((bad_audio_codec, "AAC"))

        for duration in ("0.49", "140.01"):
            bad_duration = valid_probe()
            bad_duration["format"]["duration"] = duration
            cases.append((bad_duration, "0.5 and 140"))

        bad_fps = valid_probe()
        bad_fps["streams"][0]["avg_frame_rate"] = "60001/1000"
        cases.append((bad_fps, "60 fps"))

        bad_nominal_fps = valid_probe()
        bad_nominal_fps["streams"][0]["avg_frame_rate"] = "30/1"
        bad_nominal_fps["streams"][0]["r_frame_rate"] = "120/1"
        cases.append((bad_nominal_fps, "60 fps"))

        too_small = valid_probe()
        too_small["streams"][0]["width"] = 31
        cases.append((too_small, "at least 32x32"))

        too_large = valid_probe()
        too_large["streams"][0]["width"] = 1921
        cases.append((too_large, "1920"))

        bad_aspect = valid_probe()
        bad_aspect["streams"][0]["width"] = 32
        bad_aspect["streams"][0]["height"] = 100
        cases.append((bad_aspect, "1:3 and 3:1"))

        bad_pixel_format = valid_probe()
        bad_pixel_format["streams"][0]["pix_fmt"] = "yuv444p"
        cases.append((bad_pixel_format, "YUV 4:2:0"))

        for probe_data, criterion in cases:
            with self.subTest(criterion=criterion), self.assertRaisesRegex(
                media.MediaValidationError,
                criterion,
            ):
                media.validate_ffprobe_result(copy.deepcopy(probe_data))

    def test_absent_pixel_format_is_allowed(self):
        probe_data = valid_probe()
        del probe_data["streams"][0]["pix_fmt"]
        media.validate_ffprobe_result(probe_data)

    def test_display_aspect_ratio_takes_precedence_and_is_validated(self):
        valid_display = valid_probe()
        valid_display["streams"][0]["display_aspect_ratio"] = "3:1"
        valid_display["streams"][0]["sample_aspect_ratio"] = "malformed"
        media.validate_ffprobe_result(valid_display)

        for value in ("4:1", "malformed", "0:1", "-1:1"):
            probe_data = valid_probe()
            probe_data["streams"][0]["display_aspect_ratio"] = value
            with self.subTest(value=value), self.assertRaises(
                media.MediaValidationError
            ):
                media.validate_ffprobe_result(probe_data)

    def test_sample_aspect_ratio_is_applied_when_display_ratio_absent(self):
        valid_sample = valid_probe()
        valid_sample["streams"][0]["width"] = 720
        valid_sample["streams"][0]["height"] = 1080
        valid_sample["streams"][0]["sample_aspect_ratio"] = "1:2"
        media.validate_ffprobe_result(valid_sample)

        outside = valid_probe()
        outside["streams"][0]["sample_aspect_ratio"] = "2:1"
        with self.assertRaisesRegex(
            media.MediaValidationError,
            "effective display aspect ratio",
        ):
            media.validate_ffprobe_result(outside)

        for value in ("bad", "1:0", "-1:1"):
            malformed = valid_probe()
            malformed["streams"][0]["sample_aspect_ratio"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                media.MediaValidationError,
                "sample_aspect_ratio",
            ):
                media.validate_ffprobe_result(malformed)


class FfprobeOperationalTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_ffprobe_fails_safely(self):
        with patch.object(
            media,
            "_run_ffprobe",
            side_effect=media._ProbeUnavailable(),
        ):
            with self.assertRaisesRegex(
                media.MediaValidationError,
                "not installed",
            ):
                await media.probe_and_validate_mp4("clip.mp4")

    async def test_probe_failure_fails_safely(self):
        with patch.object(
            media,
            "_run_ffprobe",
            side_effect=media._ProbeFailed("invalid data"),
        ):
            with self.assertRaisesRegex(
                media.MediaValidationError,
                "could not inspect",
            ):
                await media.probe_and_validate_mp4("clip.mp4")

    async def test_probe_runs_off_event_loop(self):
        probe_data = valid_probe()
        event_loop_thread = threading.get_ident()
        probe_thread = None

        def probe(_path):
            nonlocal probe_thread
            probe_thread = threading.get_ident()
            return probe_data

        with patch.object(media, "_run_ffprobe", side_effect=probe) as run_probe:
            await media.probe_and_validate_mp4("clip.mp4")
        run_probe.assert_called_once()
        self.assertNotEqual(probe_thread, event_loop_thread)


if __name__ == "__main__":
    unittest.main()
