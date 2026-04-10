import logging
from pathlib import Path

import tweepy

from config import X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_ENABLED

log = logging.getLogger("rota-bot.x-client")


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


def post_tweet(content: str, image_path: str | None = None) -> str | None:
    """Post a tweet and return its URL, or None on failure.

    Uses v1.1 API for media upload and v2 API for tweet creation.
    Runs synchronously — call from an executor in async code.
    """
    if not X_ENABLED:
        log.info("X posting disabled (credentials not set)")
        return None

    try:
        media_ids = []
        if image_path:
            p = Path(image_path)
            if p.exists():
                api_v1 = _get_v1_api()
                media = api_v1.media_upload(filename=str(p))
                media_ids.append(media.media_id)
                log.info(f"Uploaded media {p.name}, media_id={media.media_id}")

        client = _get_v2_client()
        response = client.create_tweet(
            text=content,
            media_ids=media_ids if media_ids else None,
        )

        tweet_id = response.data["id"]
        # Fetch the authenticated user's username for the URL
        me = client.get_me()
        username = me.data.username
        tweet_url = f"https://x.com/{username}/status/{tweet_id}"

        log.info(f"Posted tweet {tweet_id}: {tweet_url}")
        return tweet_url

    except tweepy.TweepyException:
        log.exception("Failed to post tweet")
        return None
