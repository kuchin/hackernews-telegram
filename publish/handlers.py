"""Handlers for Telegram interactions (commands, auto-forwards, callbacks)."""

from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List

from clients.telegram import TelegramAPIError, TelegramClient
from content.reader import (
    ARTICLE_BY_ID,
    get_articles,
    get_articles_by_ids,
    get_random_articles,
)
from publish.formatter import format_comments, format_highlights, format_references
from publish.writer import send_article_bundle, send_comment_bundle
from publish.pending_store import pop_pending_post, store_pending_post
from utils import logger, format_http_error
from config import LATEST_ARTICLE_COUNT, CHANNEL_ID, DISCUSSION_ID


_telegram: TelegramClient | None = None


def set_telegram(client: TelegramClient) -> None:
    """Inject the shared Telegram client so handlers can send replies."""
    global _telegram
    _telegram = client


def _ensure_telegram() -> TelegramClient:
    """Return the configured Telegram client or raise if initialization is missing."""
    if _telegram is None:
        raise RuntimeError("Telegram client not set for handlers")
    return _telegram


def handle_message(update: Dict[str, Any]) -> None:
    """Entry point for all message updates (commands, forwards, plain chat)."""
    message = update.get("message")
    if not message:
        return

    logger.debug("Incoming update", update=update)
    chat_id = message["chat"]["id"]
    thread_id = message.get("message_thread_id")
    logger.info(
        "Message metadata",
        chat_id=chat_id,
        thread_id=thread_id,
        is_topic=bool(message.get("is_topic_message")),
        chat_type=message["chat"].get("type"),
    )

    if message.get("is_automatic_forward"):
        logger.debug("Auto-forward update", message=message)
        handle_automatic_forward(message)
        return

    text = (message.get("text") or "").strip()
    # Treat each bot command explicitly so we preserve predictable replies in group chats.
    if text.startswith("/start"):
        handle_start(chat_id=chat_id, thread_id=thread_id)
    elif text.startswith("/latest"):
        handle_latest(
            chat_id=chat_id,
            thread_id=thread_id,
            origin_chat=message.get("chat"),
            article_count=LATEST_ARTICLE_COUNT,
        )
    elif text.startswith("/random"):
        handle_random(
            chat_id=chat_id,
            thread_id=thread_id,
            origin_chat=message.get("chat"),
            article_count=LATEST_ARTICLE_COUNT,
        )
    elif text.startswith("/get"):
        handle_get(
            chat_id=chat_id,
            thread_id=thread_id,
            origin_chat=message.get("chat"),
            command_text=text,
        )
    else:
        handle_unknown(chat_id=chat_id, thread_id=thread_id)


def handle_start(*, chat_id: int, thread_id: int | None) -> None:
    """Send onboarding copy for /start invocations."""
    telegram = _ensure_telegram()
    help_text = (
        "<b>Welcome!</b>\n"
        "Use /latest for a curated Hacker News drop. Tap the inline buttons to reveal highlights, "
        "comments, and related links without cluttering the feed."
    )
    telegram.send_message(chat_id, help_text, thread_id=thread_id, disable_preview=True)


def handle_unknown(*, chat_id: int, thread_id: int | None) -> None:
    """Politely guide users toward supported commands."""
    telegram = _ensure_telegram()
    telegram.send_message(
        chat_id,
        "Try /latest to see today's curated mock story.",
        thread_id=thread_id,
        disable_preview=True,
    )


