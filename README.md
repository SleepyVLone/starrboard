# arr-dashboard

A self-healing automation pipeline for a home media server (Sonarr, Radarr, qBittorrent). Three stdlib-only Python scripts, no pip dependencies, running unattended on cron/systemd.

## What it does

- **arr-dashboard.py** - a web dashboard (built on Python's `http.server`, no framework) showing a countdown to the next scheduled cleanup run and a readable history of what each run did.
- **arr-queue-cleaner.py** - runs every 30 minutes. Detects and fixes dead torrents, malware disguised as media files, redundant downloads, stuck imports, low disk space, and download-queue misconfigurations, without ever guessing when it isn't sure (uncertain cases are logged for manual review instead of acted on).
- **arr-health-check.py** - runs every 10 minutes. Catches faster-moving problems: a VPN container restarting underneath the download client, a torrent stalled at 0 seeds, a stuck download queue, and a frozen background command in Sonarr (restarting it automatically if it stays wedged, with a cooldown so it can't restart-loop).

## Design principles

- Every deploy backs up the previous version of the file first.
- All persisted state is written atomically and fails safe (a corrupt or empty state file is treated as empty, not fatal).
- Nothing is auto-fixed unless the script is highly confident; anything uncertain is logged for a human to look at.
- Repeated manual-review items are deduplicated so they get logged once, not every single run.

## Configuration

None of the scripts hardcode credentials. They read connection details from environment variables:

```
SONARR_URL, SONARR_API_KEY
RADARR_URL, RADARR_API_KEY
QBIT_URL, QBIT_USER, QBIT_PASS
```

See `.env.example` for the full list. Export these (directly, via your shell profile, or via the `Environment=` directives in a systemd unit) before running any of the three scripts.

## Running

```
python3 arr-dashboard.py       # serves the dashboard on :8099
python3 arr-queue-cleaner.py   # one cleanup pass
python3 arr-health-check.py    # one health-check pass
```

In production these are wired up via cron (`arr-queue-cleaner.py` at :17/:47, `arr-health-check.py` every 10 minutes) and a systemd service (`arr-dashboard.py`, `Restart=always`).
