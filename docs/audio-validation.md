# Audio validation report (Telegram + GRAFENO API)

Date: 2026-09-10
Host: macOS, Python 3.14.4
GRAFENO version: 1.56.0
Provider: Groq (`https://api.groq.com/openai/v1/...`), STT model `whisper-large-v3-turbo`, TTS model `canopylabs/orpheus-v1-english`, voice `troy`.

Keys were redacted everywhere (`gsk_...Ibvb`).

## Scope

Three audio flows were reviewed:

- **Telegram TTS (output voice)**: `src/grafeno/telegram/service.py::_send` ->
  `_send_voice` (`tts.synthesize` + `tts.to_ogg` with external ffmpeg;
  falls back to `send_audio` with WAV when OGG is unavailable). Verified
  by reading the code; the bot itself was not driven by an automated
  client in this run (the manual checklist at the bottom is left for the
  user).
- **Telegram STT (input voice)**: `_handle_voice` downloads the voice
  note, feeds it through `stt.transcribe` to feed the intent parser.
  Verified by direct call (see `Live validation`).
- **GRAFENO REST API - speech synthesis**: new
  `POST /api/v1/audio/speech` endpoint (`src/grafeno/server/rest.py`),
  backed by `actions.synthesize_speech`. Returns the WAV from the
  provider or the OGG/OPUS after `tts.to_ogg`; maps failures to 400 /
  502 / 503 cleanly.
- **GRAFENO REST API - audio intake**: `POST /api/v1/tasks` now accepts
  `attachments` with audio file names. The attachment is saved under
  `media/` (already did this for images) and transcribed async with
  `stt.transcribe`; the transcription is appended to the task
  description and the response carries a per-attachment `transcriptions`
  array. The WebSocket method `tasks.create` gains the same behaviour
  transparently (the handler gained `await`).

Key files:

- `src/grafeno/media.py` (`AUDIO_SUFFIXES`, `is_audio_name`,
  `save_attachment` now honours audio suffixes)
- `src/grafeno/server/httpcore.py` (`MAX_BODY` raised to 8 MiB,
  `REASONS[502] = "Bad Gateway"`)
- `src/grafeno/server/actions.py` (`create_task` is async and calls
  `_transcribe_audio_attachments`; new `synthesize_speech`)
- `src/grafeno/server/rest.py` (new `_audio_speech` route,
  `dispatch_rest` lets handlers return raw `Response` for binary bodies)
- `src/grafeno/server/ws.py` (`_tasks_create` awaits `create_task`)
- `tests/test_api_audio.py` (new), `tests/test_media.py` (audio suffix
  test added)
- `docs/api/openapi.yaml` (version bump to 1.2.0, attachments +
  transcriptions schema, new `/api/v1/audio/speech` path)

## Automated checks

- `tests/test_api_audio.py` (9 cases): wav ok, ogg ok, ogg 503 when
  to_ogg returns None, 503 when no TTS key, 400 on missing text or
  unknown format, 502 on provider failure, audio transcription happy
  path, best-effort transcription on STT failure, and non-audio
  attachment skips STT entirely.
- `tests/test_telegram_tts.py` (existing): synthesize ok / truncation
  / not configured / network error / HTTP error message. Passed.
- `tests/test_telegram_stt.py` (existing): covered by the unit tests
  already present; no regressions.
- `tests/test_media.py`: existing 17 tests plus the new
  `test_save_attachment_keeps_audio_suffix` case.

Full suite result (`/usr/bin/env python3 -m pytest -q`):

```
1 failed, 890 passed, 1 warning in 112.70s
```

The single failure (`tests/test_console_pty.py::
test_default_shell_runs_and_exits_cleanly`) is unrelated to this work
and reproduces on `main` without my changes (a flaky PTY timing test in
the console subsystem). Every test in the audio surface and the REST /
WebSocket server passes.

## Live validation (configured keys)

Two scripts were executed against the real configured provider
credentials (`~/.grafeno/config.toml`). Keys are masked below.

### 1. Provider round-trip (TTS -> STT)

The plan asked for a TTS -> STT round-trip with the configured keys.

Result: `synthesize` returns `None` with this reason from Groq:

```
HTTP 400: The model `canopylabs/orpheus-v1-english` requires terms
acceptance. Please have the org admin accept the terms at
https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english
```

The provider rejects the configured TTS model because the org admin
has not yet accepted the model terms at the Groq console. The same
rejection happens with the alternative `canopylabs/orpheus-arabic-saudi`
(only two TTS models are exposed by the account), and the API surfaces
the failure cleanly with `on_error` carrying the redacted reason and
the API key stripped.

