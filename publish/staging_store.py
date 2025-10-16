"""Redis-backed staging store for Hacker News articles awaiting publication."""

# The worker uses this module to coordinate the explore/exploit loop: stage
# top stories, refresh their engagement, and graduate or drop them without
# any shared in-memory state.

import json
import time
from dataclasses import asdict, dataclass, replace
from typing import Iterable, List, Optional

import redis
from redis.exceptions import RedisError

from config import (
    STAGING_REDIS_PUBLISHED_KEY,
    STAGING_REDIS_INDEX_KEY,
    STAGING_REDIS_PREFIX,
    STAGING_REDIS_URL,
)
from content.reader import Article
from utils import logger


@dataclass(frozen=True)
class StagedArticle:
    article: Article
    staged_at: float
    last_checked_ts: float


_redis_client: Optional[redis.Redis] = None


def _get_client() -> redis.Redis:
    """Instantiate the staging Redis client on first use."""
    global _redis_client
    if _redis_client is None:
        # Lazy init keeps import-time side effects minimal for processes that never touch Redis.
        _redis_client = redis.Redis.from_url(STAGING_REDIS_URL)
    return _redis_client


def _normalize_article_id(value: int | str) -> str:
    """Normalize ids to strings so Redis keys remain consistent."""
    return str(value)


def _stage_key(article_id: str) -> str:
    """Construct the primary staging key for a given article id."""
    return f"{STAGING_REDIS_PREFIX}:{article_id}"


def _serialize_article(article: Article) -> dict:
    """Convert an Article dataclass into a Redis-friendly dict payload."""
    payload = asdict(article)
    payload["references"] = [list(item) for item in payload.get("references", [])]
    return payload


def _deserialize_article(payload: dict) -> Article | None:
    """Rebuild an Article from staged JSON payloads."""
    if not payload:
        return None
    references = payload.get("references") or []
    payload = dict(payload)
    payload["references"] = [tuple(item) for item in references]
    try:
        return Article(**payload)
    except TypeError as err:
        logger.error("Failed to rebuild Article from staged payload", error=str(err))
        return None


def _persist(article: Article, *, staged_at: float, last_checked_ts: float) -> StagedArticle | None:
    """Commit article staging state to Redis and return the resulting record."""
    # Single write path that keeps the per-article key, the staging index,
    # and the immutable article payload in sync so we can recover after
    # crashes or worker restarts.
    client = _get_client()
    article_id = _normalize_article_id(article.article_id)
    key = _stage_key(article_id)
    payload = {
        "article": _serialize_article(article),
        "staged_at": float(staged_at),
        "last_checked_ts": float(last_checked_ts),
    }
    try:
        with client.pipeline() as pipe:
            pipe.set(key, json.dumps(payload, ensure_ascii=False))
            pipe.sadd(STAGING_REDIS_INDEX_KEY, article_id)
            pipe.execute()
    except RedisError as err:
        logger.error(
            "Staged article write failed",
            error=str(err),
            article_id=article_id,
        )
        return None

    return StagedArticle(
        article=article,
        staged_at=float(staged_at),
        last_checked_ts=float(last_checked_ts),
    )


def stage_article(article: Article, *, now: float | None = None) -> StagedArticle | None:
    """Record a newly discovered article in the staging store."""
    # Called when we first discover a qualifying article; everything else
    # (refreshing metrics, graduation) builds on this persisted state.
    timestamp = float(now or time.time())
    stored = _persist(article, staged_at=timestamp, last_checked_ts=timestamp)
    if stored:
        logger.info(
            "Article staged",
            article_id=article.article_id,
            staged_at=timestamp,
        )
    return stored


def update_staged_article(
    staged: StagedArticle,
    *,
    article: Article | None = None,
    score: int | None = None,
    comment_count: int | None = None,
    hn_time: int | None = None,
    last_checked_ts: float | None = None,
) -> StagedArticle | None:
    """Refresh stored article metrics and persist the updated payload."""
    checked_at = float(last_checked_ts or time.time())
    base_article = article or staged.article
    updated_article = replace(
        base_article,
        hn_score=score if score is not None else base_article.hn_score,
        hn_comment_count=(
            comment_count if comment_count is not None else base_article.hn_comment_count
        ),
        hn_posted_ts=hn_time if hn_time is not None else base_article.hn_posted_ts,
    )
    stored = _persist(updated_article, staged_at=staged.staged_at, last_checked_ts=checked_at)
    if stored:
        logger.info(
            "Staged article metrics updated",
            article_id=updated_article.article_id,
            score=updated_article.hn_score,
            comments=updated_article.hn_comment_count,
        )
    return stored


