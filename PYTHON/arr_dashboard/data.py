import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from arr_common import config
from arr_common.qbittorrent import login as qbit_login
from arr_common.qbittorrent import get as qbit_get

SONARR = config.SONARR
RADARR = config.RADARR

LOG_PATH = os.environ.get("ARR_LOG_PATH", "/var/log/arr-queue-cleaner.log")
CRON_MINUTES = [17, 47]  # must match /etc/cron.d/arr-queue-cleaner
HEALTH_CHECK_CRON_MINUTES = [3, 13, 23, 33, 43, 53]  # must match /etc/cron.d/arr-health-check

# same thresholds the recurring health-check uses: a torrent actively
# downloading/stalled/fetching-metadata with 0 seeds and 0 speed for this
# long is treated as dead, not just slow
STALLED_STATES = {"metaDL", "stalledDL", "downloading"}
STALLED_MIN_AGE_SECONDS = 20 * 60

RUN_HEADER_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) === run ===$')

JELLYFIN_BASE = config.JELLYFIN_URL
JELLYFIN_KEY = config.JELLYFIN_API_KEY
YOUTUBE_ROOT = config.YOUTUBE_ROOT
YT_DLP_BIN = config.YT_DLP_BIN
YOUTUBE_RESOLUTIONS = {
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "best": "bv*+ba/b",
}
YOUTUBE_JOBS = {}
YOUTUBE_JOBS_LOCK = threading.Lock()


