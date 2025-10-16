"""Helpers to turn article data into Telegram-friendly strings and keyboards."""

import html
from datetime import datetime, timezone
from typing import Protocol, Sequence


class ArticleLike(Protocol):
    article_id: str
    title: str
    url: str
    discussion_url: str
    summary: str
    image_url: str | None
    highlights: Sequence[str]
    top_comments: Sequence[str]
    references: Sequence[tuple[str, str]]
    hn_score: int | None
    hn_comment_count: int | None
    hn_posted_ts: int | None


def escape(text: str) -> str:
    """HTML-escape text for safe Telegram markup."""
    return html.escape(text, quote=False)


def build_article_caption(article: ArticleLike) -> str:
    """Compose the primary channel caption with metadata and summary."""
    article_href = html.escape(article.url, quote=True)
    discussion_href = html.escape(article.discussion_url, quote=True)
    is_hot = article.hn_score > 100 or article.hn_comment_count > 100
    return (
        f"<b><a href=\"{article_href}\">{escape(article.title)}</a></b>\n\n"
        f"{escape(article.summary)}\n\n"
        f"<i><a href=\"{discussion_href}\">Hacker News discussion</a></i>\n"
        f"{article.hn_score} points, {article.hn_comment_count} comments{is_hot and " 🔥" or ""}"
    )


def _format_hn_timestamp(timestamp: int | None) -> str | None:
    """Render a Unix timestamp as a human-friendly UTC string."""
    if not timestamp:
        return None
    try:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def build_comment_body(article: ArticleLike) -> str:
    """Assemble the threaded comment body with stats and links."""
    title_line = f"<b>{escape(article.title)}</b>"
    url_line = escape(article.url) if article.url else ""

    stats_parts: list[str] = []
    posted = _format_hn_timestamp(article.hn_posted_ts)
    if posted:
        stats_parts.append(f"\n- Posted {posted}")
    if article.hn_score is not None:
        stats_parts.append(f"\n- {article.hn_score} points")
    if article.hn_comment_count is not None:
        label = "comment" if article.hn_comment_count == 1 else "comments"
        stats_parts.append(f"\n- {article.hn_comment_count} {label}")

    stats_suffix = ""
    if stats_parts:
        stats_suffix = "\n" + "".join(stats_parts)

    discussion_href = html.escape(article.discussion_url, quote=True) if article.discussion_url else ""
    discussion_link = (
        f"<a href=\"{discussion_href}\">Hacker News discussion</a>"
        if discussion_href
        else "Hacker News discussion"
    )
    stats_line = f"{discussion_link}{stats_suffix}"

    lines = [title_line]
    if url_line:
        lines.append(url_line)
    if stats_line:
        if url_line:
            lines.append("")
        lines.append(stats_line)

    return "\n".join(lines).strip()


def build_article_keyboard(article: ArticleLike) -> dict:
    """Produce the inline keyboard shared between headline and comments."""
    return {
        "inline_keyboard": [
            [
                {"text": "More details", "callback_data": f"details:{article.article_id}"},
                {"text": "Top comments", "callback_data": f"comments:{article.article_id}"},
            ],
            [{"text": "Related links", "callback_data": f"refs:{article.article_id}"}],
        ]
    }


def format_highlights(article: ArticleLike) -> str:
    """Format the highlights button payload."""
    if not article.highlights:
        return "<b>Why it matters</b>\nHighlights coming soon."
    bullet_lines = "\n".join(f"- {escape(point)}" for point in article.highlights)
    return f"<b>Why it matters</b>\n{bullet_lines}"


def format_comments(article: ArticleLike) -> str:
    """Format the top comments button payload."""
    if not article.top_comments:
        hn_link = escape(article.discussion_url)
        return f"<b>Top Hacker News takes</b>\nNo standout comments yet. <a href=\"{hn_link}\">Join the thread</a>."
    comments = "\n\n".join(f"\"{escape(c)}\"" for c in article.top_comments)
    return f"<b>Top Hacker News takes</b>\n{comments}"


def format_references(article: ArticleLike) -> str:
    """Format the related links button payload."""
    if not article.references:
        return "<b>Related reading</b>\nNo extra links yet."
    links = "\n".join(f"- <a href=\"{url}\">{escape(label)}</a>" for label, url in article.references)
    return f"<b>Related reading</b>\n{links}"
