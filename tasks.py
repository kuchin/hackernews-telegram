"""Celery tasks for staging and graduating Hacker News articles."""

from __future__ import annotations

import os
import time
from typing import Dict, List, Tuple

from clients.telegram import TelegramClient
from worker import celery_app
from utils import logger

from clients.hackernews import get_story_details, get_story_ids
from content.reader import get_articles_by_ids
from publish.pending_store import store_pending_post
from publish.staging_store import (
    StagedArticle,
    is_published,
    list_staged_articles,
    mark_published,
    stage_article,
    update_staged_article,
)
from publish.writer import send_article_bundle
from config import (
    ARTICLE_AGE_FLOOR_COMMENTS,
    ARTICLE_AGE_FLOOR_POINTS,
    ARTICLE_MAX_WAIT_SECONDS,
    ARTICLE_METRIC_REFRESH_SECONDS,
    ARTICLE_MIN_COMMENTS,
    ARTICLE_MIN_POINTS,
    CHANNEL_ID,
    DISCUSSION_ID,
    WORKER_ARTICLE_LIMIT,
)


_TELEGRAM_CLIENT: TelegramClient | None = None


def _get_telegram() -> TelegramClient:
    """Instantiate or reuse the worker-side Telegram client."""
    global _TELEGRAM_CLIENT
    if _TELEGRAM_CLIENT is None:
        token = os.getenv("TG_TOKEN")
        if not token:
            raise RuntimeError("TG_TOKEN env var is required for worker tasks")
        # Workers reuse a shared Telegram client so retries carry session state.
        _TELEGRAM_CLIENT = TelegramClient(token)
    return _TELEGRAM_CLIENT


