"""Tests of the stdlib Telegram Bot API client (fake transport, no network)."""

from __future__ import annotations

import io
import json
import urllib.error
from collections import deque

import pytest

from grafeno.telegram import api
from grafeno.telegram.api import TelegramBotClient, TelegramError

TOKEN = "TOKEN123"


class FakeHTTP:
    """Injectable opener: serves queued (status, payload) responses."""

    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected request")
        item = self.responses.popleft()
        if isinstance(item, Exception):
            raise item
        status, payload = item
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if status == 200:
            return body
        raise urllib.error.HTTPError(
            request.full_url, status, "error", {}, io.BytesIO(body)
        )


def _ok(result):
    return (200, {"ok": True, "result": result})


def _client(http: FakeHTTP) -> TelegramBotClient:
    return TelegramBotClient(TOKEN, opener=http)


# ---------------------------------------------------------------------- #
# Parsing
# ---------------------------------------------------------------------- #
def test_parse_message_text():
    raw = {
        "message_id": 5,
        "chat": {"id": 555},
        "from": {"id": 42},
        "text": "hola",
    }
    msg = api.parse_message(raw)
    assert msg is not None
    assert msg.chat_id == 555
    assert msg.from_id == 42
    assert msg.text == "hola"


def test_parse_message_photo_picks_biggest():
    raw = {
        "message_id": 5,
        "chat": {"id": 555},
        "caption": "mira esto",
        "photo": [
            {"file_id": "small", "width": 90},
            {"file_id": "big", "width": 1280},
        ],
    }
    msg = api.parse_message(raw)
    assert msg is not None
    assert msg.photo_file_id == "big"
    assert msg.caption == "mira esto"


def test_parse_message_voice_and_video():
    raw = {
        "message_id": 5,
        "chat": {"id": 555},
        "voice": {"file_id": "vf"},
        "video": {"file_id": "vid", "file_name": "clip.mp4"},
    }
    msg = api.parse_message(raw)
    assert msg is not None
    assert msg.voice_file_id == "vf"
    assert msg.video_file_id == "vid"
    assert msg.video_name == "clip.mp4"


def test_parse_message_without_chat_is_none():
    assert api.parse_message({"message_id": 5}) is None
    assert api.parse_message("nope") is None


def test_parse_update_message_and_callback():
    update = api.parse_update({
        "update_id": 10,
        "message": {"message_id": 1, "chat": {"id": 5}, "text": "x"},
    })
    assert update is not None and update.message is not None and update.callback is None
    update = api.parse_update({
        "update_id": 11,
        "callback_query": {
            "id": "cb1",
            "data": "tg:c:abcd",
            "from": {"id": 42},
            "message": {"message_id": 2, "chat": {"id": 5}},
        },
    })
    assert update is not None and update.callback is not None
    assert update.callback.data == "tg:c:abcd"
    assert update.callback.chat_id == 5


def test_parse_update_unusable_is_none():
    assert api.parse_update({}) is None
    assert api.parse_update({"update_id": 0}) is None
    assert api.parse_update("nope") is None


# ---------------------------------------------------------------------- #
# split_message
# ---------------------------------------------------------------------- #
def test_split_message_short():
    assert api.split_message("hola") == ["hola"]


def test_split_message_splits_on_line_boundaries():
    line = "x" * 100
    text = "\n".join([line] * 50)  # ~5050 chars
    chunks = api.split_message(text, limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert len(chunks) > 1


def test_split_message_hard_cut_without_newlines():
    text = "y" * 5000
    chunks = api.split_message(text, limit=1000)
    assert len(chunks) == 5
    assert "".join(chunks) == text


# ---------------------------------------------------------------------- #
# API calls
# ---------------------------------------------------------------------- #
def test_get_updates_sends_offset_and_parses():
    http = FakeHTTP([_ok([
        {"update_id": 100, "message": {"message_id": 1, "chat": {"id": 5}, "text": "a"}},
        {"update_id": 101},  # unusable: dropped
    ])])
    updates = _client(http).get_updates(100)
    assert len(updates) == 1
    assert updates[0].update_id == 100
    body = json.loads(http.requests[0].data.decode())
    assert body["offset"] == 100
    assert body["timeout"] == int(api.POLL_TIMEOUT)
    assert f"bot{TOKEN}/getUpdates" in http.requests[0].full_url


def test_send_message_splits_and_markup_goes_last():
    text = "z" * 5000
    markup = {"inline_keyboard": [[{"text": "Crear", "callback_data": "tg:c:1"}]]}
    http = FakeHTTP([_ok({}), _ok({})])
    _client(http).send_message(555, text, reply_markup=markup)
    assert len(http.requests) == 2
    first = json.loads(http.requests[0].data.decode())
    second = json.loads(http.requests[1].data.decode())
    assert "reply_markup" not in first
    assert second["reply_markup"] == markup
    assert second["chat_id"] == 555  # native JSON types, not strings


def test_send_message_api_error_raises():
    http = FakeHTTP([(200, {"ok": False, "error_code": 400, "description": "bad"})])
    with pytest.raises(TelegramError) as excinfo:
        _client(http).send_message(555, "hola")
    assert excinfo.value.status == 400
    assert "bad" in str(excinfo.value)


def test_http_error_includes_retry_after():
    payload = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": 7},
    }
    http = FakeHTTP([(429, payload)])
    with pytest.raises(TelegramError) as excinfo:
        _client(http).get_updates(0)
    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 7.0


