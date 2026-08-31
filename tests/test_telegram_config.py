"""Tests of the Telegram configuration section."""

from __future__ import annotations

from grafeno import config
from grafeno.config import (
    DEFAULT_STT_MODEL,
    DEFAULT_STT_URL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_URL,
    DEFAULT_TTS_VOICE,
    Config,
    TelegramConfig,
)


def test_telegram_defaults():
    tg = TelegramConfig()
    assert tg.enabled is False
    assert tg.bot_token == ""
    assert tg.allowed_chat_ids == ""
    assert tg.parser_cli == ""
    assert tg.parser_model == ""
    assert tg.confirm_create is True
    assert tg.group_all is False
    assert tg.default_workdir == ""
    assert tg.stt_url == DEFAULT_STT_URL
    assert tg.stt_model == "whisper-large-v3-turbo"
    assert tg.tts_enabled is False
    assert tg.tts_url == DEFAULT_TTS_URL
    assert tg.tts_model == "canopylabs/orpheus-v1-english"
    assert tg.tts_voice == "troy"  # default voice is male


def test_telegram_from_dict_defaults():
    tg = TelegramConfig.from_dict({})
    assert tg.stt_url == DEFAULT_STT_URL
    assert tg.stt_model == DEFAULT_STT_MODEL
    assert tg.tts_url == DEFAULT_TTS_URL
    assert tg.tts_model == DEFAULT_TTS_MODEL
    assert tg.tts_voice == DEFAULT_TTS_VOICE


def test_telegram_roundtrip():
    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.bot_token = "123:abc"
    cfg.telegram.allowed_chat_ids = "111, 222"
    cfg.telegram.parser_cli = "kimi"
    cfg.telegram.parser_model = "kimi-code/k3"
    cfg.telegram.confirm_create = False
    cfg.telegram.group_all = True
    cfg.telegram.default_workdir = "/tmp/proyecto"
    cfg.telegram.stt_url = "https://stt.example.com/v1/audio/transcriptions"
    cfg.telegram.stt_key = "stt-key"
    cfg.telegram.stt_model = "whisper-1"
    cfg.telegram.tts_enabled = True
    cfg.telegram.tts_url = "https://api.openai.com/v1/audio/speech"
    cfg.telegram.tts_key = "tts-key"
    cfg.telegram.tts_model = "tts-1"
    cfg.telegram.tts_voice = "onyx"
    config.save(cfg)

    loaded = config.load().telegram
    assert loaded.enabled is True
    assert loaded.bot_token == "123:abc"
    assert loaded.allowed_chat_ids == "111, 222"
    assert loaded.parser_cli == "kimi"
    assert loaded.parser_model == "kimi-code/k3"
    assert loaded.confirm_create is False
    assert loaded.group_all is True
    assert loaded.default_workdir == "/tmp/proyecto"
    assert loaded.stt_url == "https://stt.example.com/v1/audio/transcriptions"
    assert loaded.stt_key == "stt-key"
    assert loaded.stt_model == "whisper-1"
    assert loaded.tts_enabled is True
    assert loaded.tts_url == "https://api.openai.com/v1/audio/speech"
    assert loaded.tts_key == "tts-key"
    assert loaded.tts_model == "tts-1"
    assert loaded.tts_voice == "onyx"


def test_telegram_absent_section_uses_defaults():
    """A config file without [telegram] loads the section with defaults."""
    loaded = Config.from_dict({})
    assert loaded.telegram.enabled is False
    assert loaded.telegram.stt_url == DEFAULT_STT_URL


def test_chat_ids_parsing():
    tg = TelegramConfig(allowed_chat_ids="123, 456 ,x,, -7")
    assert tg.chat_ids() == {123, 456, -7}
    assert TelegramConfig().chat_ids() == set()


def test_token_env_overrides_file(monkeypatch):
    tg = TelegramConfig(bot_token="file-token")
    monkeypatch.setenv("GRAFENO_TELEGRAM_TOKEN", "env-token")
    assert tg.resolve_token() == "env-token"
    monkeypatch.delenv("GRAFENO_TELEGRAM_TOKEN")
    assert tg.resolve_token() == "file-token"
    assert TelegramConfig().resolve_token() == ""


def test_stt_key_env_overrides_file(monkeypatch):
    tg = TelegramConfig(stt_key="file-key")
    monkeypatch.setenv("GRAFENO_TELEGRAM_STT_KEY", "env-key")
    assert tg.resolve_stt_key() == "env-key"
    monkeypatch.delenv("GRAFENO_TELEGRAM_STT_KEY")
    assert tg.resolve_stt_key() == "file-key"


def test_tts_key_falls_back_to_stt(monkeypatch):
    tg = TelegramConfig(stt_key="stt-key", tts_key="")
    assert tg.resolve_tts_key() == "stt-key"  # same provider: reuse STT key
    tg.tts_key = "tts-key"
    assert tg.resolve_tts_key() == "tts-key"
    monkeypatch.setenv("GRAFENO_TELEGRAM_TTS_KEY", "env-tts")
    assert tg.resolve_tts_key() == "env-tts"


def test_masked_token():
    assert TelegramConfig(bot_token="1234567890").masked_token() == "…7890"
    assert TelegramConfig(bot_token="ab").masked_token() == "…"
    assert TelegramConfig().masked_token() == ""


def test_keys_tolerate_pasted_labels_and_whitespace():
    """'groq gsk_…', 'Bearer <key>' or padded values resolve to the key."""
    tg = TelegramConfig(stt_key="groq gsk_real123")
    assert tg.resolve_stt_key() == "gsk_real123"
    tg = TelegramConfig(stt_key="  gsk_padded  ")
    assert tg.resolve_stt_key() == "gsk_padded"
    tg = TelegramConfig(tts_key="Bearer tts-9")
    assert tg.resolve_tts_key() == "tts-9"
    assert TelegramConfig(bot_token="  tok \n").resolve_token() == "tok"
