import os

from dotenv import load_dotenv


load_dotenv()


def parse_int_env(name: str, *, default: int | None = None) -> int | None:
    """Parse an environment variable as int, honoring an optional default."""
    from os import getenv

    value = getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer chat id") from exc


# Temporal helpers keep derived constants easy to scan below.
SECONDS_IN_HOUR = 3600
SECONDS_IN_MINUTE = 60

CHANNEL_ID = parse_int_env("TG_CHANNEL_ID")
DISCUSSION_ID = parse_int_env("TG_DISCUSSION_ID")

PENDING_REDIS_URL = os.getenv("PENDING_REDIS_URL")
PENDING_REDIS_PREFIX = "tgnews:pending"
PENDING_REDIS_TTL = 15 * SECONDS_IN_MINUTE

STAGING_REDIS_URL = os.getenv("STAGING_REDIS_URL")
STAGING_REDIS_PREFIX = "tgnews:staged"
STAGING_REDIS_INDEX_KEY = f"{STAGING_REDIS_PREFIX}:index"
STAGING_REDIS_PUBLISHED_KEY = "tgnews:published"

LATEST_ARTICLE_COUNT = 3
WORKER_ARTICLE_LIMIT = 15

ARTICLE_MAX_WAIT_SECONDS = 5 * SECONDS_IN_HOUR
ARTICLE_METRIC_REFRESH_SECONDS = 10 * SECONDS_IN_MINUTE

ARTICLE_MIN_POINTS = 50
ARTICLE_MIN_COMMENTS = 50
ARTICLE_AGE_FLOOR_POINTS = 10
ARTICLE_AGE_FLOOR_COMMENTS = 10