def _publish_articles(
    *,
    articles: List[Any],
    chat_id: int,
    thread_id: int | None,
    origin_chat: Dict[str, Any] | None,
    context: str,
) -> None:
    """Deliver one or more articles to the correct destination with fallbacks."""
    telegram = _ensure_telegram()

    if not articles:
        logger.warning("No stories available for publish", context=context, chat_id=chat_id)
        telegram.send_message(
            chat_id,
            "No fresh stories to share right now.",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    origin_chat = origin_chat or {}
    channel_target = CHANNEL_ID or origin_chat.get("linked_chat_id")
    using_channel = channel_target is not None and channel_target != chat_id

    remaining_articles: List[Any] = articles
    if using_channel:
        # Prefer to seed the channel first so the discussion thread receives
        # the official auto-forward we can anchor replies to.
        posted_count = 0
        fallback: List[Any] = []
        for article in articles:
            try:
                main_message = send_article_bundle(
                    telegram,
                    channel_target,
                    article,
                    include_keyboard=False,
                )
            except Exception as err:
                logger.error(
                    "Failed to send headline to channel",
                    channel_id=channel_target,
                    error=format_http_error(err),
                    article_id=getattr(article, "article_id", None),
                    context=context,
                )
                fallback.append(article)
                continue

            discussion_target = (
                DISCUSSION_ID
                or origin_chat.get("id")
                or main_message.get("chat", {}).get("linked_chat_id")
                or chat_id
            )
            channel_post_id = main_message["message_id"]
            pending_written = store_pending_post(
                channel_post_id,
                article,
                discussion_target,
            )
            # Pair the channel post with its discussion destination so the
            # auto-forward handler knows exactly where to drop the first
            # comment once Telegram delivers the forward.
            logger.info(
                "Channel post dispatched",
                channel_id=channel_target,
                channel_post_id=channel_post_id,
                discussion_id=discussion_target,
                article_id=getattr(article, "article_id", None),
                pending_written=pending_written,
                context=context,
            )
            posted_count += 1

        if posted_count:
            telegram.send_message(
                chat_id,
                f"Posted {posted_count} story{'ies' if posted_count != 1 else ''} to the channel. Comments will show up shortly.",
                thread_id=thread_id,
                disable_preview=True,
            )

        remaining_articles = fallback
        if remaining_articles:
            notice = (
                "Channel unavailable. Sending stories here instead."
                if posted_count == 0
                else "Couldn't post some stories to the channel, sharing them here."
            )
            telegram.send_message(
                chat_id,
                notice,
                thread_id=thread_id,
                disable_preview=True,
            )
        else:
            return

    for article in remaining_articles:
        main_message = send_article_bundle(
            telegram,
            chat_id,
            article,
            thread_id=thread_id,
        )
        if thread_id is not None:
            thread_target_id = thread_id
            root_reply_id = None
        else:
            thread_target_id = None
            root_reply_id = main_message["message_id"]

        if not send_comment_bundle(
            telegram,
            chat_id,
            article,
            thread_id=thread_target_id,
            root_reply_id=root_reply_id,
        ):
            logger.warning(
                "Comment bundle failed after headline",
                chat_id=chat_id,
                article_id=getattr(article, "article_id", None),
                context=context,
            )


def handle_latest(
    *,
    chat_id: int,
    thread_id: int | None,
    origin_chat: Dict[str, Any] | None,
    article_count: int | None = None,
) -> None:
    """Fetch and post the most recent curated set triggered via /latest."""
    count = article_count or LATEST_ARTICLE_COUNT
    logger.info(
        "handle_latest invoked",
        chat_id=chat_id,
        thread_id=thread_id,
        origin_chat=origin_chat,
        article_count=count,
    )
    try:
        telegram = _ensure_telegram()
        telegram.send_message(
            chat_id,
            "Fetching latest articles...",
            thread_id=thread_id,
            disable_preview=True,
        )

        articles = get_articles(count)
    except RuntimeError as err:
        logger.error("handle_latest failed", error=str(err), chat_id=chat_id)
        _ensure_telegram().send_message(
            chat_id,
            "Couldn't load Hacker News stories right now. Please try again shortly.",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    _publish_articles(
        articles=articles,
        chat_id=chat_id,
        thread_id=thread_id,
        origin_chat=origin_chat,
        context="latest",
    )


def handle_random(
    *,
    chat_id: int,
    thread_id: int | None,
    origin_chat: Dict[str, Any] | None,
    article_count: int | None = None,
) -> None:
    """Fetch and post a randomized slice of curated stories."""
    count = article_count or LATEST_ARTICLE_COUNT
    logger.info(
        "handle_random invoked",
        chat_id=chat_id,
        thread_id=thread_id,
        origin_chat=origin_chat,
        article_count=count,
    )
    try:
        telegram = _ensure_telegram()
        telegram.send_message(
            chat_id,
            "Fetching random articles...",
            thread_id=thread_id,
            disable_preview=True,
        )

        articles = get_random_articles(count)
    except RuntimeError as err:
        logger.error("handle_random failed", error=str(err), chat_id=chat_id)
        _ensure_telegram().send_message(
            chat_id,
            "Couldn't load random stories right now. Please try again shortly.",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    _publish_articles(
        articles=articles,
        chat_id=chat_id,
        thread_id=thread_id,
        origin_chat=origin_chat,
        context="random",
    )


def handle_get(
    *,
    chat_id: int,
    thread_id: int | None,
    origin_chat: Dict[str, Any] | None,
    command_text: str,
) -> None:
    """Resolve a specific Hacker News id or URL and publish it."""
    telegram = _ensure_telegram()

    parts = (command_text or "").split(maxsplit=1)
    if len(parts) < 2:
        telegram.send_message(
            chat_id,
            "Usage: /get &lt;Hacker News item id or URL&gt;",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    raw_target = parts[1].strip()

    def _extract_story_id(raw: str) -> int | None:
        """Best-effort parse of a Hacker News story id from text or URL."""
        candidate = raw.strip()
        if not candidate:
            return None
        if candidate.isdigit():
            return int(candidate)

        try:
            parsed = urlparse(candidate)
        except ValueError:
            return None

        # Accept both canonical /item?id= URLs and anything embedding id= in the path or query.
        query_values = parse_qs(parsed.query or "")
        query_id = (query_values.get("id") or [None])[0]
        if query_id and query_id.isdigit():
            return int(query_id)

        if "id=" in candidate:
            tail = candidate.split("id=", 1)[1]
            for token in tail.split("&"):
                token = token.strip()
                if token.isdigit():
                    return int(token)
        return None

    story_id = _extract_story_id(raw_target)
    if story_id is None:
        telegram.send_message(
            chat_id,
            "Couldn't parse a Hacker News story id from that input.",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    telegram.send_message(
        chat_id,
        f"Fetching Hacker News story {story_id}...",
        thread_id=thread_id,
        disable_preview=True,
    )

    try:
        articles = get_articles_by_ids([story_id])
    except Exception as err:
        logger.error(
            "handle_get failed to load story",
            chat_id=chat_id,
            story_id=story_id,
            error=str(err),
        )
        telegram.send_message(
            chat_id,
            "Couldn't load that story right now. Please try again shortly.",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    if not articles:
        telegram.send_message(
            chat_id,
            f"No Hacker News story found with id {story_id}.",
            thread_id=thread_id,
            disable_preview=True,
        )
        return

    article = articles[0]
    ARTICLE_BY_ID[article.article_id] = article

    _publish_articles(
        articles=[article],
        chat_id=chat_id,
        thread_id=thread_id,
        origin_chat=origin_chat,
        context="get",
    )


def handle_automatic_forward(message: Dict[str, Any]) -> bool:
    """React to channel auto-forwards by dropping the first comment bundle."""
    telegram = _ensure_telegram()
    if not message.get("is_automatic_forward"):
        return False

    chat = message["chat"]
    chat_id = chat["id"]
    sender_chat = message.get("sender_chat") or {}
    source_channel_id = sender_chat.get("id")
    logger.info(
        "Auto-forward arrived",
        chat_id=chat_id,
        source_channel_id=source_channel_id,
        message_id=message.get("message_id"),
        thread_id=message.get("message_thread_id"),
    )

    if CHANNEL_ID is not None and source_channel_id != CHANNEL_ID:
        logger.warning(
            "Auto-forward source mismatch",
            source_channel_id=source_channel_id,
            expected_channel_id=CHANNEL_ID,
        )
        return False

    origin_msg_id = None
    fwd = message.get("forward_origin") or {}
    if fwd.get("type") == "channel":
        origin_msg_id = fwd.get("message_id")

    if origin_msg_id is None and message.get("forward_from_message_id") is not None:
        origin_msg_id = message.get("forward_from_message_id")

    if origin_msg_id is None:
        logger.warning("Auto-forward missing forward origin message id")
        return False

    # Claim the staged metadata keyed by the original channel message; this
    # is how we bridge the publish worker with the discussion thread.
    pending = pop_pending_post(origin_msg_id)
    if not pending:
        logger.warning(
            "Pending record not found for channel post",
            channel_post_id=origin_msg_id,
        )
        return False

    # Rehydrate the article and figure out where the discussion should live.
    target_chat_id = getattr(pending, "discussion_chat_id", None) or chat_id
    thread_id = message.get("message_thread_id")
    root_reply_id = message.get("message_id")

    ARTICLE_BY_ID[pending.article.article_id] = pending.article

    if send_comment_bundle(
        telegram,
        target_chat_id,
        pending.article,
        thread_id=thread_id,
        root_reply_id=root_reply_id,
    ):
        log_hint = f"thread_id={thread_id}" if thread_id is not None else f"reply_to={root_reply_id}"
        logger.info(
            "Posted discussion comment",
            chat_id=target_chat_id,
            detail=log_hint,
            article_id=getattr(pending.article, "article_id", None),
        )
        return True

    if store_pending_post(origin_msg_id, pending.article, pending.discussion_chat_id):
        logger.warning(
            "Failed to post comment; pending restored",
            channel_post_id=origin_msg_id,
            discussion_chat_id=pending.discussion_chat_id,
        )
    else:
        logger.error(
            "Failed to post comment and restore pending record",
            channel_post_id=origin_msg_id,
            discussion_chat_id=pending.discussion_chat_id,
        )
    return False


def handle_callback_query(update: Dict[str, Any]) -> None:
    """Process inline keyboard interactions for article detail buttons."""
    telegram = _ensure_telegram()

    callback = update.get("callback_query")
    if not callback:
        return

    data = callback.get("data") or ""
    query_id = callback["id"]
    message = callback.get("message")
    if not message:
        telegram.answer_callback_query(query_id, text="Message no longer available", show_alert=True)
        return

    chat = message["chat"]
    chat_id = chat["id"]
    chat_type = chat.get("type")
    thread_id = message.get("message_thread_id")
    message_id = message["message_id"]

    logger.info(
        "Callback received",
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        is_topic=message.get("is_topic_message"),
    )

    _, _, article_id = data.partition(":")
    article = ARTICLE_BY_ID.get(article_id)
    if not article:
        telegram.answer_callback_query(query_id, text="That article has expired", show_alert=True)
        return

    if data.startswith("details:"):
        response_text = format_highlights(article)
        disable_preview = True
    elif data.startswith("comments:"):
        response_text = format_comments(article)
        disable_preview = True
    elif data.startswith("refs:"):
        response_text = format_references(article)
        disable_preview = False
    else:
        telegram.answer_callback_query(query_id, text="Unsupported action")
        return

    if chat_type == "channel":
        telegram.answer_callback_query(query_id, text="Use these buttons in the discussion thread.")
        return

    is_forum = False
    try:
        chat_info = telegram.get_chat(chat_id)
        is_forum = bool(chat_info.get("is_forum"))
    except TelegramAPIError as err:
        logger.warning("getChat failed", error=format_http_error(err), chat_id=chat_id)

    root_reply_id = (message.get("reply_to_message") or {}).get("message_id") or message_id

    if is_forum and thread_id is not None:
        try:
            telegram.send_message(
                chat_id,
                response_text,
                thread_id=thread_id,
                disable_preview=disable_preview,
            )
            telegram.answer_callback_query(query_id, text="Sent ✅")
            try:
                telegram.set_message_reaction(chat_id, message_id)
            except TelegramAPIError:
                pass
            return
        except TelegramAPIError as err:
            err_text = format_http_error(err).lower()
            logger.warning(
                "Thread send failed, retrying as reply",
                error=err_text,
                chat_id=chat_id,
                thread_id=thread_id,
            )

    try:
        # Fallback to plain replies so buttons still work when topics are disabled.
        telegram.send_message(
            chat_id,
            response_text,
            reply_to=root_reply_id,
            disable_preview=disable_preview,
        )
        telegram.answer_callback_query(query_id, text="Sent ✅")
        try:
            telegram.set_message_reaction(chat_id, message_id)
        except TelegramAPIError:
            pass
    except TelegramAPIError as err:
        logger.error(
            "Callback send failed",
            error=format_http_error(err),
            chat_id=chat_id,
            message_id=message_id,
        )
        telegram.answer_callback_query(query_id, text="Couldn't send the response.", show_alert=False)
