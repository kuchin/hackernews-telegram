"""Article caching and retrieval utilities shared between bot and handlers."""

from dataclasses import dataclass
import random
import time
from typing import Any, Dict, List, Sequence

from clients.exa import ExaError, exa_fetch_contents
from clients.hackernews import get_story_details, get_story_ids
from utils import logger

@dataclass(frozen=True)
class Article:
    article_id: str
    title: str
    url: str
    discussion_url: str
    summary: str
    image_url: str | None
    highlights: List[str]
    top_comments: List[str]
    references: List[tuple[str, str]]
    hn_score: int | None = None
    hn_comment_count: int | None = None
    hn_posted_ts: int | None = None

ARTICLE_BY_ID: Dict[str, Article] = {}
ARTICLE_CACHE: List[Article] = []
ARTICLE_CACHE_TS: float = 0.0
ARTICLE_CACHE_TTL = 180.0  # seconds
# Short-lived cache so /latest stays snappy while still picking up fresh
# enrichment data when the worker rotates stories.

DEFAULT_SUMMARY_PROMPT = "1 sentence, distilled key points"


def _enrich_story_payloads(
    stories: List[Dict[str, Any]],
    *,
    prompt: str | None = None,
) -> List[Dict[str, Any]]:
    """Call Exa to augment HN story payloads with summaries and media."""
    if not stories:
        return []

    urls = [story.get("url") for story in stories if story.get("url")]
    exa_results: Dict[str, Dict[str, Any]] = {}
    if urls:
        summary_prompt = prompt or DEFAULT_SUMMARY_PROMPT
        try:
            exa_payload = exa_fetch_contents(urls, ai_summary_prompt=summary_prompt)
        except ExaError as exc:
            raise ExaError(f"Failed to fetch Exa contents: {exc}") from exc
        except Exception as exc:
            raise ExaError(f"Unexpected error hitting Exa: {exc}") from exc
        else:
            for item in exa_payload.get("results", []) or []:
                key = item.get("id") or item.get("url")
                if key:
                    exa_results[key] = item

    enriched: List[Dict[str, Any]] = []
    for story in stories:
        source_url = story.get("url")
        exa_entry = exa_results.get(source_url) or {}
        enriched.append(
            {
                "id": story.get("id"),
                "title": story.get("title"),
                "url": story.get("url"),
                "hn_url": story.get("hn_url"),
                "summary": exa_entry.get("summary") or "",
                "image": exa_entry.get("image"),
                "author": story.get("by"),
                "score": story.get("score"),
                "comment_count": story.get("descendants"),
                "hn_time": story.get("time"),
                "published": exa_entry.get("publishedDate"),
            }
        )

    return enriched


def _collect_story_candidates(
    ids: Sequence[int],
    *,
    desired: int,
    batch_size: int,
) -> List[Dict[str, Any]]:
    """Iteratively fetch Firebase stories until we satisfy the desired count."""
    if desired <= 0 or not ids:
        return []

    batch = max(1, batch_size)
    collected: List[Dict[str, Any]] = []
    for start in range(0, len(ids), batch):
        batch_ids = ids[start : start + batch]
        # Pull Firebase items in chunks to balance latency and coverage.
        details = get_story_details(batch_ids)
        for story in details:
            if story.get("url"):
                collected.append(story)
        if len(collected) >= desired:
            break
    return collected


