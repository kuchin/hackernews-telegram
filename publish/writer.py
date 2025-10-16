"""Utilities for sending articles and comments via Telegram."""

import time
from typing import Any, Dict

from clients.telegram import TelegramClient
from content.reader import Article
from publish.formatter import build_article_caption, build_article_keyboard, build_comment_body
from utils import logger, format_http_error


def send_article_bundle(
    telegram: TelegramClient,
    chat_id: int,
    article: Article,
    *,
    thread_id: int | None = None,
    include_keyboard: bool = True,
) -> Dict[str, Any]:
    """Post the headline card (photo or text) optionally with inline controls."""
    caption = build_article_caption(article)
    keyboard = build_article_keyboard(article) if include_keyboard else None
    if article.image_url:
        try:
            return telegram.send_photo(
                chat_id,
                article.image_url,
                caption=caption,
                thread_id=thread_id,
                reply_markup=keyboard,
            )
        except Exception as err:
            logger.warning(
                "send_photo failed, falling back to text",
                error=format_http_error(err),
                chat_id=chat_id,
                article_id=article.article_id,
            )
    # Ensure the channel still receives the headline even if media uploads
    # fail; downstream flows rely on the text post existing.
    return telegram.send_message(
        chat_id,
        caption,
        thread_id=thread_id,
        reply_markup=keyboard,
        disable_preview=False,
    )


def send_comment_bundle(
    telegram: TelegramClient,
    chat_id: int,
    article: Article,
    *,
    thread_id: int | None = None,
    root_reply_id: int | None = None,
    attempts: int = 4,
) -> bool:
    """Deliver the discussion comment payload, adapting between thread/reply modes."""
    if thread_id is None and root_reply_id is None:
        raise ValueError("Need either thread_id or root_reply_id to deliver comments")

    def attempt_send(
        text: str,
        *,
        disable_preview: bool,
        reply_markup: Dict[str, Any] | None = None,
    ) -> bool:
        """Try to send a single comment chunk with exponential backoff."""
        # Telegram routing (thread vs reply) can flap; retry with short backoffs.
        backoffs = [0.0, 0.3, 0.8, 1.5]
        for idx, delay in enumerate(backoffs[:attempts]):
            if delay:
                time.sleep(delay)
            try:
                telegram.send_message(
                    chat_id,
                    text,
                    thread_id=thread_id,
                    reply_to=None if thread_id is not None else root_reply_id,
                    reply_markup=reply_markup,
                    disable_preview=disable_preview,
                    allow_without_reply=True if thread_id is None else None,
                )
                return True
            except Exception as err:
                detail = format_http_error(err).lower()
                recoverable = False
                # Telegram sometimes toggles between topic/non-topic modes;
                # treat these thread/reply errors as transient and retry with
                # the alternate addressing mode.
                if thread_id is not None and "message thread not found" in detail:
                    recoverable = True
                if thread_id is None and "message to reply" in detail:
                    recoverable = True
                if recoverable and idx + 1 < attempts:
                    continue
                logger.warning(
                    "Failed to send comment chunk",
                    detail=detail,
                    chat_id=chat_id,
                    article_id=article.article_id,
                    attempt=idx,
                )
                return False
        return False

    comment_text = build_comment_body(article)
    return attempt_send(
        comment_text,
        disable_preview=False,
        reply_markup=build_article_keyboard(article),
    )
