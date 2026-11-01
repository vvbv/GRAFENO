# CLI quota: available usage of the agent CLIs

## Why

Show how much usage is left in the windows (5 hours, weekly, monthly) of each
agent CLI that takes part in the general roles or in a processing profile, so a
client (Grafoide, scripts, the TUI and the web panel) can decide what to run.
Values are per GRAFENO instance.

## Request

- REST: `GET /api/v1/quota` (live, throttled) or `GET /api/v1/quota?cached=1`.
- WebSocket: `{"id": 1, "method": "quota.get", "params": {"cached": true}}`.

Same optional authentication as the rest of the API.

## Live versus cached

- Live (default): queries the providers. Two live requests within
  `live_min_interval` seconds (60) return the last snapshot with
  `throttled: true`, `retry_after`, a `warning` and a `Retry-After` header
  (REST); the providers are not called again.
- Cached (`cached=1`): returns the snapshot, refreshed at most every
  `refresh_seconds` (300). This is what the TUI and the web panel use.

## Status of each CLI

- `ok`: `windows` hold the data (`source`: `api`, `stream` or `rollout`).
- `auth_expired`: the CLI token expired; it is refreshed the next time the CLI runs.
- `no_credentials`: no login found for the CLI on the host.
- `no_data`: nothing readable yet (for example no Codex session on disk).
- `unsupported`: the CLI exposes no usage source (opencode, cursor, minimax).
- `not_installed`: the executable is not present.
- `error`: the provider failed; `message` has a short technical detail.

`stale: true` means `windows` come from an earlier successful fetch while the
current status is a failure.

## Response

```json
{
  "instance_name": "GRAFENO",
  "fetched_at": "2026-11-01T04:50:00-05:00",
  "refresh_seconds": 300,
  "live_min_interval": 60,
  "mode": "live",
  "throttled": false,
  "retry_after": 0,
  "warning": "",
  "clis": [
    {
      "cli": "claude", "status": "ok", "source": "api", "message": "",
      "fetched_at": "2026-11-01T04:50:00-05:00", "stale": false,
      "installed": true, "used_by_default": true, "profiles": [],
      "windows": [
        {"key": "five_hour", "used_percent": 2.0, "remaining_percent": 98.0,
         "resets_at": "2026-11-01T09:49:59+00:00", "resets_in_seconds": 17999},
        {"key": "seven_day", "used_percent": 40.0, "remaining_percent": 60.0,
         "resets_at": "2026-11-04T21:59:59+00:00", "resets_in_seconds": 300000}
      ]
    },
    {
      "cli": "kimi", "status": "auth_expired", "source": "", "windows": [],
      "message": "token expired; it is refreshed the next time kimi runs",
      "fetched_at": "", "stale": false, "installed": true,
      "used_by_default": false, "profiles": ["fast"]
    }
  ]
}
```

## Recommendation for clients

Poll with `cached=1` every `refresh_seconds` and request live data only on an
explicit user action; respect `retry_after` when `throttled` is true.
