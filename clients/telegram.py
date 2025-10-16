"""Telegram Bot API helper with basic retry logic."""

import time
from typing import Any, Dict

import requests


class TelegramAPIError(RuntimeError):
    """Raised when Telegram Bot API returns an error or the HTTP request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code = None,
        description = None,
        response: requests.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.description = description
        self.response = response


class TelegramClient:
    """Minimal Telegram Bot API client with retry-aware request helpers."""

    def __init__(
        self,
        token: str,
        *,
        base_url = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff: float = 0.4,
        session: requests.Session | None = None,
    ) -> None:
        """Construct a Telegram Bot API client with basic retry/backoff."""
        if not token:
            raise ValueError("Telegram token is required")
        # Allow overriding base URL for self-hosted gateways or tests.
        self.base_url = base_url or f"https://api.telegram.org/bot{token}"
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff = max(0.0, backoff)
        self.session = session or requests.Session()

    def _request(
        self,
        method: str,
        *,
        http_method: str = "POST",
        params: Dict[str, Any] | None = None,
        json_data: Dict[str, Any] | None = None,
    ) -> Any:
        """Perform an API call with exponential backoff and structured errors."""
        url = f"{self.base_url}/{method}"
        last_error: TelegramAPIError | None = None

        # Keep the retry/backoff local so the bot process can absorb transient
        # API hiccups without letting callers worry about reconstructing state.
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    http_method,
                    url,
                    params=params,
                    json=json_data,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = TelegramAPIError(f"HTTP request failed: {exc}")
            else:
                try:
                    payload = response.json()
                except ValueError:
                    detail = response.text
                    last_error = TelegramAPIError(
                        "Failed to decode Telegram response",
                        status_code=response.status_code,
                        description=detail,
                        response=response,
                    )
                else:
                    if response.status_code != 200:
                        detail = None
                        if isinstance(payload, dict):
                            detail = payload.get("description")
                        detail = detail or response.text
                        last_error = TelegramAPIError(
                            f"HTTP {response.status_code}: {detail}",
                            status_code=response.status_code,
                            description=detail,
                            response=response,
                        )
                    elif not payload.get("ok"):
                        detail = payload.get("description") or str(payload)
                        last_error = TelegramAPIError(
                            f"Telegram API error: {detail}",
                            status_code=response.status_code,
                            description=detail,
                            response=response,
                        )
                    else:
                        return payload["result"]

            if attempt + 1 == self.max_retries:
                break

            sleep_for = self.backoff * (2 ** attempt)
            if sleep_for:
                time.sleep(sleep_for)

        if last_error is None:
            last_error = TelegramAPIError("Telegram request failed without specific error")
        raise last_error

    # Public helpers -----------------------------------------------------

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> Any:
        """Long-poll for bot updates optionally starting after a given offset."""
        params: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._request("getUpdates", http_method="GET", params=params) or []

    def get_chat(self, chat_id: int) -> Any:
        """Fetch chat metadata (forum flags, linked ids, etc.)."""
        return self._request("getChat", http_method="GET", params={"chat_id": chat_id})

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to = None,
        thread_id = None,
        reply_markup: Dict[str, Any] | None = None,
        parse_mode: str = "HTML",
        disable_preview: bool = False,
        allow_without_reply: bool | None = None,
    ) -> Any:
        """Send a text message with optional threads, replies, and keyboards."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_to is not None:
            reply_parameters: Dict[str, Any] = {"message_id": reply_to}
            if allow_without_reply is not None:
                reply_parameters["allow_sending_without_reply"] = allow_without_reply
            payload["reply_parameters"] = reply_parameters
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", json_data=payload)

    def send_photo(
        self,
        chat_id: int,
        photo_url: str,
        *,
        caption: str,
        thread_id = None,
        reply_markup: Dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> Any:
        """Send a photo attachment with caption and optional inline buttons."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendPhoto", json_data=payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> Any:
        """Acknowledge an inline callback query with optional toast feedback."""
        payload = {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert}
        return self._request("answerCallbackQuery", json_data=payload)

    def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        *,
        emoji: str = "✅",
    ) -> None:
        """React to a message, primarily for button acknowledgement UX."""
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        }
        self._request("setMessageReaction", json_data=payload)
