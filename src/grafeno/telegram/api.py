"""Minimal Telegram Bot API client (stdlib only, long polling).

HTTP is blocking (``urllib``); the service layer wraps every call with
``asyncio.to_thread``. The transport is injectable (``opener``) so tests
never touch the network. The bot token is part of the request URL: it is
never included in error messages, return values or logs.
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096       # Telegram message text limit
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # bots may download files up to 20 MB
POLL_TIMEOUT = 30.0          # long polling timeout (seconds)
CA_BUNDLE_ENV = "GRAFENO_SSL_CA_BUNDLE"  # custom CA bundle (corporate proxies)
# urllib's default UA ("Python-urllib/3.x") is blocked by Cloudflare on some
# providers (e.g. Groq: HTTP 403 error 1010); a product UA goes through.
USER_AGENT = "grafeno/1.33 (+https://github.com/vvbv/GRAFENO)"


def ssl_context() -> ssl.SSLContext:
    """TLS context for the bot's HTTPS calls.

    Resolution order: ``GRAFENO_SSL_CA_BUNDLE``, ``REQUESTS_CA_BUNDLE``,
    the certifi bundle (when installed) and finally the OpenSSL defaults
    of the interpreter. python.org macOS builds ship without root
    certificates (their "Install Certificates.command" installs certifi),
    which is the usual cause of CERTIFICATE_VERIFY_FAILED errors.
    """
    cafile = os.environ.get(CA_BUNDLE_ENV, "").strip() or os.environ.get(
        "REQUESTS_CA_BUNDLE", ""
    ).strip()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def is_cert_error(exc: BaseException) -> bool:
    """True if the exception carries a CERTIFICATE_VERIFY_FAILED failure."""
    return "CERTIFICATE_VERIFY_FAILED" in repr(exc)


def _unverified_context() -> ssl.SSLContext:
    """TLS context without certificate verification (fallback only)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    """Default blocking opener for every HTTPS call of the bot.

    Policy: certificate errors are ignored — a request that fails with
    CERTIFICATE_VERIFY_FAILED is retried once without verification, so the
    bot works on interpreters without root certificates (python.org macOS
    builds) and behind TLS-intercepting proxies. Verified contexts are
    still used first whenever they work.
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        if not is_cert_error(exc):
            raise
        with urllib.request.urlopen(
            request, timeout=timeout, context=_unverified_context()
        ) as response:
            return response.read()

# opener(request, timeout) -> response body bytes
Opener = Callable[[urllib.request.Request, float], bytes]


class TelegramError(Exception):
    """Bot API call failed. ``status`` is the HTTP/Telegram error code (0 for
    network errors); ``retry_after`` is set on 429 responses; ``cert_error``
    flags SSL certificate verification failures (they need user action)."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        retry_after: float = 0.0,
        cert_error: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.cert_error = cert_error


@dataclass
class TgMessage:
    """Subset of a Telegram message relevant to the bot."""

    message_id: int
    chat_id: int
    from_id: int = 0
    text: str = ""
    caption: str = ""
    voice_file_id: str = ""
    photo_file_id: str = ""   # biggest size only
    video_file_id: str = ""
    video_name: str = ""
    chat_type: str = "private"        # private | group | supergroup | channel
    reply_to_from_id: int = 0         # sender id of the replied-to message


@dataclass
class CallbackQuery:
    """Subset of a Telegram callback query (inline buttons)."""

    id: str
    chat_id: int
    message_id: int
    data: str = ""
    from_id: int = 0


@dataclass
class Update:
    update_id: int
    message: TgMessage | None = None
    callback: CallbackQuery | None = None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_message(raw: dict[str, Any]) -> TgMessage | None:
    """Tolerant parse of a Telegram message dict (None if unusable)."""
    if not isinstance(raw, dict):
        return None
    chat = raw.get("chat") or {}
    chat_id = _int(chat.get("id"))
    if not chat_id:
        return None
    photos = raw.get("photo") or []
    photo_file_id = ""
    if isinstance(photos, list) and photos:
        # Sizes come ascending: the last one is the biggest.
        biggest = photos[-1] if isinstance(photos[-1], dict) else {}
        photo_file_id = str(biggest.get("file_id", ""))
    voice = raw.get("voice") or {}
    video = raw.get("video") or {}
    sender = raw.get("from") or {}
    reply_to = raw.get("reply_to_message") or {}
    reply_sender = reply_to.get("from") or {}
    return TgMessage(
        message_id=_int(raw.get("message_id")),
        chat_id=chat_id,
        from_id=_int(sender.get("id")),
        text=str(raw.get("text", "") or ""),
        caption=str(raw.get("caption", "") or ""),
        voice_file_id=str(voice.get("file_id", "") or ""),
        photo_file_id=photo_file_id,
        video_file_id=str(video.get("file_id", "") or ""),
        video_name=str(video.get("file_name", "") or "video.mp4"),
        chat_type=str(chat.get("type", "private") or "private"),
        reply_to_from_id=_int(reply_sender.get("id")),
    )


def parse_callback(raw: dict[str, Any]) -> CallbackQuery | None:
    """Tolerant parse of a callback query dict (None if unusable)."""
    if not isinstance(raw, dict):
        return None
    message = raw.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = _int(chat.get("id"))
    query_id = str(raw.get("id", ""))
    if not chat_id or not query_id:
        return None
    sender = raw.get("from") or {}
    return CallbackQuery(
        id=query_id,
        chat_id=chat_id,
        message_id=_int(message.get("message_id")),
        data=str(raw.get("data", "") or ""),
        from_id=_int(sender.get("id")),
    )


