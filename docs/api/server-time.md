# Server time: current date and time with timezone

## Why

A remote client (Grafoide, a voice assistant) must answer "what date and time
is it" with the clock of the host running GRAFENO, where tasks and schedules
run, not with the clock of its own device, and without timezone ambiguity.

## Request

- REST: `GET /api/v1/time` (no parameters).
- WebSocket: `{"id": 1, "method": "time", "params": {}}`.

Same optional authentication as the rest of the API (401 only when tokens are
configured).

## Response

```json
{
  "iso": "2026-10-21T00:51:06-05:00",
  "utc": "2026-10-21T05:51:06Z",
  "epoch": 1792561866,
  "epoch_ms": 1792561866000,
  "date": "2026-10-21",
  "time": "00:51:06",
  "weekday": "Wednesday",
  "iso_weekday": 3,
  "timezone": "America/Bogota",
  "tz_abbreviation": "-05",
  "utc_offset": "UTC-05:00",
  "utc_offset_minutes": -300
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `iso` | string | Server local time, ISO 8601 with explicit offset. |
| `utc` | string | Same instant in UTC, ending in `Z`. |
| `epoch` | integer | Unix time in seconds. |
| `epoch_ms` | integer | Unix time in milliseconds. |
| `date` | string | Local calendar date (`YYYY-MM-DD`). |
| `time` | string | Local wall-clock time (`HH:MM:SS`, 24 h). |
| `weekday` | string | English weekday name of the local date. |
| `iso_weekday` | integer | 1 = Monday ... 7 = Sunday. |
| `timezone` | string | IANA zone name, or empty when not reliable. |
| `tz_abbreviation` | string | Zone abbreviation reported by the host (informative). |
| `utc_offset` | string | Offset label (`UTC-05:00`), same format as the task schedule. |
| `utc_offset_minutes` | integer | Offset from UTC in minutes (east positive). |

## Rules

- `iso` always carries an explicit offset; `utc` ends in `Z`; `iso`, `utc` and `epoch` are the same instant.
- `date`, `time` and `weekday` are the server local wall-clock values.
- `timezone` is best effort (order: `TZ`, `/etc/localtime`, `/etc/timezone`) and is empty when the candidate does not match the real offset; use `utc_offset` when it is empty.
- `tz_abbreviation` is informative only (it may be `-05`); do not use it for calculations.
- `weekday` is always English; the client translates it to the user language.

## Compatibility

Added in GRAFENO 1.87.0. Older servers answer `404 {"error": "not found"}` over
REST and `{"error": {"message": "unknown method: time"}}` over WebSocket. A
client can detect this with `GET /api/v1/status` (`version`) and fall back to
its local clock, warning that it is not the server time.

## Notes for Grafoide

- The tool that answers the time must call `time`.
- Say date and time in the user language from `date`/`time`/`weekday`, and include the zone (`timezone`, or `utc_offset` when empty).
- For relative calculations ("in 2 hours") use `epoch`.
- Do not cache the value for more than a few seconds.
