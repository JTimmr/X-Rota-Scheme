"""Validation and storage for the bot's single optional media attachment."""

import asyncio
import json
import logging
import math
import os
import subprocess
import uuid
import warnings
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

from database import IMAGES_DIR

log = logging.getLogger("rota-bot.media")

MIB = 1024 * 1024
STILL_IMAGE_MAX_BYTES = 5 * MIB
GIF_MAX_BYTES = 15 * MIB
VIDEO_MAX_BYTES = 512 * MIB
GIF_MAX_WIDTH = 1280
GIF_MAX_HEIGHT = 1080
GIF_MAX_FRAMES = 350
GIF_MAX_PIXEL_BUDGET = 300_000_000
# 40 MP permits normal high-resolution stills (including 6000x6000) while
# bounding memory use before Pillow decodes attacker-controlled pixels.
STILL_IMAGE_MAX_PIXELS = 40_000_000
VIDEO_MIN_DURATION = 0.5
VIDEO_MAX_DURATION = 140.0
VIDEO_MAX_FPS = 60.0
VIDEO_MIN_DIMENSION = 32
VIDEO_MAX_DIMENSION = 1920
VIDEO_MIN_ASPECT = 1 / 3
VIDEO_MAX_ASPECT = 3.0

MEDIA_EXTENSIONS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".gif": "gif",
    ".mp4": "mp4",
}
LEGACY_STORED_IMAGE_EXTENSIONS = {
    ".jfif": "jpeg",
}
MEDIA_CONTENT_TYPES = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "application/mp4": "mp4",
    # Discord can report valid iPhone-originated .mp4 files with an Apple MIME
    # type. Treat these as MP4 candidates; signature and ffprobe validation
    # below still reject actual MOV containers, HEVC, and other unsupported
    # content.
    "video/quicktime": "mp4",
    "video/x-m4v": "mp4",
}
GENERIC_CONTENT_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
}
NORMALIZED_MEDIA_EXTENSIONS = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
    "mp4": ".mp4",
}
IMAGE_FORMATS = {"jpeg", "png", "webp", "gif"}
COMPATIBLE_420_PIXEL_FORMATS = {"yuv420p", "yuvj420p"}

SUPPORTED_MEDIA_ERROR = (
    "That file type is not supported. Attach one JPG/JPEG, PNG, WebP, GIF, "
    "or MP4 video."
)
MEDIA_MISMATCH_ERROR = (
    "The file name, reported type, and actual contents do not match the same "
    "supported media format. Re-export the file with the correct extension."
)
MEDIA_SAVE_ERROR = "Failed to download or save the media. Please try again."
CORRUPT_IMAGE_ERROR = (
    "The image or GIF is corrupt or truncated. Re-export it and attach the "
    "new file."
)
IMAGE_PIXEL_LIMIT_ERROR = (
    "JPG, PNG, and WebP images may contain at most 40,000,000 decoded pixels "
    "(width × height × frames, where applicable). Resize or shorten the image "
    "and try again."
)
FFPROBE_MISSING_ERROR = (
    "Video validation is unavailable because ffprobe is not installed. "
    "Ask an administrator to install ffmpeg/ffprobe, then try again."
)
FFPROBE_INSPECTION_ERROR = (
    "Video validation could not inspect this file. Re-export it as an "
    "MP4 with H.264 video (and AAC audio, if used), then try again."
)


class MediaValidationError(ValueError):
    """A safe, actionable validation error suitable for a Discord response."""


class _DownloadTooLarge(Exception):
    pass


class _ProbeUnavailable(Exception):
    pass


class _ProbeFailed(Exception):
    pass


def media_size_error(media_format: str) -> str:
    if media_format == "gif":
        return "GIF files must be 15 MiB or smaller. Export a smaller GIF."
    if media_format == "mp4":
        return "MP4 videos must be 512 MiB or smaller. Export a smaller video."
    return (
        "JPG, PNG, and WebP images must be 5 MiB or smaller. "
        "Export a smaller image."
    )


def max_bytes_for_format(media_format: str) -> int:
    if media_format == "gif":
        return GIF_MAX_BYTES
    if media_format == "mp4":
        return VIDEO_MAX_BYTES
    return STILL_IMAGE_MAX_BYTES