def test_errors_never_leak_the_token():
    http = FakeHTTP([(401, {"ok": False, "description": "Unauthorized"})])
    with pytest.raises(TelegramError) as excinfo:
        _client(http).get_me()
    assert excinfo.value.status == 401
    assert TOKEN not in str(excinfo.value)


def test_network_error_maps_to_status_zero():
    http = FakeHTTP([urllib.error.URLError("connection refused")])
    with pytest.raises(TelegramError) as excinfo:
        _client(http).get_updates(0)
    assert excinfo.value.status == 0
    assert TOKEN not in str(excinfo.value)


def test_invalid_json_response_raises():
    http = FakeHTTP([(200, b"<html>not json</html>")])
    with pytest.raises(TelegramError):
        _client(http).get_me()


def test_send_document_builds_multipart(tmp_path):
    target = tmp_path / "01-plan.md"
    target.write_text("# Plan\ncontenido", encoding="utf-8")
    http = FakeHTTP([_ok({})])
    _client(http).send_document(555, target, caption="plan")
    request = http.requests[0]
    content_type = request.headers.get("Content-type", "")
    assert "multipart/form-data" in content_type
    body = request.data
    assert b'filename="01-plan.md"' in body
    assert b"# Plan\ncontenido" in body
    assert b'name="chat_id"' in body


def test_send_voice_builds_multipart():
    http = FakeHTTP([_ok({})])
    _client(http).send_voice(555, b"RIFF....WAVE", filename="voice.wav")
    body = http.requests[0].data
    assert b'filename="voice.wav"' in body
    assert b"RIFF....WAVE" in body


def test_answer_callback_query():
    http = FakeHTTP([_ok(True)])
    _client(http).answer_callback_query("cb-1", "vale")
    body = json.loads(http.requests[0].data.decode())
    assert body["callback_query_id"] == "cb-1"
    assert body["text"] == "vale"


def test_send_chat_action():
    http = FakeHTTP([_ok(True)])
    _client(http).send_chat_action(555, "typing")
    body = json.loads(http.requests[0].data.decode())
    assert body == {"chat_id": 555, "action": "typing"}
    assert "sendChatAction" in http.requests[0].full_url


def test_get_file_path_ok_and_traversal_rejected():
    http = FakeHTTP([_ok({"file_path": "voice/file_1.oga"})])
    assert _client(http).get_file_path("vf") == "voice/file_1.oga"

    http = FakeHTTP([_ok({"file_path": "../secrets"})])
    with pytest.raises(TelegramError):
        _client(http).get_file_path("vf")

    http = FakeHTTP([_ok({})])
    with pytest.raises(TelegramError):
        _client(http).get_file_path("vf")


def test_download_file_size_cap(monkeypatch):
    monkeypatch.setattr(api, "MAX_DOWNLOAD_BYTES", 8)
    http = FakeHTTP([(200, b"0123456789")])
    with pytest.raises(TelegramError):
        _client(http).download_file("voice/file.oga")


def test_download_file_ok():
    http = FakeHTTP([(200, b"AUDIO")])
    assert _client(http).download_file("voice/file.oga") == b"AUDIO"


