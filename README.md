# arr-dashboard

A self-healing automation pipeline for a home media server (Sonarr, Radarr, and a download client). Three stdlib-only Python programs, no pip dependencies, running unattended on cron/systemd.

## What it does

- **arr-dashboard.py** - a web dashboard (built on Python's `http.server`, no framework) showing a countdown to the next scheduled cleanup run and a readable history of what each run did.
- **arr-queue-cleaner.py** - runs every 30 minutes. Detects and fixes dead torrents, malware disguised as media files, redundant downloads, stuck imports, low disk space, and download-queue misconfigurations, without ever guessing when it isn't sure (uncertain cases are logged for manual review instead of acted on).
- **arr-health-check.py** - runs every 10 minutes. Catches faster-moving problems: a VPN container restarting underneath the download client, a torrent stalled at 0 seeds, a stuck download queue, and a frozen background command in Sonarr or Radarr (restarting whichever one is wedged automatically, with a per-service cooldown so it can't restart-loop).

## How the pieces fit together

This project sits on top of Sonarr, Radarr, and qBittorrent, it doesn't run them for you. Before pointing this at anything, those need to already be wired up to each other:

1. **Run Sonarr, Radarr, qBittorrent, and a media server (Jellyfin/Plex/Emby)**, however suits you: Docker Compose, separate containers, bare metal. This project only talks to their HTTP APIs, so it doesn't care how or where they're hosted. qBittorrent is commonly routed through a VPN container (gluetun, for example); `arr-health-check.py`'s VPN-orphan and port-sync checks assume that shape specifically, so if you don't run qBittorrent behind a VPN container, those two checks just won't find anything to do and can be ignored.

2. **Give Sonarr and Radarr a shared indexer source**, e.g. point Prowlarr at your indexers, then sync its Sonarr and Radarr app connections so both share the same indexer list without configuring each one twice.

3. **Add qBittorrent as a download client in both Sonarr and Radarr**, each with its own category (`tv-sonarr` for Sonarr, `movies-radarr` for Radarr are the defaults this project expects). The category names matter: `arr-queue-cleaner.py`'s stuck-import rescue and `arr-health-check.py`'s stall detection both filter qBittorrent's torrent list by category to know which download belongs to which app. Using different names is fine, just update `RESCUE_CATEGORIES` in `arr-queue-cleaner.py` and the category check in `arr-health-check.py` to match.

4. **Point Sonarr and Radarr's root folders at your real library paths** (one for TV, one for movies, more if you split anime, documentaries, K-dramas, etc. into their own libraries).

5. **Notify your media server on import.** Add an Emby/Jellyfin (or Plex) notification connection in both Sonarr and Radarr so new episodes and movies show up as soon as they're imported, instead of waiting on the next scheduled library scan.

6. **Point this project at all of it.** Once Sonarr, Radarr, and qBittorrent are running and talking to each other, fill in `.env` with their URLs, API keys, and qBittorrent credentials (see Setup below). This dashboard and automation layer just watches all three from the outside.

## Setup

**Requirements:** Python 3.8 or later. No pip packages to install, nothing else to build. You'll also need an existing Sonarr, Radarr, and qBittorrent instance to point it at, wired up as described above.

1. Clone the repo and move into it:

   ```
   git clone <this-repo-url>
   cd arr-dashboard
   ```

2. Copy the example environment file and fill in your real values:

   ```
   cp .env.example .env
   ```

   Edit `.env` with your Sonarr/Radarr URLs and API keys (found in each app's Settings > General) and your qBittorrent WebUI username/password.

3. Load those values into your shell (none of the scripts read `.env` files directly, they read real environment variables):

   ```
   set -a
   source .env
   set +a
   ```

4. Start the dashboard and open it in a browser:

   ```
   python3 PYTHON/arr-dashboard.py
   ```

   Then visit `http://localhost:8099`. If a variable is still blank, the script prints a clear warning naming it instead of failing silently.

5. Run `arr-queue-cleaner.py` and `arr-health-check.py` the same way whenever you want a one-off pass (see **Running** below for what each does and how they're normally scheduled).

## Project layout

```
PYTHON/            all Python source
  arr-dashboard.py       entry point: starts the web server
  arr-queue-cleaner.py   entry point: one 30-minute cleanup pass
  arr-health-check.py    entry point: one 10-minute health-check pass
  arr_common/            config, qBittorrent client, and state persistence shared by all three
  arr_dashboard/         dashboard-only code: data-fetching + the HTTP request handler
  tests.py               stdlib unittest suite for the pure-logic pieces (no live services needed)
HTML/              one .html file per dashboard page (structure only)
CSS/               one .css file per page
JS/                one .js file per page
bg.png             dashboard background image
```

Each dashboard page's HTML links its own CSS/JS file (`/css/<page>.css`, `/js/<page>.js`); the server reads all of it from the `HTML/`, `CSS/`, and `JS/` folders at startup and serves it directly, no build step or template engine involved.

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

See `.env.example` for the full list. Export these (directly, via your shell profile, or via the `Environment=` directives in a systemd unit) before running any of the three scripts. If a required variable is still blank at startup, each script logs a clear warning naming it rather than just quietly showing "Unreachable" everywhere with no explanation.

## Running

```
python3 PYTHON/arr-dashboard.py       # serves the dashboard on :8099
python3 PYTHON/arr-queue-cleaner.py   # one cleanup pass
python3 PYTHON/arr-health-check.py    # one health-check pass
```

In production these are wired up via cron (`arr-queue-cleaner.py` at :17/:47, `arr-health-check.py` every 10 minutes) and a systemd service (`arr-dashboard.py`, `Restart=always`).

## Running the tests

```
python3 PYTHON/tests.py -v
```

Stdlib `unittest`, no pip dependencies. Covers the pure-logic pieces that don't need a live Sonarr/Radarr/qBittorrent to test: byte formatting, next-run-time scheduling, log parsing, and the atomic/corrupt-safe state file pattern every script relies on.

## Known limitations

- **No authentication.** The dashboard and its API are wide open to anything that can reach the port. Fine on a trusted home LAN behind a router, not something to expose to the internet as-is.