def precheck_media_attachment(attachment: Any) -> str:
    """Validate advisory filename/MIME/size metadata and return its format."""
    suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()
    filename_format = MEDIA_EXTENSIONS.get(suffix)
    if suffix and filename_format is None:
        raise MediaValidationError(SUPPORTED_MEDIA_ERROR)

    content_type = (
        (getattr(attachment, "content_type", None) or "")
        .partition(";")[0]
        .strip()
        .lower()
    )
    if content_type in GENERIC_CONTENT_TYPES:
        content_type = ""
    mime_format = MEDIA_CONTENT_TYPES.get(content_type)
    if content_type and mime_format is None:
        raise MediaValidationError(SUPPORTED_MEDIA_ERROR)

    if filename_format and mime_format and filename_format != mime_format:
        raise MediaValidationError(MEDIA_MISMATCH_ERROR)

    expected_format = filename_format or mime_format
    if expected_format is None:
        raise MediaValidationError(SUPPORTED_MEDIA_ERROR)

    reported_size = getattr(attachment, "size", None)
    if reported_size is not None:
        try:
            reported_size = int(reported_size)
        except (TypeError, ValueError):
            reported_size = None
    if (
        reported_size is not None
        and reported_size > max_bytes_for_format(expected_format)
    ):
        raise MediaValidationError(media_size_error(expected_format))

    return expected_format