# ---------------------------------------------------------------------- #
# SSL / TLS context
# ---------------------------------------------------------------------- #
def test_ssl_context_prefers_grafeno_bundle(monkeypatch, tmp_path):
    """GRAFENO_SSL_CA_BUNDLE wins over every other CA source."""
    recorded = {}

    def fake_context(*, cafile=None):
        recorded["cafile"] = cafile
        return object()

    monkeypatch.setattr(api.ssl, "create_default_context", fake_context)
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("dummy", encoding="utf-8")
    monkeypatch.setenv("GRAFENO_SSL_CA_BUNDLE", str(bundle))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/other.pem")

    api.ssl_context()

    assert recorded["cafile"] == str(bundle)


def test_ssl_context_falls_back_to_requests_bundle(monkeypatch):
    recorded = {}

    def fake_context(*, cafile=None):
        recorded["cafile"] = cafile
        return object()

    monkeypatch.setattr(api.ssl, "create_default_context", fake_context)
    monkeypatch.delenv("GRAFENO_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/requests-ca.pem")

    api.ssl_context()

    assert recorded["cafile"] == "/requests-ca.pem"


def test_ssl_context_uses_certifi_when_present(monkeypatch):
    """With no env bundles, certifi's CA bundle is used when installed."""
    import sys
    import types

    recorded = {}

    def fake_context(*, cafile=None):
        recorded["cafile"] = cafile
        return object()

    fake_certifi = types.ModuleType("certifi")
    fake_certifi.where = lambda: "/certifi/cacert.pem"
    monkeypatch.setitem(sys.modules, "certifi", fake_certifi)
    monkeypatch.setattr(api.ssl, "create_default_context", fake_context)
    monkeypatch.delenv("GRAFENO_SSL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    api.ssl_context()

    assert recorded["cafile"] == "/certifi/cacert.pem"


def test_ssl_context_defaults_without_certifi(monkeypatch):
    """Without env bundles nor certifi, the interpreter defaults are used."""
    import sys

    recorded = {}

    def fake_context(*, cafile=None):
        recorded["cafile"] = cafile
        return object()

    monkeypatch.setitem(sys.modules, "certifi", None)  # import raises ImportError
    monkeypatch.setattr(api.ssl, "create_default_context", fake_context)
    monkeypatch.delenv("GRAFENO_SSL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    api.ssl_context()

    assert recorded["cafile"] is None


def test_default_opener_passes_ssl_context(monkeypatch):
    """The shared opener always hands an SSL context to urlopen."""
    import ssl as ssl_module

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"OK"

    def fake_urlopen(request, timeout, context=None):
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    request = api.urllib.request.Request("https://example.com", data=b"")
    assert api.default_opener(request, 1.0) == b"OK"
    assert isinstance(captured["context"], ssl_module.SSLContext)


def test_default_opener_ignores_cert_errors(monkeypatch):
    """Policy: CERTIFICATE_VERIFY_FAILED is retried without verification."""
    import ssl as ssl_module

    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"OK"

    def fake_urlopen(request, timeout, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise urllib.error.URLError(
                ssl_module.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] x")
            )
        return FakeResponse()

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    request = api.urllib.request.Request("https://example.com", data=b"")
    assert api.default_opener(request, 1.0) == b"OK"
    assert len(calls) == 2  # verified first, unverified retry
    assert calls[1].verify_mode == ssl_module.CERT_NONE
    assert calls[1].check_hostname is False


def test_default_opener_does_not_retry_other_network_errors(monkeypatch):
    """Only certificate errors are ignored; other network errors propagate."""

    calls = []

    def fake_urlopen(request, timeout, context=None):
        calls.append(context)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    request = api.urllib.request.Request("https://example.com", data=b"")
    with pytest.raises(urllib.error.URLError):
        api.default_opener(request, 1.0)
    assert len(calls) == 1


def test_cert_error_is_flagged():
    """CERTIFICATE_VERIFY_FAILED surfaces as TelegramError with cert_error=True."""
    import ssl as ssl_module

    url_error = urllib.error.URLError(
        ssl_module.SSLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate"
        )
    )
    http = FakeHTTP([url_error])
    with pytest.raises(TelegramError) as excinfo:
        _client(http).get_updates(0)
    assert excinfo.value.cert_error is True
    assert excinfo.value.status == 0
    assert TOKEN not in str(excinfo.value)


def test_is_cert_error_detection():
    import ssl as ssl_module

    assert api.is_cert_error(urllib.error.URLError(
        ssl_module.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] x")
    ))
    assert not api.is_cert_error(urllib.error.URLError("connection refused"))
    assert not api.is_cert_error(TelegramError("boom"))