def get_active_downloads():
    """Currently-downloading items across Sonarr and Radarr, one entry per
    torrent (a season-pack download spans many per-episode queue rows in
    Sonarr, so those are grouped down to a single card)."""
    items = []

    for name, base, key in (SONARR, RADARR):
        try:
            req = urllib.request.Request(
                f"{base}/api/v3/queue?pageSize=1000&includeSeries=true&includeMovie=true",
                headers={"X-Api-Key": key},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except Exception:
            continue

        by_download = {}
        for rec in data.get("records", []):
            if rec.get("status") != "downloading":
                continue
            by_download.setdefault(rec.get("downloadId"), []).append(rec)

        for rows in by_download.values():
            r0 = rows[0]
            size = r0.get("size") or 0
            sizeleft = r0.get("sizeleft") or 0
            progress = round(100 * (1 - sizeleft / size), 1) if size else 0

            if name == "Sonarr":
                series = r0.get("series") or {}
                seasons = sorted({rec.get("seasonNumber") for rec in rows if rec.get("seasonNumber") is not None})
                title = series.get("title") or r0.get("title", "")
                subtitle = f"Season {seasons[0]}" if len(seasons) == 1 else "Multiple seasons"
                if len(rows) > 1:
                    subtitle += f" · {len(rows)} episodes"
                images = series.get("images", [])
            else:
                movie = r0.get("movie") or {}
                title = movie.get("title") or r0.get("title", "")
                year = movie.get("year")
                subtitle = str(year) if year else "Movie"
                images = movie.get("images", [])

            by_type = {img.get("coverType"): img.get("remoteUrl") for img in images}
            image = by_type.get("fanart") or by_type.get("poster")

            items.append({
                "title": title,
                "subtitle": subtitle,
                "progress": progress,
                "source": name,
                "image": image,
            })

    # yt-dlp downloads never touch qBittorrent/Sonarr/Radarr, so they need
    # folding in here separately from this same in-memory job dict.
    with YOUTUBE_JOBS_LOCK:
        jobs_snapshot = list(YOUTUBE_JOBS.values())
    for job in jobs_snapshot:
        if job["status"] not in ("starting", "downloading"):
            continue
        m = re.search(r'(\d{1,3}(?:\.\d)?)%', job.get("detail") or "")
        progress = float(m.group(1)) if m else 0
        items.append({
            "title": job["title"],
            "subtitle": "YouTube",
            "progress": progress,
            "source": "YouTube",
            "image": None,
        })

    items.sort(key=lambda i: -i["progress"])
    return items


def get_server_health():
    """Mirrors what the recurring chat-triggered health check watches for,
    surfaced here so it doesn't only exist transiently in conversation."""
    health = {}

    try:
        cookie = qbit_login(timeout=8)
        torrents = qbit_get("/api/v2/torrents/info", cookie, timeout=8)
        transfer = qbit_get("/api/v2/transfer/info", cookie, timeout=8)

        now = time.time()
        stalled = []
        for t in torrents:
            if t.get("state") not in STALLED_STATES:
                continue
            if (t.get("progress") or 0) >= 1.0:
                continue
            if (t.get("num_seeds") or 0) > 0:
                continue
            if (t.get("dlspeed") or 0) > 0:
                continue
            age = now - t.get("added_on", now)
            if age >= STALLED_MIN_AGE_SECONDS:
                stalled.append({"name": t["name"], "age_min": int(age / 60)})

        health["qbittorrent"] = {
            "reachable": True,
            "dl_speed": transfer.get("dl_info_speed", 0),
            "up_speed": transfer.get("up_info_speed", 0),
            "stalled_count": len(stalled),
            "stalled": stalled[:5],
        }
    except Exception:
        health["qbittorrent"] = {"reachable": False}

    started_total, queued_total, longest_minutes = 0, 0, 0
    now_utc = datetime.now(timezone.utc)
    any_reachable = False
    for _, base, key in (SONARR, RADARR):
        try:
            req = urllib.request.Request(f"{base}/api/v3/command", headers={"X-Api-Key": key})
            with urllib.request.urlopen(req, timeout=8) as r:
                commands = json.loads(r.read())
        except Exception:
            continue
        any_reachable = True

        started = [c for c in commands if c.get("status") == "started"]
        queued = [c for c in commands if c.get("status") == "queued"]
        started_total += len(started)
        queued_total += len(queued)

        for c in started:
            # Sonarr/Radarr's field is "started", not "startedOn" -- it's
            # usually populated for anything actually running, but queued is
            # always populated as a fallback for the rare case it isn't.
            ts = c.get("started") or c.get("queued")
            if not ts:
                continue
            try:
                started_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_minutes = (now_utc - started_at).total_seconds() / 60
                longest_minutes = max(longest_minutes, age_minutes)
            except ValueError:
                continue

    health["commands"] = {
        "reachable": any_reachable,
        "started_count": started_total,
        "queued_count": queued_total,
        "longest_running_minutes": round(longest_minutes, 1),
    }

    return health


# Sonarr/Radarr's internal command names -> plain English. Anything not
# listed falls back to its raw name so the modal never shows a blank.
FRIENDLY_COMMANDS = {
    "SeriesSearch": "Searching for a whole series",
    "SeasonSearch": "Searching for a full season",
    "EpisodeSearch": "Searching for specific episodes",
    "MissingEpisodeSearch": "Searching for all missing episodes",
    "RefreshSeries": "Refreshing series info from TheTVDB",
    "RenameSeries": "Renaming files to match the library",
    "RescanSeries": "Rescanning files on disk",
    "DownloadedEpisodesScan": "Scanning the downloads folder",
    "MoviesSearch": "Searching for missing movies",
    "MovieSearch": "Searching for this movie",
    "MissingMoviesSearch": "Searching for all missing movies",
    "RefreshMovie": "Refreshing info from the movie database",
    "RenameMovie": "Renaming files to match the library",
    "RescanMovie": "Rescanning files on disk",
    "DownloadedMoviesScan": "Scanning the downloads folder",
    "RefreshMonitoredDownloads": "Checking download progress",
    "ProcessMonitoredDownloads": "Importing finished downloads",
    "RssSync": "Checking indexers for new releases",
    "Backup": "Backing up the database",
    "RenameFiles": "Renaming files to match the library",
    "MessagingCleanup": "Routine housekeeping",
    "Housekeeping": "Routine housekeeping",
    "ApplicationUpdate": "Updating",
    "CheckHealth": "Running its own health check",
    "ManualImport": "Importing files you picked by hand",
    "ClearBlocklist": "Clearing the blocklist",
}


def _fetch_arr_commands(instance, title_lookup_path, title_id_field):
    """Running + queued commands for one Sonarr/Radarr instance, tagged with
    which service they came from. Independent per instance so one being
    unreachable doesn't hide the other's queue. Returns None if this instance
    couldn't be reached at all."""
    name, base, key = instance
    try:
        req = urllib.request.Request(f"{base}/api/v3/command", headers={"X-Api-Key": key})
        with urllib.request.urlopen(req, timeout=8) as r:
            cmds = json.loads(r.read())
    except Exception:
        return None

    # id -> title, so a queued search can say WHICH show/movie it's for
    # instead of a bare number. Best-effort; the queue still renders without it.
    titles = {}
    try:
        req = urllib.request.Request(f"{base}{title_lookup_path}", headers={"X-Api-Key": key})
        with urllib.request.urlopen(req, timeout=8) as r:
            titles = {item["id"]: item["title"] for item in json.loads(r.read())}
    except Exception:
        pass

    now_utc = datetime.now(timezone.utc)
    running, queued = [], []

    for c in cmds:
        status = c.get("status")
        if status not in ("started", "queued"):
            continue
        cmd_name = c.get("name", "")
        body = c.get("body", {}) or {}

        # A specific, human-readable detail line where we can build one.
        detail = (c.get("message") or "").strip()
        if not detail:
            item_id = body.get(title_id_field)
            if item_id in titles:
                detail = titles[item_id]
            elif cmd_name in ("EpisodeSearch",) and body.get("episodeIds"):
                n = len(body["episodeIds"])
                detail = f"{n} episode{'s' if n != 1 else ''}"

        entry = {
            "source": name,
            "friendly": FRIENDLY_COMMANDS.get(cmd_name, cmd_name),
            "raw_name": cmd_name,
            "detail": detail,
        }

        if status == "started":
            ts = c.get("started") or c.get("queued")
            secs = 0
            if ts:
                try:
                    secs = int((now_utc - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
                except ValueError:
                    pass
            entry["running_seconds"] = max(secs, 0)
            running.append(entry)
        else:
            entry["queued_at"] = c.get("queued") or ""
            queued.append(entry)

    return running, queued


def get_command_queue():
    """What Sonarr and Radarr are actually doing right now and what's
    waiting, in plain language. Powers the click-through on the command-queue
    card so the queue isn't a black box -- you can see exactly what it's
    chewing through."""
    running, queued = [], []
    any_reachable = False

    for instance, title_path, id_field in (
        (SONARR, "/api/v3/series", "seriesId"),
        (RADARR, "/api/v3/movie", "movieId"),
    ):
        result = _fetch_arr_commands(instance, title_path, id_field)
        if result is None:
            continue
        any_reachable = True
        running.extend(result[0])
        queued.extend(result[1])

    if not any_reachable:
        return {"reachable": False}

    queued.sort(key=lambda e: e["queued_at"])  # FIFO: next to run first
    running.sort(key=lambda e: -e["running_seconds"])
    return {"reachable": True, "running": running, "queued": queued}


def get_calendar_events(start_date, end_date):
    """Episodes/movies releasing in [start_date, end_date] (both YYYY-MM-DD),
    merged from Sonarr and Radarr into one flat event list the frontend can
    just group by date -- mirrors Sonarr's own calendar view but covers
    Radarr too."""
    events = []

    try:
        req = urllib.request.Request(
            f"{SONARR[1]}/api/v3/calendar?start={start_date}&end={end_date}&includeSeries=true",
            headers={"X-Api-Key": SONARR[2]},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            episodes = json.loads(r.read())
        for e in episodes:
            air = e.get("airDateUtc")
            if not air:
                continue
            series = e.get("series") or {}
            events.append({
                "date": air[:10],
                "time": air[11:16],
                "title": series.get("title", "Unknown series"),
                "subtitle": f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}",
                "source": "Sonarr",
                "has_file": bool(e.get("hasFile")),
            })
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            f"{RADARR[1]}/api/v3/calendar?start={start_date}&end={end_date}",
            headers={"X-Api-Key": RADARR[2]},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            movies = json.loads(r.read())
        for m in movies:
            date_key = None
            for field in ("digitalRelease", "physicalRelease", "inCinemas"):
                val = m.get(field)
                if val and start_date <= val[:10] <= end_date:
                    date_key = val[:10]
                    break
            if not date_key:
                continue
            events.append({
                "date": date_key,
                "time": "",
                "title": m.get("title", "Unknown movie"),
                "subtitle": str(m.get("year", "")),
                "source": "Radarr",
                "has_file": bool(m.get("hasFile")),
            })
    except Exception:
        pass

    return {"events": events}


def search_lookup(kind, term):
    """Thin wrapper over Sonarr's series lookup / Radarr's movie lookup --
    just search, no add. Trimmed to what a simple add UI needs; the full
    object gets re-fetched by exact ID at add time since that's what the
    POST actually needs (seasons, images, etc)."""
    if kind == "show":
        base, key = SONARR[1], SONARR[2]
        url = f"{base}/api/v3/series/lookup?term={urllib.parse.quote(term)}"
    else:
        base, key = RADARR[1], RADARR[2]
        url = f"{base}/api/v3/movie/lookup?term={urllib.parse.quote(term)}"

    req = urllib.request.Request(url, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        results = json.loads(r.read())

    trimmed = []
    for item in results[:20]:
        poster = None
        for img in item.get("images", []):
            if img.get("coverType") == "poster":
                poster = img.get("remoteUrl")
                break
        trimmed.append({
            "title": item.get("title"),
            "year": item.get("year"),
            "overview": (item.get("overview") or "")[:240],
            "poster": poster,
            "already_added": bool(item.get("id")),
            "series_type": item.get("seriesType"),
            "tvdb_id": item.get("tvdbId"),
            "tmdb_id": item.get("tmdbId"),
        })
    return {"results": trimmed}


def get_add_defaults(kind):
    """Quality profiles + root folders available to choose from, plus a
    sensible pre-selected default for each -- whichever is already the most
    commonly used across the existing library, so the common case is just
    search, pick, add with zero extra decisions."""
    base, key = (SONARR[1], SONARR[2]) if kind == "show" else (RADARR[1], RADARR[2])

    req = urllib.request.Request(f"{base}/api/v3/qualityprofile", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        profiles = json.loads(r.read())

    req2 = urllib.request.Request(f"{base}/api/v3/rootfolder", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req2, timeout=10) as r:
        folders = json.loads(r.read())

    library_path = "series" if kind == "show" else "movie"
    req3 = urllib.request.Request(f"{base}/api/v3/{library_path}", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req3, timeout=15) as r:
        library = json.loads(r.read())

    profile_counts = {}
    folder_counts = {}
    for item in library:
        profile_counts[item.get("qualityProfileId")] = profile_counts.get(item.get("qualityProfileId"), 0) + 1
        folder_counts[item.get("rootFolderPath")] = folder_counts.get(item.get("rootFolderPath"), 0) + 1

    default_profile = max(profile_counts, key=profile_counts.get) if profile_counts else (profiles[0]["id"] if profiles else None)
    default_folder = max(folder_counts, key=folder_counts.get) if folder_counts else (folders[0]["path"] if folders else None)

    result = {
        "profiles": [{"id": p["id"], "name": p["name"]} for p in profiles],
        "folders": [{"path": f["path"]} for f in folders],
        "default_profile_id": default_profile,
        "default_folder": default_folder,
    }

    if kind == "show":
        # seriesType (standard vs anime) isn't guessable from TVDB metadata --
        # it depends on how the show is actually released (weekly simulcast
        # with normal SxxEyy numbering vs fansub/BD releases needing anime
        # categories + absolute-numbering parsing), which only becomes
        # apparent once you've found the release. Sonarr's own lookup always
        # defaults it to "standard" regardless of genre, so we surface a
        # per-folder majority as a starting suggestion (most anime-folder
        # shows in this library are seriesType=anime, but not all -- e.g.
        # Tower of God/MASHLE are simulcast and correctly "standard") and let
        # the add flow override it explicitly rather than inherit a silent
        # default that's wrong roughly as often as it's right.
        type_counts_by_folder = {}
        for item in library:
            folder = item.get("rootFolderPath")
            stype = item.get("seriesType", "standard")
            counts = type_counts_by_folder.setdefault(folder, {})
            counts[stype] = counts.get(stype, 0) + 1

        def majority_type(folder):
            counts = type_counts_by_folder.get(folder)
            if not counts:
                return "standard"
            return max(counts, key=counts.get)

        for f in result["folders"]:
            f["default_series_type"] = majority_type(f["path"])
        result["default_series_type"] = majority_type(default_folder)

    return result


def add_media(kind, body):
    """Re-looks up the exact title by ID to get Sonarr/Radarr's full object
    (seasons, images, etc -- more than the trimmed search result carries),
    layers on the chosen profile/folder/monitoring, then adds it for real."""
    if kind == "show":
        base, key = SONARR[1], SONARR[2]
        term = f"tvdb:{body['tvdb_id']}"
        req = urllib.request.Request(
            f"{base}/api/v3/series/lookup?term={urllib.parse.quote(term)}",
            headers={"X-Api-Key": key},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            matches = json.loads(r.read())
        if not matches:
            raise ValueError("Could not re-look-up series by tvdbId")
        item = matches[0]
        item["qualityProfileId"] = int(body["profile_id"])
        item["rootFolderPath"] = body["root_folder"]
        item["monitored"] = True
        item["seasonFolder"] = True
        item["seriesType"] = body.get("series_type") or "standard"
        item["addOptions"] = {"searchForMissingEpisodes": True, "monitor": "all"}
        post_url = f"{base}/api/v3/series"
    else:
        base, key = RADARR[1], RADARR[2]
        term = f"tmdb:{body['tmdb_id']}"
        req = urllib.request.Request(
            f"{base}/api/v3/movie/lookup?term={urllib.parse.quote(term)}",
            headers={"X-Api-Key": key},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            matches = json.loads(r.read())
        if not matches:
            raise ValueError("Could not re-look-up movie by tmdbId")
        item = matches[0]
        item["qualityProfileId"] = int(body["profile_id"])
        item["rootFolderPath"] = body["root_folder"]
        item["monitored"] = True
        item["minimumAvailability"] = "released"
        item["addOptions"] = {"searchForMovie": True}
        post_url = f"{base}/api/v3/movie"

    req = urllib.request.Request(
        post_url, data=json.dumps(item).encode(), method="POST",
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def format_bytes(n):
    # binary units (GiB/TiB) to match Sonarr/Radarr's own stats footer
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def get_library(kind):
    """Full Sonarr/Radarr library as a poster grid -- read-only browsing,
    same data and the same 5-category status breakdown their own library
    views show (continuing/ended/missing-monitored/missing-unmonitored/
    downloading), not a management console."""
    base, key = (SONARR[1], SONARR[2]) if kind == "show" else (RADARR[1], RADARR[2])
    list_path = "series" if kind == "show" else "movie"

    req = urllib.request.Request(f"{base}/api/v3/{list_path}", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=20) as r:
        items = json.loads(r.read())

    profiles_req = urllib.request.Request(f"{base}/api/v3/qualityprofile", headers={"X-Api-Key": key})
    with urllib.request.urlopen(profiles_req, timeout=10) as r:
        profiles = {p["id"]: p["name"] for p in json.loads(r.read())}

    # which seriesId/movieId currently have something actively downloading,
    # so those can take priority over their missing/complete status
    downloading_ids = set()
    try:
        queue_req = urllib.request.Request(
            f"{base}/api/v3/queue?pageSize=1000", headers={"X-Api-Key": key},
        )
        with urllib.request.urlopen(queue_req, timeout=15) as r:
            queue = json.loads(r.read())
        id_field = "seriesId" if kind == "show" else "movieId"
        for rec in queue.get("records", []):
            if rec.get("status") == "downloading" and rec.get(id_field):
                downloading_ids.add(rec[id_field])
    except Exception:
        pass

    trimmed = []
    counts = {
        "total": 0, "monitored": 0, "unmonitored": 0,
        "continuing": 0, "ended": 0, "downloaded": 0,
        "missing_monitored": 0, "missing_unmonitored": 0,
        "downloading": 0, "episodes": 0, "files": 0, "size_bytes": 0,
    }

    for item in items:
        poster = None
        for img in item.get("images", []):
            if img.get("coverType") == "poster":
                poster = img.get("remoteUrl")
                break

        monitored = bool(item.get("monitored"))
        is_downloading = item.get("id") in downloading_ids

        if kind == "show":
            stats = item.get("statistics", {})
            has_file = stats.get("episodeFileCount", 0)
            total = stats.get("episodeCount", 0)
            size_bytes = stats.get("sizeOnDisk", 0)
            is_ended = bool(item.get("ended"))
        else:
            has_file = 1 if item.get("hasFile") else 0
            total = 1
            size_bytes = item.get("sizeOnDisk", 0)
            is_ended = False

        complete = total > 0 and has_file >= total

        if is_downloading:
            category = "downloading"
        elif complete:
            if kind == "show":
                category = "ended" if is_ended else "continuing"
            else:
                category = "downloaded"
        elif monitored:
            category = "missing_monitored"
        else:
            category = "missing_unmonitored"

        counts["total"] += 1
        counts["monitored" if monitored else "unmonitored"] += 1
        counts["episodes"] += total
        counts["files"] += has_file
        counts["size_bytes"] += size_bytes or 0
        counts[category] = counts.get(category, 0) + 1

        trimmed.append({
            "title": item.get("title"),
            "year": item.get("year"),
            "poster": poster,
            "monitored": monitored,
            "profile": profiles.get(item.get("qualityProfileId"), "Unknown"),
            "has_file": has_file,
            "total": total,
            "category": category,
        })

    trimmed.sort(key=lambda i: (i["title"] or "").lower())
    counts["size_display"] = format_bytes(counts["size_bytes"])
    return {"items": trimmed, "stats": counts}


def next_run_time(now=None, minutes=CRON_MINUTES):
    now = now or datetime.now()
    candidates = []
    for hour_offset in (0, 1):
        for minute in minutes:
            candidate = now.replace(minute=minute, second=0, microsecond=0) + timedelta(hours=hour_offset)
            if candidate > now:
                candidates.append(candidate)
    return min(candidates)


def parse_log():
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    runs = []
    current = None
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        m = RUN_HEADER_RE.match(line)
        if m:
            if current is not None:
                runs.append(current)
            current = {"timestamp": m.group(1), "lines": []}
        elif current is not None and line.strip():
            current["lines"].append(line)
    if current is not None:
        runs.append(current)
    runs.reverse()  # most recent first
    return runs


def summarize_run(run):
    text = "\n".join(run["lines"])
    action_markers = ("DEAD:", "SECURITY", "removed", "imported", "NEEDS MANUAL REVIEW", "ERROR")
    had_action = any(marker in text for marker in action_markers)
    return had_action


def safe_folder_name(title):
    """Collapse a free-typed title into something safe to use as a folder
    name -- strips path separators and other characters that would either
    break out of youtube/ or just look wrong on disk."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", title or "").strip()
    cleaned = re.sub(r'\s+', " ", cleaned)
    return cleaned[:100] or "Untitled"


def start_youtube_download(title, url, resolution):
    """Kicks off a background yt-dlp download into its own show/Season 01
    folder and returns a job id the Add page can poll for progress. Runs in
    a thread rather than blocking the request -- a channel backlog can take
    a long time, and the dashboard's single-threaded-per-request HTTP server
    still needs to serve other pages while it runs."""
    if not re.match(r'^https?://(www\.)?(youtube\.com|youtu\.be)/', url or ""):
        raise ValueError("URL must be a youtube.com or youtu.be link")

    fmt = YOUTUBE_RESOLUTIONS.get(resolution, YOUTUBE_RESOLUTIONS["1080p"])
    safe_title = safe_folder_name(title)

    job_id = uuid.uuid4().hex[:12]
    with YOUTUBE_JOBS_LOCK:
        YOUTUBE_JOBS[job_id] = {
            "title": safe_title,
            "status": "starting",
            "detail": "",
            "started": datetime.now().isoformat(),
        }

    thread = threading.Thread(target=_run_youtube_download, args=(job_id, safe_title, url, fmt), daemon=True)
    thread.start()
    return job_id


def _run_youtube_download(job_id, safe_title, url, fmt):
    show_dir = os.path.join(YOUTUBE_ROOT, safe_title)
    season_dir = os.path.join(show_dir, "Season 01")
    os.makedirs(season_dir, exist_ok=True)

    # playlist_index numbers episodes for a channel/playlist URL; autonumber
    # is the fallback for a single-video URL where playlist_index is unset.
    out_template = os.path.join(season_dir, "S01E%(playlist_index,autonumber)03d - %(title).150B [%(id)s].%(ext)s")
    # per-show archive file so re-submitting the same channel URL later (to
    # pick up new uploads) skips everything already downloaded.
    archive_path = os.path.join(show_dir, ".download-archive.txt")

    cmd = [
        YT_DLP_BIN,
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--download-archive", archive_path,
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "-o", out_template,
        url,
    ]

    with YOUTUBE_JOBS_LOCK:
        YOUTUBE_JOBS[job_id]["status"] = "downloading"

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if line:
                with YOUTUBE_JOBS_LOCK:
                    YOUTUBE_JOBS[job_id]["detail"] = line
        proc.wait()
        ok = proc.returncode == 0
        with YOUTUBE_JOBS_LOCK:
            YOUTUBE_JOBS[job_id]["status"] = "done" if ok else "error"
        if not ok:
            return
    except Exception as e:
        with YOUTUBE_JOBS_LOCK:
            YOUTUBE_JOBS[job_id]["status"] = "error"
            YOUTUBE_JOBS[job_id]["detail"] = str(e)
        return

    # yt-dlp writes a per-video .jpg alongside each .mp4 (matching basename),
    # which Jellyfin already picks up as that episode's thumbnail on its own.
    # A show only gets a poster/card image from one living in its own root,
    # so seed that from whichever video's thumbnail we just grabbed -- but
    # only the first time, so a later re-run for new uploads doesn't clobber
    # a poster you've since picked yourself in Jellyfin.
    poster_path = os.path.join(show_dir, "poster.jpg")
    if not os.path.exists(poster_path):
        thumbs = sorted(f for f in os.listdir(season_dir) if f.endswith(".jpg"))
        if thumbs:
            shutil.copyfile(os.path.join(season_dir, thumbs[0]), poster_path)

    # Jellyfin auto-identifies new items against IMDb/TMDb even with this
    # library's internet-provider settings off -- so an invented YouTube
    # title reliably gets fuzzy-matched to some unrelated real show/movie.
    # Rather than trust that toggle, we correct and hard-lock the metadata
    # ourselves once Jellyfin has scanned it in.
    with YOUTUBE_JOBS_LOCK:
        YOUTUBE_JOBS[job_id]["detail"] = "Downloaded -- fixing Jellyfin identification..."
    try:
        _delock_youtube_metadata(safe_title)
        with YOUTUBE_JOBS_LOCK:
            YOUTUBE_JOBS[job_id]["detail"] = "Downloaded and Jellyfin metadata corrected."
    except Exception as e:
        with YOUTUBE_JOBS_LOCK:
            YOUTUBE_JOBS[job_id]["detail"] = "Downloaded OK, but Jellyfin metadata cleanup failed: " + str(e)


def _jellyfin_get(path):
    req = urllib.request.Request(f"{JELLYFIN_BASE}{path}", headers={"X-Emby-Token": JELLYFIN_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _jellyfin_post(path, body=None):
    data_bytes = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        f"{JELLYFIN_BASE}{path}", data=data_bytes, method="POST",
        headers={"X-Emby-Token": JELLYFIN_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


_jellyfin_user_id_cache = None
_youtube_library_id_cache = None


def _jellyfin_user_id():
    global _jellyfin_user_id_cache
    if not _jellyfin_user_id_cache:
        users = _jellyfin_get("/Users")
        _jellyfin_user_id_cache = users[0]["Id"]
    return _jellyfin_user_id_cache


def _youtube_library_id():
    global _youtube_library_id_cache
    if not _youtube_library_id_cache:
        folders = _jellyfin_get("/Library/VirtualFolders")
        _youtube_library_id_cache = next(f["ItemId"] for f in folders if f["Name"] == "YouTube")
    return _youtube_library_id_cache


def _lock_clean_metadata(item_id, clean_name):
    """Rewrites an item's identity fields back to the plain local name and
    hard-locks it (LockData=True) so Jellyfin's scanner leaves it alone on
    every future scan, not just this one."""
    uid = _jellyfin_user_id()
    item = _jellyfin_get(f"/Users/{uid}/Items/{item_id}")
    item["Name"] = clean_name
    item["Overview"] = ""
    item["Genres"] = []
    item["GenreItems"] = []
    item["ProviderIds"] = {}
    item["ExternalUrls"] = []
    item["Tags"] = []
    item.pop("ProductionYear", None)
    item["LockData"] = True
    _jellyfin_post(f"/Items/{item_id}", item)


def _delock_youtube_metadata(safe_title):
    """Polls for the series Jellyfin just scanned in under /media/youtube/
    <safe_title>, then strips and locks any auto-matched metadata on it and
    its episodes. Runs after the download finishes, since Jellyfin's own
    identification pass needs a few seconds after the file lands."""
    _jellyfin_post("/Library/Refresh")
    library_id = _youtube_library_id()
    expected_path_part = f"/{safe_title}/"

    series_item = None
    for _ in range(40):
        time.sleep(3)
        items = _jellyfin_get(f"/Items?parentId={urllib.parse.quote(library_id)}&recursive=true&includeItemTypes=Series")
        for item in items.get("Items", []):
            if expected_path_part in (item.get("Path") or ""):
                series_item = item
                break
        if series_item:
            break
    if not series_item:
        # loud rather than silent -- the caller needs to know this genuinely
        # didn't happen, not report success when nothing was actually fixed
        raise TimeoutError(f"Jellyfin never scanned in '{safe_title}' within 120s -- metadata was NOT corrected")

    _lock_clean_metadata(series_item["Id"], safe_title)

    children = _jellyfin_get(f"/Items?parentId={urllib.parse.quote(series_item['Id'])}&recursive=true&includeItemTypes=Episode")
    for ep in children.get("Items", []):
        path = ep.get("Path") or ""
        stem = os.path.splitext(os.path.basename(path))[0]
        m = re.match(r'^S\d+E\d+ - (.+) \[[^\[\]]+\]$', stem)
        clean_ep_name = m.group(1) if m else stem
        _lock_clean_metadata(ep["Id"], clean_ep_name)
