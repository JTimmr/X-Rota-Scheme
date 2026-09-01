import logging
import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import tweepy

from config import (
    X_ACCESS_TOKEN,
    X_ACCESS_TOKEN_SECRET,
    X_API_KEY,
    X_API_SECRET,
    X_ENABLED,
)
from media import NORMALIZED_MEDIA_EXTENSIONS, classify_stored_media

log = logging.getLogger("rota-bot.x-client")

CHUNKED_MEDIA_CATEGORIES = {
    "gif": "tweet_gif",
    "mp4": "tweet_video",
}
MEDIA_PROCESSING_TIMEOUT_SECONDS = 15 * 60
# X Premium supports long posts; this conservative direct-call guard is far
# above the Discord composer's 4,000-character cap and counts Unicode code points.
MAX_X_POST_CODEPOINTS = 25_000
X_POST_SUCCESS = "success"
X_POST_FAILED = "failed"
X_POST_UNKNOWN = "unknown"


@dataclass(frozen=True)
class XPostResult:
    status: str
    url: str | None = None
    detail: str | None = None


def _get_v1_api() -> tweepy.API:
    auth = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
    return tweepy.API(auth)


def _get_v2_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
    )


def _processing_info(media) -> dict | None:
    info = getattr(media, "processing_info", None)
    if info is None:
        return None
    if not isinstance(info, dict):
        raise RuntimeError("X returned malformed media processing information")
    return info


def _processing_error(info: dict) -> str:
    error = info.get("error")
    if isinstance(error, dict):
        return str(
            error.get("message")
            or error.get("name")
            or error.get("code")
            or error
        )
    return str(error or "X reported an unspecified media processing failure")


def _remaining_processing_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            "X media processing did not finish within 15 minutes"
        )
    return remaining


@contextmanager
def _bounded_api_timeout(api: tweepy.API, remaining: float):
    """Temporarily cap Tweepy's requests timeout to the overall deadline."""
    previous_timeout = api.timeout
    try:
        numeric_previous = float(previous_timeout)
    except (TypeError, ValueError):
        numeric_previous = remaining
    if not math.isfinite(numeric_previous) or numeric_previous <= 0:
        numeric_previous = remaining

    api.timeout = min(numeric_previous, remaining)
    try:
        yield
    finally:
        api.timeout = previous_timeout


def _wait_for_media_processing(api: tweepy.API, media):
    """Poll asynchronous X processing within a strict local deadline."""
    deadline = time.monotonic() + MEDIA_PROCESSING_TIMEOUT_SECONDS
    media_id = getattr(media, "media_id", None)
    if media_id is None:
        raise RuntimeError("X media upload returned no media_id")
    current = media
    while True:
        remaining = _remaining_processing_time(deadline)
        info = _processing_info(current)
        if info is None:
            return current

        state = str(info.get("state", "")).lower()
        if state == "succeeded":
            return current
        if state == "failed":
            raise RuntimeError(_processing_error(info))
        if state not in {"pending", "in_progress"}:
            raise RuntimeError(f"unexpected X media processing state: {state!r}")

        try:
            delay = float(info.get("check_after_secs", 1))
        except (TypeError, ValueError):
            delay = 1.0
        if not math.isfinite(delay) or delay <= 0:
            delay = 1.0
        time.sleep(min(delay, 30.0, remaining))
        remaining = _remaining_processing_time(deadline)
        with _bounded_api_timeout(api, remaining):
            status = api.get_media_upload_status(media_id)
            _remaining_processing_time(deadline)
        current = status


@contextmanager
def _canonical_upload_path(path: Path, media_format: str):
    """Give Tweepy a canonical extension for legacy stored image paths."""
    canonical_suffix = NORMALIZED_MEDIA_EXTENSIONS[media_format]
    if path.suffix.lower() == canonical_suffix:
        yield path
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".x-upload-",
        suffix=canonical_suffix,
        dir=path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copyfile(path, temporary_path)
        yield temporary_path
    finally:
        temporary_path.unlink(missing_ok=True)