def list_staged_articles() -> List[StagedArticle]:
    """Return the full set of staged articles ordered by staging time."""
    # Enumerate the full staging set; this is what the worker loop iterates
    # over each tick before deciding who graduates.
    client = _get_client()
    try:
        raw_ids = client.smembers(STAGING_REDIS_INDEX_KEY)
    except RedisError as err:
        logger.error("Failed to read stage index", error=str(err))
        return []

    article_ids: List[str] = []
    for raw in raw_ids or []:
        if isinstance(raw, bytes):
            article_ids.append(raw.decode("utf-8"))
        else:
            article_ids.append(str(raw))

    if not article_ids:
        return []

    keys = [_stage_key(article_id) for article_id in article_ids]
    try:
        raw_payloads = client.mget(keys)
    except RedisError as err:
        logger.error("Failed to fetch staged payloads", error=str(err))
        return []

    staged: List[StagedArticle] = []
    for article_id, raw in zip(article_ids, raw_payloads):
        if raw is None:
            try:
                client.srem(STAGING_REDIS_INDEX_KEY, article_id)
            except RedisError:
                pass
            # Index can drift after crashes; drop empty slots eagerly.
            continue
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            logger.error(
                "Failed to parse staged payload",
                article_id=article_id,
                error=str(err),
            )
            continue

        article = _deserialize_article(data.get("article"))
        if not article:
            continue

        staged_at = float(data.get("staged_at", time.time()))
        last_checked = float(data.get("last_checked_ts", staged_at))
        staged.append(
            StagedArticle(
                article=article,
                staged_at=staged_at,
                last_checked_ts=last_checked,
            )
        )

    staged.sort(key=lambda item: item.staged_at)
    return staged


def get_staged_article(article_id: int | str) -> StagedArticle | None:
    """Fetch a specific staged article by id."""
    # Used for targeted lookups (debug or manual interventions) without
    # walking the entire staging set.
    client = _get_client()
    article_id = _normalize_article_id(article_id)
    key = _stage_key(article_id)
    try:
        raw = client.get(key)
    except RedisError as err:
        logger.error("Failed to read staged article", article_id=article_id, error=str(err))
        return None

    if raw is None:
        return None

    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        logger.error("Failed to decode staged article", article_id=article_id, error=str(err))
        return None

    article = _deserialize_article(data.get("article"))
    if not article:
        return None

    staged_at = float(data.get("staged_at", time.time()))
    last_checked = float(data.get("last_checked_ts", staged_at))
    return StagedArticle(article=article, staged_at=staged_at, last_checked_ts=last_checked)


def is_published(article_id: int | str) -> bool:
    """Check whether an article id has already been published."""
    client = _get_client()
    try:
        return bool(
            client.sismember(STAGING_REDIS_PUBLISHED_KEY, _normalize_article_id(article_id))
        )
    except RedisError as err:
        logger.error("Failed to check published set", error=str(err))
        return False


def mark_published(article_id: int | str) -> bool:
    """Remove a staged article and record it in the published set."""
    # Graduation path: delete staging material and remember the id in the
    # published set so we never re-stage it.
    client = _get_client()
    article_id = _normalize_article_id(article_id)
    key = _stage_key(article_id)
    try:
        with client.pipeline() as pipe:
            pipe.delete(key)
            pipe.srem(STAGING_REDIS_INDEX_KEY, article_id)
            pipe.sadd(STAGING_REDIS_PUBLISHED_KEY, article_id)
            pipe.execute()
    except RedisError as err:
        logger.error(
            "Failed to mark article as published",
            article_id=article_id,
            error=str(err),
        )
        return False

    logger.info("Article marked as published", article_id=article_id)
    return True


def ensure_published(article_ids: Iterable[int | str]) -> None:
    """Bulk-mark articles as published without touching staging records."""
    # Helper for bootstrapping: bulk mark historical ids as published so the
    # worker ignores them on future passes.
    client = _get_client()
    normalized = [_normalize_article_id(i) for i in article_ids]
    if not normalized:
        return
    try:
        client.sadd(STAGING_REDIS_PUBLISHED_KEY, *normalized)
    except RedisError as err:
        logger.error("Failed to seed published set", error=str(err))


def clear_stage(article_id: int | str) -> None:
    """Delete a staged article without adding it to the published set."""
    # Administrative escape hatch: remove a staged record without marking it
    # published (used when we want to forget the article entirely).
    client = _get_client()
    article_id = _normalize_article_id(article_id)
    key = _stage_key(article_id)
    try:
        with client.pipeline() as pipe:
            pipe.delete(key)
            pipe.srem(STAGING_REDIS_INDEX_KEY, article_id)
            pipe.execute()
    except RedisError as err:
        logger.error("Failed to clear staged article", article_id=article_id, error=str(err))