def _coerce_int(value: Any) -> int | None:
    """Convert friendly numeric fields while tolerating missing data."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_top_articles_with_exa(
    limit: int = 2,
    *,
    kind: str = "top",
    prompt: str | None = None,
) -> List[Dict[str, Any]]:
    """Return a curated slice of top stories with Exa enrichment."""
    if limit <= 0:
        return []

    candidate_ids = get_story_ids(kind=kind)
    stories = _collect_story_candidates(
        candidate_ids,
        desired=limit,
        batch_size=max(limit * 3, 20),
    )
    if not stories:
        return []

    return _enrich_story_payloads(stories[:limit], prompt=prompt)


def get_articles_by_ids(
    ids: Sequence[int | str],
    *,
    prompt: str | None = None,
) -> List[Article]:
    """Hydrate full Article objects for specific Hacker News ids."""
    normalized_ids: List[int] = []
    seen: set[int] = set()
    for raw_id in ids:
        try:
            iid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if iid in seen:
            continue
        normalized_ids.append(iid)
        seen.add(iid)

    if not normalized_ids:
        return []

    stories = get_story_details(normalized_ids)
    if not stories:
        return []

    enriched = _enrich_story_payloads(stories, prompt=prompt)
    articles = [_build_article(payload) for payload in enriched]
    for article in articles:
        ARTICLE_BY_ID[article.article_id] = article
    return articles


def _fetch_random_articles_with_exa(
    count: int,
    *,
    kind: str = "top",
    prompt: str | None = None,
) -> List[Dict[str, Any]]:
    """Pull a shuffled batch of enriched stories to support /random."""
    if count <= 0:
        return []

    ids = get_story_ids(kind=kind)
    if not ids:
        return []

    shuffled_ids = list(ids)
    # Randomize before batching so we get a different slice each command.
    random.shuffle(shuffled_ids)

    stories = _collect_story_candidates(
        shuffled_ids,
        desired=count,
        batch_size=max(count * 4, 40),
    )
    if not stories:
        return []

    return _enrich_story_payloads(stories[:count], prompt=prompt)


def _build_article(payload: Dict[str, Any]) -> Article:
    """Convert an enriched payload into our immutable Article dataclass."""
    article_id = str(payload.get("id"))
    url = payload.get("url") or payload.get("hn_url")
    discussion_url = payload.get("hn_url") or url or ""
    summary = (payload.get("summary") or "").strip()
    # if not summary:
    #     summary = "Summary unavailable."

    highlights = [summary] if summary else []
    references: List[tuple[str, str]] = []
    if url:
        references.append(("Original story", url))
    if discussion_url:
        references.append(("Hacker News discussion", discussion_url))

    return Article(
        article_id=article_id,
        title=payload.get("title") or "Untitled",
        url=url or "https://news.ycombinator.com/",
        discussion_url=discussion_url or "https://news.ycombinator.com/",
        summary=summary,
        image_url=payload.get("image"),
        highlights=highlights,
        top_comments=[],
        references=references,
        hn_score=_coerce_int(payload.get("score")),
        hn_comment_count=_coerce_int(payload.get("comment_count")),
        hn_posted_ts=_coerce_int(payload.get("hn_time")),
    )


def _refresh_articles(limit: int) -> None:
    """Refresh the hot article cache, raising if we cannot populate it."""
    global ARTICLE_CACHE, ARTICLE_CACHE_TS, ARTICLE_BY_ID
    try:
        payloads = get_top_articles_with_exa(limit=limit)
    except ExaError as err:
        raise RuntimeError(f"Failed to fetch articles: {err}") from err

    articles = [_build_article(p) for p in payloads]
    if not articles:
        raise RuntimeError("No stories available from reader.get_top_articles_with_exa().")

    # Replace the hot cache atomically so lookups never mix generations.
    ARTICLE_CACHE = articles
    ARTICLE_BY_ID = {a.article_id: a for a in articles}
    ARTICLE_CACHE_TS = time.time()


def _ensure_articles(limit: int) -> List[Article]:
    """Ensure the in-memory cache contains at least `limit` fresh articles."""
    global ARTICLE_CACHE_TS
    now = time.time()
    if ARTICLE_CACHE and (now - ARTICLE_CACHE_TS) < ARTICLE_CACHE_TTL and len(ARTICLE_CACHE) >= limit:
        return ARTICLE_CACHE

    try:
        # Cache expired or undersized; fetch a fresh batch from upstream.
        _refresh_articles(limit=limit)
    except RuntimeError as err:
        logger.warning("Article refresh failed", error=str(err))
        if ARTICLE_CACHE:
            return ARTICLE_CACHE
        raise
    return ARTICLE_CACHE


def get_articles(count: int) -> List[Article]:
    """Return up to `count` cached articles, refreshing if required."""
    if count <= 0:
        return []
    articles = _ensure_articles(limit=count)
    return articles[:count]


def get_random_articles(count: int) -> List[Article]:
    """Return `count` random articles using Exa enrichment on demand."""
    if count <= 0:
        return []
    try:
        payloads = _fetch_random_articles_with_exa(count)
    except ExaError as err:
        raise RuntimeError(f"Failed to fetch random articles: {err}") from err
    except Exception as exc:
        raise RuntimeError(f"Unexpected error while fetching random stories: {exc}") from exc

    articles = [_build_article(p) for p in payloads]
    for article in articles:
        ARTICLE_BY_ID[article.article_id] = article
    return articles
