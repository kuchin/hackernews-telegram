import os
from typing import Any, Dict, Iterable, Tuple

import requests

EXA_CONTENTS_URL = "https://api.exa.ai/contents"
DEFAULT_TIMEOUT = (6.1, 12.0)


class ExaError(RuntimeError):
    pass


def _require_exa_key() -> str:
    """Return the Exa API key or raise with a helpful error if missing."""
    key = os.getenv("EXA_API_KEY")
    if not key:
        raise ExaError("EXA_API_KEY environment variable is required for exa_fetch_contents()")
    # Fail fast so worker logs make missing credentials obvious.
    return key


def exa_fetch_contents(
    urls: Iterable[str],
    *,
    ai_summary_prompt: str,
    include_text: bool = False,
    highlights: bool | Dict[str, Any] = False,
    livecrawl: str | None = "fallback",
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> Dict[str, Any]:
    """Fetch summaries and optional highlights for one or more URLs via Exa."""
    key = _require_exa_key()
    s = session or requests.Session()

    # Exa can return heavy payloads; default to summaries-only so we can call
    # it inline during staging without blocking the worker.
    body: Dict[str, Any] = {
        "urls": list(urls),
        "text": bool(include_text),
        "summary": {"query": ai_summary_prompt},
    }
    if highlights:
        body["highlights"] = highlights if isinstance(highlights, dict) else {"numSentences": 2}
    if livecrawl:
        body["livecrawl"] = livecrawl

    headers = {
        "x-api-key": key,
        "Content-Type": "application/json",
    }

    r = s.post(EXA_CONTENTS_URL, headers=headers, json=body, timeout=timeout)
    try:
        r.raise_for_status()
    except Exception as exc:
        raise ExaError(f"Exa /contents error: {getattr(r, 'text', '')}") from exc

    return r.json()