def _int_or_none(value: object) -> int | None:
    """Coerce integers from mixed sources without raising."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _collect_top_story_ids(limit: int) -> List[int]:
    """Fetch and normalize the set of top Hacker News ids to evaluate."""
    try:
        raw_ids = get_story_ids(kind="top", limit=limit)
    except Exception as exc:
        logger.error("Failed to fetch top story ids", error=str(exc))
        return []

    normalized: List[int] = []
    for raw in raw_ids or []:
        iid = _int_or_none(raw)
        if iid is not None:
            normalized.append(iid)
    return normalized


def _stage_new_articles(candidate_ids: List[int], staged_map: Dict[int, StagedArticle]) -> None:
    """Enrich and persist any unseen article candidates in Redis staging."""
    # Filter out anything already scheduled or published before we spend
    # network calls enriching it again.
    pending_ids: List[int] = []
    for story_id in candidate_ids:
        if story_id in staged_map:
            continue
        if is_published(story_id):
            continue
        pending_ids.append(story_id)

    if not pending_ids:
        return

    try:
        articles = get_articles_by_ids(pending_ids)
    except Exception as exc:
        logger.error("Failed to stage article details", error=str(exc))
        return

    staged_count = 0
    for article in articles:
        article_id = _int_or_none(getattr(article, "article_id", None))
        if article_id is None:
            continue
        staged_record = stage_article(article)
        if not staged_record:
            continue
        staged_map[article_id] = staged_record
        staged_count += 1

    if staged_count:
        logger.info("Articles staged", count=staged_count)


def _refresh_metrics(staged_map: Dict[int, StagedArticle], *, now: float) -> None:
    """Refresh score/comment metrics for staged articles that need it."""
    refresh_ids: List[int] = []
    for article_id, staged in staged_map.items():
        # Throttle Firebase refreshes so we only poll items that aged past the cadence.
        if now - staged.last_checked_ts >= ARTICLE_METRIC_REFRESH_SECONDS:
            refresh_ids.append(article_id)

    if not refresh_ids:
        return

    try:
        details = get_story_details(refresh_ids)
    except Exception as exc:
        logger.warning("Failed to refresh staged metrics", error=str(exc))
        return

    detail_by_id: Dict[int, dict] = {}
    for item in details or []:
        iid = _int_or_none(item.get("id")) if isinstance(item, dict) else None
        if iid is not None:
            detail_by_id[iid] = item

    for article_id in refresh_ids:
        staged = staged_map.get(article_id)
        if not staged:
            continue
        detail = detail_by_id.get(article_id)
        updated = update_staged_article(
            staged,
            score=(detail or {}).get("score") if detail else None,
            comment_count=(detail or {}).get("descendants") if detail else None,
            hn_time=(detail or {}).get("time") if detail else None,
            last_checked_ts=now,
        )
        if updated:
            staged_map[article_id] = updated


def _should_graduate(staged: StagedArticle, *, now: float) -> tuple[bool, str | None, Dict[str, float | int | None]]:
    """Decide whether a staged article meets any graduation criteria."""
    article = staged.article
    score = article.hn_score or 0
    comments = article.hn_comment_count or 0
    age_seconds = None
    if article.hn_posted_ts:
        age_seconds = max(0.0, now - float(article.hn_posted_ts))

    reason: str | None = None
    if score >= ARTICLE_MIN_POINTS:
        reason = "score"
    elif comments >= ARTICLE_MIN_COMMENTS:
        reason = "comments"
    elif age_seconds is not None and age_seconds >= ARTICLE_MAX_WAIT_SECONDS:
        reason = "age"

    extras: Dict[str, float | int | None] = {
        "score": score,
        "comments": comments,
        "age_seconds": age_seconds,
    }
    # Downstream logging uses extras to explain why an article moved (or did not).
    return bool(reason), reason, extras


def _publish_graduates(
    telegram: TelegramClient,
    staged_items: List[Tuple[StagedArticle, str, Dict[str, float | int | None]]],
) -> None:
    """Post qualifying articles to the channel and wire up pending metadata."""
    for staged, reason, extras in staged_items:
        article = staged.article

        score_val = _int_or_none((extras or {}).get("score"))
        if score_val is None:
            score_val = _int_or_none(article.hn_score) or 0
        comments_val = _int_or_none((extras or {}).get("comments"))
        if comments_val is None:
            comments_val = _int_or_none(article.hn_comment_count) or 0

        if reason == "age" and (
            score_val < ARTICLE_AGE_FLOOR_POINTS and comments_val < ARTICLE_AGE_FLOOR_COMMENTS
        ):
            # Aging out acts as a safety valve; low-engagement stories are forgotten instead of posted.
            if mark_published(article.article_id):
                logger.info(
                    "Aged article discarded for low engagement",
                    article_id=article.article_id,
                    score=score_val,
                    comments=comments_val,
                )
            else:
                logger.warning(
                    "Failed to discard aged article",
                    article_id=article.article_id,
                    score=score_val,
                    comments=comments_val,
                )
            continue
        try:
            main_message = send_article_bundle(
                telegram,
                CHANNEL_ID,
                article,
                include_keyboard=False,
            )
        except Exception as exc:
            logger.error(
                "Failed to publish staged article",
                article_id=article.article_id,
                error=str(exc),
                reason=reason,
            )
            continue

        pending_stored = False
        channel_post_id = None
        if isinstance(main_message, dict):
            channel_post_id = main_message.get("message_id")
            message_chat = main_message.get("chat", {})
        else:
            message_chat = {}

        discussion_target = DISCUSSION_ID or message_chat.get("linked_chat_id")

        if channel_post_id is not None:
            # Cache the headline metadata so the bot can post the threaded comment
            # once Telegram delivers the auto-forward into the discussion chat.
            pending_stored = store_pending_post(channel_post_id, article, discussion_target)
            if not pending_stored:
                logger.error(
                    "Failed to persist pending mapping for graduated article",
                    article_id=article.article_id,
                    channel_post_id=channel_post_id,
                )
        else:
            logger.error(
                "Graduated article missing message id",
                article_id=article.article_id,
            )

        if mark_published(article.article_id):
            log_payload = {
                "article_id": article.article_id,
                "reason": reason,
                "pending_stored": pending_stored,
                **extras,
            }
            logger.info("Graduated article published", **log_payload)
        else:
            logger.error(
                "Failed to mark article as published",
                article_id=article.article_id,
            )


@celery_app.task
def publish_latest() -> None:
    # One beat tick: pull fresh IDs, ensure staging is populated, refresh
    # engagement, then graduate anything that now meets policy.
    """Celery beat entrypoint orchestrating the staging-to-publish pipeline."""
    if CHANNEL_ID is None:
        logger.info("Scheduled publish skipped: no channel configured")
        return

    telegram = _get_telegram()

    candidate_ids = _collect_top_story_ids(WORKER_ARTICLE_LIMIT)

    staged_articles = list_staged_articles()
    staged_map: Dict[int, StagedArticle] = {}
    for staged in staged_articles:
        article_id = _int_or_none(getattr(staged.article, "article_id", None))
        if article_id is None:
            continue
        # Keep everything in-memory for this tick; Redis remains source of truth.
        staged_map[article_id] = staged

    _stage_new_articles(candidate_ids, staged_map)

    if not staged_map:
        logger.info("No staged articles to evaluate")
        return

    now = time.time()
    _refresh_metrics(staged_map, now=now)

    graduates: List[Tuple[StagedArticle, str, Dict[str, float | int | None]]] = []
    for staged in staged_map.values():
        should_post, reason, extras = _should_graduate(staged, now=now)
        if should_post and reason:
            graduates.append((staged, reason, extras))

    if not graduates:
        logger.info("No staged articles met graduation criteria")
        return

    graduates.sort(key=lambda item: item[0].staged_at)
    _publish_graduates(telegram, graduates)