def detect_media_format(data: bytes) -> str | None:
    """Classify supported content from its file signature."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if (
        len(data) >= 12
        and data[4:8] == b"ftyp"
        and data[8:12].lower() != b"qt  "
        and not data[8:12].lower().startswith((b"3gp", b"3g2"))
    ):
        return "mp4"
    return None


def detect_image_format(data: bytes) -> str | None:
    """Backward-compatible image-only signature classifier."""
    media_format = detect_media_format(data)
    return media_format if media_format in IMAGE_FORMATS else None


def detect_file_format(path: str | Path) -> str | None:
    with Path(path).open("rb") as media_file:
        return detect_media_format(media_file.read(64))


def classify_stored_media(path: str | Path) -> str | None:
    """Classify a stored file only when extension and contents agree."""
    media_path = Path(path)
    try:
        detected_format = detect_file_format(media_path)
    except OSError:
        return None
    suffix = media_path.suffix.lower()
    suffix_format = MEDIA_EXTENSIONS.get(suffix)
    if suffix_format is None:
        suffix_format = LEGACY_STORED_IMAGE_EXTENSIONS.get(suffix)
    if suffix_format is None and not suffix and detected_format in IMAGE_FORMATS:
        return detected_format
    if suffix_format is None:
        return None
    return suffix_format if suffix_format == detected_format else None


def _validate_image_file(path: Path, expected_format: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            with Image.open(path) as image:
                detected_format = (image.format or "").lower()
                if detected_format == "jpg":
                    detected_format = "jpeg"
                if detected_format != expected_format:
                    raise MediaValidationError(MEDIA_MISMATCH_ERROR)
                width, height = image.size
                frame_count = getattr(image, "n_frames", 1)
                if (
                    expected_format != "gif"
                    and width * height * frame_count > STILL_IMAGE_MAX_PIXELS
                ):
                    raise MediaValidationError(IMAGE_PIXEL_LIMIT_ERROR)
                image.verify()

            # verify() checks structure but does not decode pixels. Reopen and
            # load every frame so truncated image data is rejected as well.
            with Image.open(path) as image:
                detected_format = (image.format or "").lower()
                if detected_format == "jpg":
                    detected_format = "jpeg"
                if detected_format != expected_format:
                    raise MediaValidationError(MEDIA_MISMATCH_ERROR)

                width, height = image.size
                frame_count = getattr(image, "n_frames", 1)
                if expected_format == "gif":
                    if width > GIF_MAX_WIDTH or height > GIF_MAX_HEIGHT:
                        raise MediaValidationError(
                            "GIF dimensions must be 1280x1080 or smaller. "
                            "Resize the GIF and try again."
                        )
                    if frame_count > GIF_MAX_FRAMES:
                        raise MediaValidationError(
                            "GIFs may contain at most 350 frames. "
                            "Shorten or lower the frame rate of the GIF."
                        )
                    if width * height * frame_count > GIF_MAX_PIXEL_BUDGET:
                        raise MediaValidationError(
                            "This GIF exceeds the 300,000,000 total-pixel budget "
                            "(width × height × frames). Reduce its size or frames."
                        )
                elif width * height * frame_count > STILL_IMAGE_MAX_PIXELS:
                    raise MediaValidationError(IMAGE_PIXEL_LIMIT_ERROR)

                for frame_index in range(frame_count):
                    image.seek(frame_index)
                    image.load()
    except MediaValidationError:
        raise
    except (
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise MediaValidationError(IMAGE_PIXEL_LIMIT_ERROR) from exc
    except (
        UnidentifiedImageError,
        OSError,
        EOFError,
        IndexError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise MediaValidationError(CORRUPT_IMAGE_ERROR) from exc


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _frame_rates(stream: dict[str, Any]) -> list[float]:
    rates = []
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if not value:
            continue
        try:
            rate = float(Fraction(str(value)))
        except (ValueError, ZeroDivisionError):
            continue
        if math.isfinite(rate) and rate > 0:
            rates.append(rate)
    return rates


def _video_duration(
    probe_data: dict[str, Any],
    video_stream: dict[str, Any],
) -> float | None:
    format_data = probe_data.get("format")
    if isinstance(format_data, dict):
        duration = _positive_number(format_data.get("duration"))
        if duration is not None:
            return duration
    return _positive_number(video_stream.get("duration"))


def _parse_aspect_ratio(value: Any, field_name: str) -> Fraction:
    try:
        parts = str(value).strip().split(":")
        if len(parts) != 2:
            raise ValueError
        numerator = Fraction(parts[0])
        denominator = Fraction(parts[1])
        if numerator <= 0 or denominator <= 0:
            raise ValueError
        return numerator / denominator
    except (ValueError, ZeroDivisionError) as exc:
        raise MediaValidationError(
            f"The MP4 {field_name} is malformed or nonpositive. "
            "Re-export the video with a valid display aspect ratio."
        ) from exc


def _effective_aspect_ratio(
    video_stream: dict[str, Any],
    width: int,
    height: int,
) -> Fraction:
    display_aspect = video_stream.get("display_aspect_ratio")
    if display_aspect is not None:
        return _parse_aspect_ratio(
            display_aspect,
            "display_aspect_ratio",
        )

    sample_aspect = video_stream.get("sample_aspect_ratio")
    if sample_aspect is not None:
        return Fraction(width, height) * _parse_aspect_ratio(
            sample_aspect,
            "sample_aspect_ratio",
        )
    return Fraction(width, height)


def validate_ffprobe_result(probe_data: dict[str, Any]) -> None:
    """Validate ffprobe JSON against the conservative X MP4 profile."""
    format_data = probe_data.get("format")
    if not isinstance(format_data, dict):
        raise MediaValidationError(
            "The video container could not be identified. Export it as MP4."
        )

    format_names = {
        name.strip().lower()
        for name in str(format_data.get("format_name", "")).split(",")
    }
    tags = format_data.get("tags")
    major_brand = (
        str(tags.get("major_brand", "")).strip().lower()
        if isinstance(tags, dict)
        else ""
    )
    if (
        "mp4" not in format_names
        or major_brand == "qt"
        or major_brand.startswith(("3gp", "3g2"))
    ):
        raise MediaValidationError(
            "The video must use an MP4 container, not MOV or another format. "
            "Export it as MP4."
        )

    streams = probe_data.get("streams")
    if not isinstance(streams, list):
        streams = []
    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise MediaValidationError(
            "The MP4 must contain a video track. Export it with H.264 video."
        )
    if any(stream.get("codec_name") != "h264" for stream in video_streams):
        raise MediaValidationError(
            "The MP4 video codec must be H.264. Re-export the video using H.264."
        )
    video_stream = video_streams[0]

    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if any(stream.get("codec_name") != "aac" for stream in audio_streams):
        raise MediaValidationError(
            "MP4 audio must use AAC. Re-export with AAC audio or remove audio."
        )

    duration = _video_duration(probe_data, video_stream)
    if duration is None:
        raise MediaValidationError(
            "The MP4 duration could not be determined. Re-export the video."
        )
    if duration < VIDEO_MIN_DURATION or duration > VIDEO_MAX_DURATION:
        raise MediaValidationError(
            "MP4 duration must be between 0.5 and 140 seconds. "
            "Trim the video and try again."
        )

    frame_rates = _frame_rates(video_stream)
    if not frame_rates:
        raise MediaValidationError(
            "The MP4 frame rate could not be determined. Re-export at 60 fps "
            "or lower."
        )
    if any(fps > VIDEO_MAX_FPS for fps in frame_rates):
        raise MediaValidationError(
            "MP4 frame rate must be 60 fps or lower. Re-export at 60 fps or "
            "lower."
        )

    width = video_stream.get("width")
    height = video_stream.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
    ):
        raise MediaValidationError(
            "The MP4 dimensions could not be determined. Re-export the video."
        )
    if width < VIDEO_MIN_DIMENSION or height < VIDEO_MIN_DIMENSION:
        raise MediaValidationError(
            "MP4 dimensions must be at least 32x32 pixels. "
            "Export a larger video."
        )
    if max(width, height) > VIDEO_MAX_DIMENSION:
        raise MediaValidationError(
            "MP4 width and height must each be 1920 pixels or less. "
            "Resize the video and try again."
        )

    aspect_ratio = _effective_aspect_ratio(video_stream, width, height)
    if not Fraction(1, 3) <= aspect_ratio <= Fraction(3, 1):
        raise MediaValidationError(
            "MP4 effective display aspect ratio must be between 1:3 and 3:1. "
            "Crop or pad the video and try again."
        )

    pixel_format = video_stream.get("pix_fmt")
    if pixel_format and pixel_format not in COMPATIBLE_420_PIXEL_FORMATS:
        raise MediaValidationError(
            "MP4 video must use a broadly compatible YUV 4:2:0 pixel format "
            "(yuv420p). Re-export with yuv420p."
        )


def _run_ffprobe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _ProbeUnavailable from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _ProbeFailed(str(exc)) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffprobe error").strip()
        raise _ProbeFailed(detail)
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _ProbeFailed("ffprobe returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise _ProbeFailed("ffprobe returned an unexpected result")
    return data


async def probe_and_validate_mp4(path: str | Path) -> None:
    media_path = Path(path)
    try:
        probe_data = await asyncio.to_thread(_run_ffprobe, media_path)
    except _ProbeUnavailable as exc:
        log.error("Cannot validate MP4 %s: ffprobe is missing", media_path)
        raise MediaValidationError(FFPROBE_MISSING_ERROR) from exc
    except _ProbeFailed as exc:
        log.error("ffprobe failed for MP4 %s: %s", media_path, exc)
        raise MediaValidationError(FFPROBE_INSPECTION_ERROR) from exc
    validate_ffprobe_result(probe_data)


def _download_url_to_file(url: str, path: Path, max_bytes: int) -> None:
    request = Request(url, headers={"User-Agent": "x-rota-bot/1.0"})
    with urlopen(request, timeout=60) as response, path.open("wb") as output:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise _DownloadTooLarge
            except ValueError:
                pass

        downloaded = 0
        while True:
            chunk = response.read(MIB)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise _DownloadTooLarge
            output.write(chunk)


async def _save_attachment_to_temp(
    attachment: Any,
    path: Path,
    max_bytes: int,
) -> None:
    url = getattr(attachment, "url", None)
    if url:
        await asyncio.to_thread(
            _download_url_to_file,
            str(url),
            path,
            max_bytes,
        )
        return

    # Real Discord attachments always expose a CDN URL. This fallback keeps the
    # helper usable with test doubles and alternate Attachment implementations.
    save = getattr(attachment, "save", None)
    if save is not None:
        await save(path)
        return

    read = getattr(attachment, "read", None)
    if read is None:
        raise OSError("attachment exposes neither a URL, save(), nor read()")
    data = await read()
    if len(data) > max_bytes:
        raise _DownloadTooLarge
    await asyncio.to_thread(path.write_bytes, data)


async def validate_media_file(path: str | Path, expected_format: str) -> None:
    media_path = Path(path)
    actual_size = await asyncio.to_thread(lambda: media_path.stat().st_size)
    if actual_size > max_bytes_for_format(expected_format):
        raise MediaValidationError(media_size_error(expected_format))

    detected_format = await asyncio.to_thread(detect_file_format, media_path)
    if detected_format != expected_format:
        raise MediaValidationError(MEDIA_MISMATCH_ERROR)

    if expected_format in IMAGE_FORMATS:
        await asyncio.to_thread(
            _validate_image_file,
            media_path,
            expected_format,
        )
    else:
        await probe_and_validate_mp4(media_path)


async def save_validated_media_attachment(
    attachment: Any,
    *,
    storage_dir: str | Path = IMAGES_DIR,
) -> tuple[str | None, str | None]:
    """Stream, validate, and atomically store one supported attachment."""
    try:
        expected_format = precheck_media_attachment(attachment)
    except MediaValidationError as exc:
        return None, str(exc)

    directory = Path(storage_dir)
    temp_path = directory / f".{uuid.uuid4().hex}.upload"
    try:
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        await _save_attachment_to_temp(
            attachment,
            temp_path,
            max_bytes_for_format(expected_format),
        )
        await validate_media_file(temp_path, expected_format)

        final_path = directory / (
            f"{uuid.uuid4().hex}{NORMALIZED_MEDIA_EXTENSIONS[expected_format]}"
        )
        await asyncio.to_thread(os.replace, temp_path, final_path)
        return str(final_path), None
    except _DownloadTooLarge:
        return None, media_size_error(expected_format)
    except MediaValidationError as exc:
        log.warning(
            "Rejected media attachment %r: %s",
            getattr(attachment, "filename", None),
            exc,
        )
        return None, str(exc)
    except Exception:
        log.exception(
            "Failed to download or save media attachment %r",
            getattr(attachment, "filename", None),
        )
        return None, MEDIA_SAVE_ERROR
    finally:
        if temp_path.exists():
            try:
                await asyncio.to_thread(temp_path.unlink)
            except OSError:
                log.exception("Failed to remove temporary media file %s", temp_path)
