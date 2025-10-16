"""Redis-backed pending post storage for channel → discussion bridging."""

# We stage channel message metadata in Redis so that the bot process can
# locate the auto-forwarded copy in the discussion chat and drop the first
# comment exactly once, even if the publish and consume happen in different
# processes.

import json
from dataclasses import asdict, dataclass
from typing import Optional

import redis
from redis.exceptions import RedisError

from config import PENDING_REDIS_PREFIX, PENDING_REDIS_TTL, PENDING_REDIS_URL
from content.reader import Article
from utils import logger


@dataclass(frozen=True)
class PendingPost:
    """Captured channel post metadata waiting for its auto-forward."""

    article: Article
    discussion_chat_id: Optional[int]


_redis_client: Optional[redis.Redis] = None


def _get_client() -> redis.Redis:
    """Lazily initialize the Redis connection used for pending posts."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(PENDING_REDIS_URL)
    return _redis_client


def _key(channel_post_id: int) -> str:
    """Build the Redis key for a pending channel post entry."""
    return f"{PENDING_REDIS_PREFIX}:{channel_post_id}"


def store_pending_post(
    channel_post_id: int,
    article: Article,
    discussion_chat_id: Optional[int],
) -> bool:
    """Persist a channel post payload so the bot can reconcile the auto-forward."""
    # Persist the headline payload keyed by channel message id; this is the
    # only authoritative linkage between the publish path and the auto-forward
    # handler that eventually posts the threaded comment.
    client = _get_client()
    payload = {
        "article": asdict(article),
        "discussion_chat_id": discussion_chat_id,
    }
    ttl = PENDING_REDIS_TTL or None
    try:
        client.set(_key(channel_post_id), json.dumps(payload, ensure_ascii=False), ex=ttl)
    except RedisError as err:
        logger.error(
            "Pending store write failed",
            channel_post_id=channel_post_id,
            discussion_chat_id=discussion_chat_id,
            article_id=article.article_id,
            error=str(err),
        )
        return False

    logger.info(
        "Pending store write",
        channel_post_id=channel_post_id,
        discussion_chat_id=discussion_chat_id,
        article_id=article.article_id,
        ttl_seconds=ttl,
    )
    return True


def pop_pending_post(channel_post_id: int) -> Optional[PendingPost]:
    """Retrieve and delete the pending entry for an auto-forwarded channel post."""
    # Atomically claim the pending record when the auto-forward arrives so
    # retries never post more than once.
    client = _get_client()
    key = _key(channel_post_id)
    try:
        raw_value = client.get(key)
    except RedisError as err:
        logger.error(
            "Pending store read failed",
            channel_post_id=channel_post_id,
            error=str(err),
        )
        return None

    if raw_value is None:
        logger.info("Pending store miss", channel_post_id=channel_post_id)
        return None

    try:
        client.delete(key)
    except RedisError as err:
        logger.warning(
            "Pending store delete failed",
            channel_post_id=channel_post_id,
            error=str(err),
        )

    try:
        payload = json.loads(raw_value if isinstance(raw_value, str) else raw_value.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        logger.error(
            "Pending store payload decode failed",
            channel_post_id=channel_post_id,
            error=str(err),
        )
        return None

    article_payload = payload.get("article") or {}
    if not article_payload:
        logger.warning(
            "Pending store payload missing article",
            channel_post_id=channel_post_id,
        )
        return None

    references = article_payload.get("references") or []
    # Stored references are lists for JSON; convert back to tuples for dataclass construction.
    article_payload["references"] = [tuple(item) for item in references]

    try:
        article = Article(**article_payload)
    except TypeError as err:
        logger.error(
            "Pending store article reconstruction failed",
            channel_post_id=channel_post_id,
            error=str(err),
        )
        return None

    discussion_chat_id = payload.get("discussion_chat_id")
    logger.info(
        "Pending store hit",
        channel_post_id=channel_post_id,
        discussion_chat_id=discussion_chat_id,
        article_id=article.article_id,
    )
    return PendingPost(article=article, discussion_chat_id=discussion_chat_id)