def parse_update(raw: dict[str, Any]) -> Update | None:
    """Parse one update dict; None when it carries nothing we handle."""
    if not isinstance(raw, dict):
        return None
    update_id = _int(raw.get("update_id"))
    if not update_id:
        return None
    message = parse_message(raw.get("message") or {})
    callback = parse_callback(raw.get("callback_query") or {})
    if message is None and callback is None:
        return None
    return Update(update_id=update_id, message=message, callback=callback)


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split ``text`` into chunks under the Telegram message limit.

    Splits on line boundaries when possible so Markdown survives better.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit  # no good line boundary: hard cut
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _encode_multipart(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data body; returns (body, content_type)."""
    boundary = f"grafeno-{secrets.token_hex(16)}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for name, (filename, data, mime) in files.items():
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8")
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    return default_opener(request, timeout)


class TelegramBotClient:
    """Blocking Bot API client; every method raises TelegramError on failure."""

    def __init__(self, token: str, *, opener: Opener | None = None):
        self._token = token
        self._opener = opener or _default_opener

    # ------------------------------------------------------------------ #
    # HTTP core
    # ------------------------------------------------------------------ #
    def _request_raw(self, url: str, body: bytes, content_type: str, timeout: float) -> bytes:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": content_type, "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            return self._opener(request, timeout)
        except urllib.error.HTTPError as exc:
            self._raise_http(exc)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(
                f"network error: {reason}", status=0, cert_error=is_cert_error(exc)
            ) from exc
        raise TelegramError("unexpected opener path", status=0)  # unreachable

    def _raise_http(self, exc: urllib.error.HTTPError) -> None:
        """Translate an HTTP error into TelegramError (token never included)."""
        description = ""
        retry_after = 0.0
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            description = str(payload.get("description", ""))
            parameters = payload.get("parameters") or {}
            retry_after = float(parameters.get("retry_after", 0) or 0)
        except (ValueError, TypeError):
            pass
        detail = description or exc.reason or "HTTP error"
        raise TelegramError(
            f"HTTP {exc.code}: {detail}", status=exc.code, retry_after=retry_after
        ) from exc

    def _call(
        self,
        method: str,
        fields: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST to ``/bot<token>/<method>`` and return the ``result`` payload."""
        url = f"{API_BASE}/bot{self._token}/{method}"
        fields = {key: value for key, value in (fields or {}).items() if value is not None}
        if files:
            # Multipart requires flat string fields; dicts travel as JSON text.
            flat = {
                key: (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
                for key, value in fields.items()
            }
            body, content_type = _encode_multipart(flat, files)
        else:
            body = json.dumps(fields, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        raw = self._request_raw(url, body, content_type, timeout)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise TelegramError("invalid JSON in response", status=0) from exc
        if not payload.get("ok"):
            parameters = payload.get("parameters") or {}
            raise TelegramError(
                f"api error: {payload.get('description', '?')}",
                status=_int(payload.get("error_code")),
                retry_after=float(parameters.get("retry_after", 0) or 0),
            )
        return payload.get("result")

    # ------------------------------------------------------------------ #
    # API methods
    # ------------------------------------------------------------------ #
    def get_me(self) -> dict[str, Any]:
        """Validate the token; returns the bot user payload."""
        return self._call("getMe", timeout=15.0)

    def get_updates(self, offset: int, *, timeout: float = POLL_TIMEOUT) -> list[Update]:
        """Long polling; returns parsed updates (unusable ones are dropped)."""
        result = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": int(timeout),
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 10.0,
        )
        updates = []
        for raw in result if isinstance(result, list) else []:
            update = parse_update(raw)
            if update is not None:
                updates.append(update)
        return updates

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Send a text message, splitting it over the 4096-char limit."""
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk, "reply_markup": markup},
            )

    def send_document(self, chat_id: int, path: Path, *, caption: str = "") -> None:
        """Send a file (e.g. a task .md artifact) as a document."""
        data = path.read_bytes()
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise TelegramError("file too large to send", status=0)
        self._call(
            "sendDocument",
            {"chat_id": chat_id, "caption": caption[:1024]},
            {"document": (path.name, data, "application/octet-stream")},
            timeout=60.0,
        )

    def send_voice(self, chat_id: int, data: bytes, *, filename: str = "voice.wav") -> None:
        """Send a generated voice note (TTS audio bytes)."""
        self._call(
            "sendVoice",
            {"chat_id": chat_id},
            {"voice": (filename, data, "audio/wav")},
            timeout=60.0,
        )

    def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        """Acknowledge an inline-button tap (stops the client spinner)."""
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:200]},
        )

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict[str, Any]
    ) -> None:
        """Replace the inline keyboard of a sent message (picker toggles)."""
        self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Send a chat action (e.g. ``typing``); clients show it for ~5s."""
        self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    def get_file_path(self, file_id: str) -> str:
        """Resolve a file_id to its download path on Telegram servers."""
        result = self._call("getFile", {"file_id": file_id})
        file_path = str(result.get("file_path", "")) if isinstance(result, dict) else ""
        if not file_path or file_path.startswith("/") or ".." in file_path.split("/"):
            raise TelegramError("invalid file path from getFile", status=0)
        return file_path

    def download_file(self, file_path: str) -> bytes:
        """Download a file by its path (size-capped at MAX_DOWNLOAD_BYTES)."""
        url = f"{API_BASE}/file/bot{self._token}/{file_path}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        try:
            data = self._opener(request, 60.0)
        except urllib.error.HTTPError as exc:
            self._raise_http(exc)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(
                f"network error: {reason}", status=0, cert_error=is_cert_error(exc)
            ) from exc
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise TelegramError("file too large to download", status=0)
        return data
