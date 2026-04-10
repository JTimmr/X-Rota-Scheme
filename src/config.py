import os


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DISCORD_TOKEN = require_env("DISCORD_TOKEN")
GUILD_ID = int(require_env("DISCORD_GUILD_ID"))
SCHEDULED_CHANNEL_ID = int(require_env("SCHEDULED_CHANNEL_ID"))
ARCHIVE_CHANNEL_ID = int(require_env("ARCHIVE_CHANNEL_ID"))
REMINDERS_CHANNEL_ID = int(require_env("REMINDERS_CHANNEL_ID"))

# X/Twitter API credentials (optional — if not set, X posting is disabled)
X_API_KEY = os.environ.get("X_API_KEY")
X_API_SECRET = os.environ.get("X_API_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET")

X_ENABLED = all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET])
