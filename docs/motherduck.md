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

## Troubleshooting

Start with these; every problem below shows up in at least one of them:

```bash
screentime doctor --online                    # opens MotherDuck, reports the last run
tail -n 40 ~/Library/Logs/screentime/screentime.log      # one JSON summary per run
tail -n 20 ~/Library/Logs/screentime/screentime.err.log
sqlite3 ~/Library/Application\ Support/screentime/screentime.db \
  "SELECT id, status, started_at, points_written, error FROM runs ORDER BY id DESC LIMIT 5"
launchctl kickstart gui/$(id -u)/io.github.nichtlegacy.screentime   # run the agent now
```

`motherduck_status` in the run summary is `success`, `failed` or `skipped`
(not configured). A failed export never loses data: the cursor only moves
after a successful write, so the next run resends everything since then.

### The agent has no Full Disk Access

Symptom: `last_run: partial` with `Operation not permitted` on
`~/Library/Biome/…` or `knowledgeC.db`, `events_read: 0`, while
`screentime run` from the terminal works. MotherDuck still reports `success`
because there was nothing new to send — the data just stops growing.

Full Disk Access is granted to the real Python binary at the end of the tool
environment's symlink chain, and that path contains the patch version, e.g.
`~/.local/share/uv/python/cpython-3.13.7-macos-aarch64-none/bin/python3.13`.
It breaks when uv moves the tool to a different interpreter: `uv python
upgrade`, or a `uv tool install --force` that resolves a newer Python. Find
the current path and add it under *System Settings → Privacy & Security →
Full Disk Access* (**+**, Cmd+Shift+G):

```bash
readlink -f ~/.local/share/uv/tools/screentime-exporter/bin/python
```

Your terminal needs its own grant for manual runs; the terminal's grant does
not cover the agent. Install with `--managed-python` so the tool never lands
on a Homebrew, pyenv or python.org interpreter whose path changes with
unrelated upgrades.

### DuckDB version not supported by MotherDuck

Symptom: `Your DuckDB version (vX.Y.Z) is not yet supported by MotherDuck`.
MotherDuck supports DuckDB releases with a delay, so the `motherduck` extra
pins `duckdb<2`. When a reinstall picks up an unsupported version, tighten
the pin in `pyproject.toml` to the latest version named in the error, run
`uv lock`, push, and reinstall. Check what is installed with:

```bash
~/.local/share/uv/tools/screentime-exporter/bin/python -c "import duckdb; print(duckdb.__version__)"
```

### The token is in my shell but the agent doesn't see it

launchd does not read your shell profile, so `export MOTHERDUCK_TOKEN=…` only
works for commands run in that terminal. The agent needs `token` in the
`[motherduck]` section of `~/.config/screentime/config.toml` (mode `0600`).
`screentime setup` keeps that section when it rewrites the file.

### Token expired or revoked

Symptom: `doctor --online` fails with an authentication error. Create a new
token in MotherDuck and replace `token` in the config. Use a read/write token
here; give anything that only reads (a scheduled agent, a dashboard) a
separate **read-only** token so it can be revoked on its own.

### First connection downloads an extension

DuckDB fetches the `motherduck` extension into
`~/.duckdb/extensions/<duckdb version>/` on first use, per DuckDB version.
After a DuckDB upgrade the first run needs internet access to download it
again; an offline Mac fails that run and recovers on the next one.

### Data is stale but every check is OK

The agent only runs while the Mac is awake and logged in, and iPhone/iPad
data only arrives when iCloud has synced it to the Mac (Screen Time → Share
Across Devices on every device). `doctor` warns per device after 3 days
without new usage. MotherDuck is exactly as fresh as the local database.