- WAV magic check: not executed (no audio returned by the provider).
- OGG conversion: not executed (TTS reached `None` before `to_ogg`).
- STT with the WAV: not executed (no WAV produced end-to-end).
- STT with a separately generated WAV: working. The STT key alone is
  sufficient; a WAV (RIFF/WAVE/PCM 16-bit/22050 Hz mono, 59 KiB,
  produced by macOS `say` + `ffmpeg` locally with the literal text
  "Hello world test message") was transcribed by whisper to
  `"Hello World Test Message"` without any error.

This is the expected behaviour for a provider misconfiguration: GRAFENO
returns 502 on `/api/v1/audio/speech` with the redacted error message,
and the audio intake path keeps working because it only needs the STT
key. The resolution is operator-side: open the URL in the message and
accept the model terms.

### 2. REST API live validation (`POST /api/v1/audio/speech` and
`POST /api/v1/tasks`)

Server bound on `127.0.0.1:<ephemeral>`, no tokens (anonymous access per
the default `[api]` config).

`POST /api/v1/audio/speech` with `{"text": "GRAFENO API speech
validation."}` and default `wav` format: **502** with body
`{"error": "tts provider error: HTTP 400: The model
`canopylabs/orpheus-v1-english` requires terms acceptance. Please have
the org admin accept the terms at https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english"}`.
This is the documented provider-error path: the redacted reason is
returned to the client and the full reason is appended to the per-run
`~/.grafeno/api.log` (`tts synthesis failed: ...`).

`POST /api/v1/audio/speech` with `{"text": "x", "format": "ogg"}`:
also **502** for the same provider error. The OGG conversion path is
not exercised because TTS reaches `None` first; `to_ogg` is reached
only after a successful synthesize.

`POST /api/v1/tasks` with one attachment `voice.wav` (the same WAV used
above, base64-encoded; raw size 59 168 bytes):

- Status **201** (created).
- Response `transcriptions == [{"name": "voice.wav", "text": "Hello
  World Test Message"}]`.
- Task description contains the section
  `Transcription of audio attachment 'voice.wav':\nHello World Test
  Message\n`.
- Media dir contains `media-01.wav` (59 168 bytes).
- Workdir is the temporary directory used by the validator
  (`/var/folders/.../grafeno-audio-live-...`); the real
  `~/.grafeno/tasks/` of the user was NOT touched.

Validation errors (live):

- `{"text": ""}` -> **400** `{"error": "text is required"}`.
- `{"text": "x", "format": "mp3"}` -> **400** `{"error": "format must
  be wav or ogg"}`.

These match the contract in `docs/api/openapi.yaml` (`/audio/speech`
operation 400 description) and the corresponding unit tests.

### 3. Telegram manual checklist

Static review of `src/grafeno/telegram/service.py`:

- `_send` invokes `_send_voice` only when `cfg.tts_enabled` is True
  (the existing opt-in flag). Confirmed.
- `_send_voice` resolves the key with `resolve_tts_key()`, calls
  `synthesize` (which truncates via `MAX_TTS_INPUT = 1500`), converts
  via `to_ogg`, and uses `send_voice`; on missing OGG conversion it
  falls back to `send_audio` with `filename="voice.wav"`. Confirmed.
- Failures of TTS are logged in `~/.grafeno/telegram.log` with the key
  redacted by `_http_error_detail` (same redaction pattern as the API
  log line above). Confirmed.
- `_handle_voice` downloads the voice note, calls `stt.transcribe`
  with `filename="voice.ogg"`, and reports the reason to the chat
  (`tg.stt.failed_reason` translation key) when STT fails. Confirmed.

Manual steps the user can run (require a Telegram client):

- MANUAL: send a voice note to the bot -> the note is transcribed and
  the reply includes a voice bubble (because `tts_enabled = true` in
  the config of this instance). Failure modes surface in the chat as a
  reason; the key never appears.
- MANUAL: `grep -i tts ~/.grafeno/telegram.log | tail` shows no recent
  errors after the manual run. With the provider rejection documented
  above, the FIRST attempt will log a provider error; subsequent
  attempts keep the same shape.

## Known limits

- `MAX_TTS_INPUT = 1500` chars. Voice replies are short summaries; the
  Telegram bot already trims, the API endpoint does not. Clients should
  pre-trim if needed.
- `MAX_AUDIO_BYTES = 25 * 1024 * 1024` (`telegram/stt.py`) is the
  provider-side cap; the API server accepts bodies up to 8 MiB
  (`httpcore.MAX_BODY`). Base64 inflates by ~33%, so the practical
  single-attachment audio cap is around 6 MiB raw audio / 8 MiB body.
- OGG/OPUS output requires `ffmpeg` on the host. Without it the API
  returns 503 and the bot falls back to sending the WAV as an audio
  file (`send_audio`).
- The API TTS endpoint is on-demand and only needs the configured
  TTS/STT key; it does NOT require `telegram.tts_enabled` (that flag
  only gates the automatic voice reply of the Telegram bot).
- Rejection of the configured TTS model on this account: see
  `Live validation` step 1.