def _upload_media(api: tweepy.API, path: Path, media_format: str):
    category = CHUNKED_MEDIA_CATEGORIES.get(media_format)
    if category is None:
        return api.media_upload(filename=str(path))

    media = api.media_upload(
        filename=str(path),
        chunked=True,
        media_category=category,
        wait_for_async_finalize=False,
    )
    return _wait_for_media_processing(api, media)


def post_tweet_result(
    content: str,
    image_path: str | None = None,
) -> XPostResult:
    """Post once and retain whether a non-success outcome is known or unknown."""
    if len(content) > MAX_X_POST_CODEPOINTS:
        log.error(
            "Refusing X post with %s Unicode code points; conservative limit is %s",
            f"{len(content):,}",
            f"{MAX_X_POST_CODEPOINTS:,}",
        )
        return XPostResult(
            X_POST_FAILED,
            detail="content exceeds the defensive Unicode code-point limit",
        )

    if not X_ENABLED:
        log.info("X posting disabled (credentials not set)")
        return XPostResult(X_POST_FAILED, detail="X credentials are disabled")

    media_ids = None
    if image_path:
        path = Path(image_path)
        if not path.is_file():
            log.error(
                "Media requested for tweet but stored file is missing: %s",
                path,
            )
            return XPostResult(X_POST_FAILED, detail="stored media file is missing")

        media_format = classify_stored_media(path)
        if media_format is None:
            log.error(
                "Refusing media upload because extension and contents do not "
                "identify the same supported format: %s",
                path,
            )
            return XPostResult(
                X_POST_FAILED,
                detail="stored media format could not be validated",
            )

        try:
            api_v1 = _get_v1_api()
            with _canonical_upload_path(path, media_format) as upload_path:
                uploaded_media = _upload_media(
                    api_v1,
                    upload_path,
                    media_format,
                )
            media_id = getattr(uploaded_media, "media_id", None)
            if media_id is None:
                raise RuntimeError("X media upload returned no media_id")
            media_ids = [media_id]
            log.info(
                "Uploaded %s media %s, media_id=%s",
                media_format,
                path.name,
                media_id,
            )
        except Exception:
            log.exception(
                "Failed to upload/process %s media %s; tweet was not created",
                media_format,
                path,
            )
            return XPostResult(
                X_POST_FAILED,
                detail="media upload or processing failed before tweet creation",
            )

    try:
        client = _get_v2_client()
    except Exception:
        log.exception("Failed to initialize X client; tweet was not created")
        return XPostResult(
            X_POST_FAILED,
            detail="X client initialization failed before tweet creation",
        )

    try:
        response = client.create_tweet(
            text=content,
            media_ids=media_ids,
        )
    except Exception:
        log.exception(
            "X create_tweet raised%s; outcome is unknown and must be checked",
            " after successful media upload" if media_ids else "",
        )
        return XPostResult(
            X_POST_UNKNOWN,
            detail="create_tweet raised after submission may have reached X",
        )

    data = getattr(response, "data", None)
    tweet_id = data.get("id") if isinstance(data, dict) else None
    tweet_id_text = str(tweet_id).strip() if tweet_id is not None else ""
    if (
        not tweet_id_text
        or not tweet_id_text.isascii()
        or not tweet_id_text.isdigit()
        or len(tweet_id_text) > 30
    ):
        log.error(
            "X create_tweet returned without a usable tweet ID; outcome is unknown"
        )
        return XPostResult(
            X_POST_UNKNOWN,
            detail="create_tweet returned no usable tweet ID",
        )

    tweet_url = f"https://x.com/i/web/status/{tweet_id_text}"
    log.info("Posted tweet %s: %s", tweet_id_text, tweet_url)
    return XPostResult(X_POST_SUCCESS, url=tweet_url)


def post_tweet(content: str, image_path: str | None = None) -> str | None:
    """Compatibility wrapper returning a URL only for confirmed success."""
    result = post_tweet_result(content, image_path)
    return result.url if result.status == X_POST_SUCCESS else None
