# MotherDuck

The MotherDuck export copies the local database to
[MotherDuck](https://motherduck.com) (hosted DuckDB) after every sync, so the
data can be queried with SQL from anywhere — a dashboard, a notebook or a
scheduled AI agent — while the Mac is asleep.

## Install

DuckDB is an optional extra:

```bash
uv tool install --force "screentime-exporter[mcp,motherduck] @ git+https://github.com/gradient-here/screentime@motherduck-sink"
```

## Configure

Create an access token in MotherDuck (*Settings → Access Tokens*), then add
to `~/.config/screentime/config.toml` (mode `0600`):

```toml
[motherduck]
database = "screentime"
token = "…"
```

`MOTHERDUCK_DATABASE` and `MOTHERDUCK_TOKEN` override the file. Both must be
set; a token alone does not enable the export. The database is created on the
first export. `screentime doctor --online` opens it to test the token.

Re-send everything, for example after the first install:

```bash
screentime export --from 2026-01-01
```

## Tables

All days are local days in the configured `timezone`; durations in seconds.

| Table | One row per | Columns |
|---|---|---|
| `devices` | device | `device_id`, `name`, `platform`, `first_seen_at`, `last_seen_at` |
| `sessions` | app session | `event_key`, `device_id`, `day`, `started_at`, `duration_s`, `bundle_id`, `title`, `category` |
| `daily_device_stats` | device, day | `duration_s`, `late_duration_s` (00–06), `deep_night_duration_s` (03–06), `event_count`, `first_activity_at`, `last_activity_at` |
| `daily_app_stats` | device, day, app | `bundle_id`, `title`, `category`, `duration_s`, `event_count` |
| `daily_category_stats` | device, day, category | `category`, `duration_s`, `event_count` |

Changed (device, day) pairs are deleted and rewritten in one transaction, so
exports are idempotent; `devices` is replaced on every export.

```sql
SELECT d.name, s.day, round(s.duration_s / 3600, 1) AS hours
FROM daily_device_stats s JOIN devices d USING (device_id)
WHERE s.day >= current_date - 7
ORDER BY s.day, d.name;
```
