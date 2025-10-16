import concurrent.futures
from typing import Any, Dict, List, Sequence

import requests

HN_BASE = "https://hacker-news.firebaseio.com/v0"
HN_ITEM_URL = f"{HN_BASE}/item/{{id}}.json"
HN_LISTS = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
}
HN_WEB_ITEM = "https://news.ycombinator.com/item?id={id}"

DEFAULT_TIMEOUT = (6.1, 12.0)
SESSION = requests.Session()
SESSION.headers.update(
    {
        # sensible UA to reduce chance of random blocks on some hosts
        "User-Agent": (
            "HNBot/1.0 (+https://news.ycombinator.com/) "
            "PythonRequests"
        )
    }
)

def _http_get_json(url: str, *, session: requests.Session | None = None, timeout=DEFAULT_TIMEOUT) -> Any:
    """GET a JSON payload from Firebase with basic retry session reuse."""
    s = session or SESSION
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _fetch_item(item_id: int) -> Dict[str, Any] | None:
    """Best-effort fetch of a single HN item; swallow errors for resiliency."""
    try:
        return _http_get_json(HN_ITEM_URL.format(id=item_id))
    except Exception:
        return None


def _normalize_story(item: Dict[str, Any]) -> Dict[str, Any] | None:
    """Filter and normalize raw Firebase entries down to stories we can use."""
    if not item or item.get("type") != "story":
        return None
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "by": item.get("by"),
        "time": item.get("time"),
        "score": item.get("score"),
        "descendants": item.get("descendants"),
        "url": item.get("url"),
        "hn_url": HN_WEB_ITEM.format(id=item.get("id")),
        "type": "story",
}


def get_story_ids(kind: str = "top", *, limit: int | None = None) -> List[int]:
    """Return up to `limit` story ids for a Firebase collection."""
    kind_key = HN_LISTS.get(kind.lower())
    if not kind_key:
        raise ValueError(f"Unknown kind '{kind}'. Use one of {list(HN_LISTS)}.")

    ids_url = f"{HN_BASE}/{kind_key}.json"
    ids: List[int] = _http_get_json(ids_url) or []
    if limit is not None:
        # Firebase returns 500 items; trim early for callers that only need a sample.
        ids = ids[: max(0, limit)]
    return ids


def get_story_details(ids: Sequence[int]) -> List[Dict[str, Any]]:
    """Fetch and normalize story payloads for the provided ids."""
    # Deduplicate while preserving order so callers get deterministic story sets.
    unique_ids: List[int] = []
    seen: set[int] = set()
    for raw_id in ids:
        try:
            iid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if iid in seen:
            continue
        unique_ids.append(iid)
        seen.add(iid)

    if not unique_ids:
        return []

    stories: List[Dict[str, Any]] = []
    # HN's Firebase API is latency-bound; pool the fetches so the worker
    # can sample several ids per beat without blocking on serial HTTP calls.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(16, max(4, len(unique_ids)))
    ) as ex:
        futures = {ex.submit(_fetch_item, iid): iid for iid in unique_ids}
        for fut in concurrent.futures.as_completed(futures):
            item = fut.result()
            story = _normalize_story(item) if item else None
            if story:
                stories.append(story)

    order = {iid: i for i, iid in enumerate(unique_ids)}
    stories.sort(key=lambda s: order.get(s["id"], 1_000_000))
    return stories


def get_recent_top_articles(limit: int = 30, kind: str = "top") -> List[Dict[str, Any]]:
    """Shortcut helper combining id and detail fetch for top stories."""
    if limit <= 0:
        return []
    ids = get_story_ids(kind=kind, limit=limit)
    return get_story_details(ids)


def get_article(item_id: int) -> Dict[str, Any]:
    """Fetch a single HN item by id without additional normalization."""
    return _http_get_json(HN_ITEM_URL.format(id=item_id))
