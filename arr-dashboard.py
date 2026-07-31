#!/usr/bin/env python3
# Tiny dashboard for the arr-queue-cleaner auto-fix job: shows a countdown to
# the next scheduled run and a readable log of what each past run did.
# Stdlib only (no pip deps) so it runs anywhere python3 does.

import hashlib
import http.server
import json
import os
import re
import socketserver
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

LOG_PATH = "/var/log/arr-queue-cleaner.log"
BG_IMAGE_PATH = "/opt/arr-dashboard/bg.png"
CRON_MINUTES = [17, 47]  # must match /etc/cron.d/arr-queue-cleaner
HEALTH_CHECK_CRON_MINUTES = [3, 13, 23, 33, 43, 53]  # must match /etc/cron.d/arr-health-check
PORT = 8099

SONARR = ("Sonarr", os.environ.get("SONARR_URL", "http://localhost:8989"), os.environ.get("SONARR_API_KEY", ""))
RADARR = ("Radarr", os.environ.get("RADARR_URL", "http://localhost:7878"), os.environ.get("RADARR_API_KEY", ""))

QBIT_BASE = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")

# same thresholds the recurring health-check uses: a torrent actively
# downloading/stalled/fetching-metadata with 0 seeds and 0 speed for this
# long is treated as dead, not just slow
STALLED_STATES = {"metaDL", "stalledDL", "downloading"}
STALLED_MIN_AGE_SECONDS = 20 * 60

RUN_HEADER_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) === run ===$')

def sniff_image_content_type(data):
    """The background gets swapped out from time to time and isn't always
    actually the format its filename suggests, so detect the real type from
    the file's magic bytes rather than assuming PNG."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


try:
    with open(BG_IMAGE_PATH, "rb") as f:
        BG_IMAGE_BYTES = f.read()
except FileNotFoundError:
    BG_IMAGE_BYTES = b""

BG_IMAGE_CONTENT_TYPE = sniff_image_content_type(BG_IMAGE_BYTES)
# Content hash in the URL so swapping the background file always busts any
# browser cache -- no stale image left behind after an update.
BG_VERSION = hashlib.sha256(BG_IMAGE_BYTES).hexdigest()[:10]


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

    items.sort(key=lambda i: -i["progress"])
    return items


def qbit_login():
    data = urllib.parse.urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode()
    req = urllib.request.Request(f"{QBIT_BASE}/api/v2/auth/login", data=data, method="POST")
    resp = urllib.request.urlopen(req, timeout=8)
    return resp.headers.get("Set-Cookie", "").split(";")[0]


def get_server_health():
    """Mirrors what the recurring chat-triggered health check watches for,
    surfaced here so it doesn't only exist transiently in conversation."""
    health = {}

    try:
        cookie = qbit_login()
        req = urllib.request.Request(f"{QBIT_BASE}/api/v2/torrents/info", headers={"Cookie": cookie})
        with urllib.request.urlopen(req, timeout=8) as r:
            torrents = json.loads(r.read())
        req2 = urllib.request.Request(f"{QBIT_BASE}/api/v2/transfer/info", headers={"Cookie": cookie})
        with urllib.request.urlopen(req2, timeout=8) as r:
            transfer = json.loads(r.read())

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

    try:
        req = urllib.request.Request(f"{SONARR[1]}/api/v3/command", headers={"X-Api-Key": SONARR[2]})
        with urllib.request.urlopen(req, timeout=8) as r:
            commands = json.loads(r.read())

        started = [c for c in commands if c.get("status") == "started"]
        queued = [c for c in commands if c.get("status") == "queued"]

        longest_minutes = 0
        now_utc = datetime.now(timezone.utc)
        for c in started:
            # Sonarr's field is "started", not "startedOn" -- it's usually
            # populated for anything actually running, but queued is always
            # populated as a fallback for the rare case it isn't.
            ts = c.get("started") or c.get("queued")
            if not ts:
                continue
            try:
                started_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_minutes = (now_utc - started_at).total_seconds() / 60
                longest_minutes = max(longest_minutes, age_minutes)
            except ValueError:
                continue

        health["sonarr"] = {
            "reachable": True,
            "started_count": len(started),
            "queued_count": len(queued),
            "longest_running_minutes": round(longest_minutes, 1),
        }
    except Exception:
        health["sonarr"] = {"reachable": False}

    return health


# Sonarr's internal command names -> plain English. Anything not listed falls
# back to its raw name so the modal never shows a blank.
FRIENDLY_COMMANDS = {
    "SeriesSearch": "Searching for a whole series",
    "SeasonSearch": "Searching for a full season",
    "EpisodeSearch": "Searching for specific episodes",
    "MissingEpisodeSearch": "Searching for all missing episodes",
    "RefreshSeries": "Refreshing series info from TheTVDB",
    "RefreshMonitoredDownloads": "Checking download progress",
    "ProcessMonitoredDownloads": "Importing finished downloads",
    "RssSync": "Checking indexers for new releases",
    "Backup": "Backing up the database",
    "RenameSeries": "Renaming files to match the library",
    "RenameFiles": "Renaming files to match the library",
    "RescanSeries": "Rescanning files on disk",
    "DownloadedEpisodesScan": "Scanning the downloads folder",
    "MessagingCleanup": "Routine housekeeping",
    "Housekeeping": "Routine housekeeping",
    "ApplicationUpdate": "Updating Sonarr",
    "CheckHealth": "Running its own health check",
    "ManualImport": "Importing files you picked by hand",
    "ClearBlocklist": "Clearing the blocklist",
    "RefreshMovie": "Refreshing info",
}


def get_command_queue():
    """What Sonarr is actually doing right now and what's waiting, in plain
    language. Powers the click-through on the command-queue card so the queue
    isn't a black box -- you can see exactly what it's chewing through."""
    try:
        req = urllib.request.Request(f"{SONARR[1]}/api/v3/command", headers={"X-Api-Key": SONARR[2]})
        with urllib.request.urlopen(req, timeout=8) as r:
            cmds = json.loads(r.read())
    except Exception:
        return {"reachable": False}

    # seriesId -> title, so a queued search can say WHICH show it's for instead
    # of a bare number. Best-effort; the queue still renders without it.
    series_titles = {}
    try:
        req = urllib.request.Request(f"{SONARR[1]}/api/v3/series", headers={"X-Api-Key": SONARR[2]})
        with urllib.request.urlopen(req, timeout=8) as r:
            series_titles = {s["id"]: s["title"] for s in json.loads(r.read())}
    except Exception:
        pass

    now_utc = datetime.now(timezone.utc)
    running, queued = [], []

    for c in cmds:
        status = c.get("status")
        if status not in ("started", "queued"):
            continue
        name = c.get("name", "")
        body = c.get("body", {}) or {}

        # A specific, human-readable detail line where we can build one.
        detail = (c.get("message") or "").strip()
        if not detail:
            sid = body.get("seriesId")
            if sid in series_titles:
                detail = series_titles[sid]
            elif name in ("EpisodeSearch",) and body.get("episodeIds"):
                n = len(body["episodeIds"])
                detail = f"{n} episode{'s' if n != 1 else ''}"

        entry = {
            "friendly": FRIENDLY_COMMANDS.get(name, name),
            "raw_name": name,
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


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>arr-queue-cleaner dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root
  {
    --ink: #1c234c;
    --glass: rgba(28, 32, 66, 0.52);
    --glass-border: rgba(255, 255, 255, 0.16);
    --text: #eef0ff;
    --dim: #b9bfe6;
    --coral: #f2b6ae;
    --coral-strong: #f5978b;
    --mint: #a6e3c8;
    --star: #fff6df;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body
  {
    margin: 0;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 0 1.25rem 4rem;
    background-image:
      radial-gradient(1.5px 1.5px at 12% 18%, var(--star) 50%, transparent 55%),
      radial-gradient(1.5px 1.5px at 24% 9%, var(--star) 50%, transparent 55%),
      radial-gradient(1px 1px at 33% 26%, var(--star) 50%, transparent 55%),
      radial-gradient(1.5px 1.5px at 6% 32%, var(--star) 50%, transparent 55%),
      radial-gradient(1px 1px at 18% 40%, var(--star) 50%, transparent 55%),
      url("/bg.png");
    background-repeat: no-repeat;
    background-size: auto, auto, auto, auto, auto, cover;
    background-position: center, center, center, center, center, center;
    background-attachment: fixed;
  }
  body::before
  {
    content: "";
    position: fixed;
    inset: 0;
    background: linear-gradient(180deg, rgba(12, 15, 40, 0.35) 0%, rgba(12, 15, 40, 0.15) 40%, rgba(12, 15, 40, 0.45) 100%);
    pointer-events: none;
    z-index: 0;
  }
  .wrap { max-width: 1080px; margin: 0 auto; position: relative; z-index: 1; }

  /* top bar -- deliberately outside .wrap so it can span the full viewport
     width, with the add-menu and live pill pinned to the true screen edges */
  .topbar
  {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 0 -1.25rem;
    padding: 1.5rem 2rem 1.75rem;
  }
  .icon-btn
  {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text);
    cursor: pointer;
  }
  .add-menu { position: relative; }
  .add-dropdown
  {
    display: none;
    position: absolute;
    top: calc(100% + 8px);
    left: 0;
    min-width: 140px;
    background: rgba(20, 24, 58, 0.92);
    border: 1px solid var(--glass-border);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-radius: 12px;
    padding: 0.4rem;
    box-shadow: 0 12px 32px rgba(6, 8, 30, 0.45);
    z-index: 10;
  }
  .add-dropdown.open { display: block; }
  .add-dropdown-item
  {
    padding: 0.55rem 0.75rem;
    border-radius: 8px;
    font-size: 0.88rem;
    color: var(--text);
    cursor: pointer;
  }
  .add-dropdown-item:hover { background: rgba(255, 255, 255, 0.08); }
  .topbar-nav { display: flex; gap: 2rem; font-size: 0.95rem; color: var(--dim); }
  .topbar-nav a { color: inherit; text-decoration: none; }
  .topbar-nav a.active { color: var(--text); font-weight: 600; border-bottom: 2px solid var(--coral); padding-bottom: 0.3rem; }
  .live-pill
  {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.5rem 1rem;
    border-radius: 999px;
    background: rgba(166, 227, 200, 0.14);
    border: 1px solid rgba(166, 227, 200, 0.45);
    color: var(--mint);
    font-size: 0.85rem;
    font-weight: 600;
    transition: background 0.3s ease, border-color 0.3s ease, color 0.3s ease;
  }
  .live-pill.warn { background: rgba(245, 151, 139, 0.14); border-color: rgba(245, 151, 139, 0.45); color: var(--coral-strong); }
  .live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--mint); box-shadow: 0 0 8px var(--mint); transition: background 0.3s ease, box-shadow 0.3s ease; }
  .live-dot.warn { background: var(--coral-strong); box-shadow: 0 0 8px var(--coral-strong); }

  /* hero -- full width, matching the body grid below it */
  .hero { margin-bottom: 2.5rem; }
  .hero-card
  {
    width: 100%;
    position: relative;
    border-radius: 28px;
    overflow: hidden;
    min-height: 300px;
    border: 1px solid var(--glass-border);
    box-shadow: 0 20px 60px rgba(6, 8, 30, 0.5);
  }
  .hero-bg-layer
  {
    position: absolute;
    inset: 0;
    background-size: cover;
    background-position: center 30%;
    opacity: 0;
    transition: opacity 1.6s ease;
    z-index: 0;
  }
  .hero-bg-layer.current { opacity: 1; }
  .hero-scrim
  {
    position: absolute;
    inset: 0;
    background: linear-gradient(120deg, rgba(20, 24, 58, 0.72) 0%, rgba(20, 24, 58, 0.4) 55%, rgba(20, 24, 58, 0.18) 100%);
    z-index: 1;
  }
  .hero-content
  {
    position: relative;
    z-index: 2;
    padding: 2.25rem 2.5rem;
    min-height: 300px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .hero-badge
  {
    display: inline-block;
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    font-weight: 700;
    color: var(--coral);
    background: rgba(242, 182, 174, 0.14);
    border: 1px solid rgba(242, 182, 174, 0.4);
    padding: 0.3rem 0.75rem;
    border-radius: 999px;
    width: fit-content;
    margin-bottom: 1rem;
  }
  .hero-title
  {
    font-size: 2.75rem;
    font-weight: 700;
    color: var(--text);
    text-shadow: 0 4px 24px rgba(0, 0, 0, 0.45);
    line-height: 1.1;
    margin-bottom: 0.6rem;
  }
  .hero-sub { color: var(--dim); font-size: 0.95rem; max-width: 30rem; margin-bottom: 1.25rem; }
  .hero-progress { max-width: 24rem; margin-top: 0.25rem; }
  .hero-progress-pct { color: var(--dim); font-size: 0.85rem; margin-top: 0.4rem; }

  /* live server health -- the same signals the recurring chat health-check
     watches, surfaced persistently instead of only existing in conversation */
  .health-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1.5rem; margin-bottom: 1.5rem; }
  .health-row .info-card { margin-bottom: 0; }
  .health-row .info-card.clickable { cursor: pointer; }
  .health-row .info-card.clickable:hover { background: rgba(28, 32, 66, 0.68); }
  .info-icon.warn { background: rgba(245, 151, 139, 0.18); color: var(--coral-strong); }

  /* three-column body */
  .body-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; align-items: start; }
  .col-title
  {
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 1rem;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.35);
  }
  .col-title .chev { color: var(--dim); font-weight: 400; }

  .thumb-row { display: flex; flex-direction: column; gap: 0.75rem; margin-bottom: 1rem; }
  .thumb-card
  {
    border-radius: 16px;
    padding: 1rem;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
  .thumb-card .thumb-time { font-size: 0.85rem; color: var(--text); font-variant-numeric: tabular-nums; }
  .thumb-card .thumb-tag { font-size: 0.75rem; color: var(--dim); margin-top: 0.2rem; }
  .view-more
  {
    display: block;
    width: 100%;
    padding: 0.75rem;
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid var(--glass-border);
    color: var(--text);
    font-weight: 600;
    font-size: 0.9rem;
    text-align: center;
    text-decoration: none;
    cursor: pointer;
    box-sizing: border-box;
  }
  .view-more:hover { background: rgba(255, 255, 255, 0.14); }

  .dl-carousel
  {
    position: relative;
    min-height: 168px;
    border-radius: 16px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    padding: 1.1rem 1.2rem;
    display: flex;
    flex-direction: column;
    justify-content: center;
    overflow: hidden;
    cursor: pointer;
  }
  .dl-carousel:hover { background: rgba(28, 32, 66, 0.68); }
  .dl-hint { font-size: 0.7rem; color: var(--dim); text-align: center; margin-top: 0.6rem; }

  .modal-backdrop
  {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(8, 10, 28, 0.6);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    z-index: 50;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }
  .modal-backdrop.open { display: flex; }
  .modal-box
  {
    width: 100%;
    max-width: 640px;
    max-height: 80vh;
    background: rgba(20, 24, 58, 0.92);
    border: 1px solid var(--glass-border);
    border-radius: 20px;
    box-shadow: 0 24px 64px rgba(6, 8, 30, 0.6);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .modal-header
  {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid var(--glass-border);
  }
  .modal-title { font-size: 1.05rem; font-weight: 700; color: var(--text); }
  .modal-close
  {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.08);
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 0.95rem;
  }
  .modal-close:hover { background: rgba(255, 255, 255, 0.16); }
  .modal-list { overflow-y: auto; padding: 1rem 1.5rem 1.5rem; }
  .modal-row { padding: 0.9rem 0; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
  .modal-row:last-child { border-bottom: none; }
  .modal-row-top { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; margin-bottom: 0.5rem; }
  .modal-row-title { font-size: 0.9rem; font-weight: 600; color: var(--text); }
  .modal-row-subtitle { font-size: 0.76rem; color: var(--dim); margin-top: 0.1rem; }
  .modal-row-pct { font-size: 0.82rem; font-weight: 700; color: var(--coral); flex: none; }
  .modal-empty { color: var(--dim); text-align: center; padding: 2rem; }
  .cmd-section-label { font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--dim); padding: 1rem 0 0.35rem; }
  .cmd-section-label:first-child { padding-top: 0; }
  .cmd-elapsed { font-variant-numeric: tabular-nums; }
  .dl-card { opacity: 0; transition: opacity 0.5s ease; }
  .dl-card.visible { opacity: 1; }
  .dl-source
  {
    display: inline-block;
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    font-weight: 700;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    margin-bottom: 0.6rem;
    width: fit-content;
  }
  .dl-source.Sonarr { color: var(--mint); background: rgba(166, 227, 200, 0.16); }
  .dl-source.Radarr { color: var(--coral-strong); background: rgba(245, 151, 139, 0.16); }
  .dl-title { font-size: 1rem; font-weight: 700; color: var(--text); margin-bottom: 0.2rem; line-height: 1.3; }
  .dl-subtitle { font-size: 0.8rem; color: var(--dim); margin-bottom: 0.8rem; }
  .dl-progress-track { height: 6px; border-radius: 999px; background: rgba(255, 255, 255, 0.12); overflow: hidden; }
  .dl-progress-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--mint), var(--coral)); transition: width 0.6s ease; }
  .dl-progress-pct { font-size: 0.72rem; color: var(--dim); margin-top: 0.35rem; text-align: right; }
  .dl-dots { display: flex; gap: 0.35rem; justify-content: center; margin-top: 0.9rem; }
  .dl-dot { width: 6px; height: 6px; border-radius: 50%; background: rgba(255, 255, 255, 0.25); transition: background 0.3s ease; }
  .dl-dot.current { background: var(--coral); }

  .info-card
  {
    display: flex;
    gap: 0.9rem;
    align-items: flex-start;
    padding: 1rem 1.1rem;
    border-radius: 16px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    margin-bottom: 0.9rem;
  }
  .info-icon
  {
    flex: none;
    width: 38px;
    height: 38px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .info-icon.mint { background: rgba(166, 227, 200, 0.18); color: var(--mint); }
  .info-icon.coral { background: rgba(245, 151, 139, 0.18); color: var(--coral-strong); }
  .info-icon.star { background: rgba(255, 246, 223, 0.18); color: var(--star); }
  .info-label { font-size: 0.68rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.15rem; }
  .info-title { font-size: 0.92rem; font-weight: 700; color: var(--text); margin-bottom: 0.2rem; }
  .info-desc { font-size: 0.78rem; color: var(--dim); line-height: 1.35; }

  h2
  {
    font-size: 1rem;
    color: var(--text);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin: 0;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.35);
  }
  .section-header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 0.75rem; }
  .section-link { font-size: 0.85rem; color: var(--star); text-decoration: none; font-weight: 600; }
  .section-link:hover { text-decoration: underline; }
  .run
  {
    background: var(--glass);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    margin-bottom: 0.75rem;
    overflow: hidden;
    box-shadow: 0 8px 32px rgba(10, 12, 40, 0.25);
  }
  .run-header
  {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.8rem 1.1rem;
    cursor: pointer;
    user-select: none;
  }
  .run-header:hover { background: rgba(255, 255, 255, 0.06); }
  .run-time { font-variant-numeric: tabular-nums; color: var(--text); font-size: 0.9rem; }
  .run-badge
  {
    font-size: 0.72rem;
    padding: 0.2rem 0.65rem;
    border-radius: 999px;
    border: 1px solid var(--glass-border);
  }
  .badge-clean { color: var(--mint); border-color: rgba(166, 227, 200, 0.5); background: rgba(166, 227, 200, 0.12); }
  .badge-action { color: var(--coral-strong); border-color: rgba(245, 151, 139, 0.5); background: rgba(245, 151, 139, 0.14); }
  .run-body
  {
    display: none;
    padding: 0 1.1rem 1rem;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.82rem;
    white-space: pre-wrap;
    color: var(--dim);
    border-top: 1px solid var(--glass-border);
  }
  .run-body.open { display: block; padding-top: 0.75rem; }
  .run-body .flag-error { color: var(--coral-strong); }
  .run-body .flag-security { color: var(--coral-strong); font-weight: 600; }
  .run-body .flag-action { color: var(--coral); }
  .empty { color: var(--dim); text-align: center; padding: 2rem; }

  @media (max-width: 820px)
  {
    .topbar-nav { display: none; }
    .hero-title { font-size: 2.4rem; }
    .body-grid { grid-template-columns: 1fr; }
    .health-row { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header class="topbar">
  <div class="add-menu">
    <div class="icon-btn" id="addMenuBtn">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
    </div>
    <div class="add-dropdown" id="addDropdown">
      <div class="add-dropdown-item" onclick="window.location.href='/add?type=movie'">Movie</div>
      <div class="add-dropdown-item" onclick="window.location.href='/add?type=show'">Show</div>
    </div>
  </div>
  <nav class="topbar-nav">
    <a href="/" class="active">Overview</a>
    <a href="/calendar">Calendar</a>
    <a href="/library">Library</a>
    <a href="/history">History</a>
  </nav>
  <div class="live-pill" id="livePill"><span class="live-dot" id="liveDot"></span><span id="liveText">Starting&hellip;</span></div>
</header>

<div class="wrap">

  <div class="hero">
    <div class="hero-card">
      <div class="hero-bg-layer current" id="heroBgA" style="background-image: url('/bg.png')"></div>
      <div class="hero-bg-layer" id="heroBgB"></div>
      <div class="hero-scrim"></div>
      <div class="hero-content">
        <div class="hero-badge" id="heroSource">NOW DOWNLOADING</div>
        <div class="hero-title" id="heroDlTitle">Nothing downloading right now</div>
        <div class="hero-sub" id="heroDlSubtitle"></div>
        <div class="dl-progress-track hero-progress" id="heroProgressTrack" style="display: none;"><div class="dl-progress-fill" id="heroProgressFill" style="width: 0%"></div></div>
        <div class="hero-progress-pct" id="heroProgressPct"></div>
      </div>
    </div>
  </div>

  <div class="health-row">
    <div class="info-card">
      <div class="info-icon mint" id="healthQbitIcon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
      </div>
      <div>
        <div class="info-label">qBittorrent speed</div>
        <div class="info-title" id="healthQbitSpeed">Checking&hellip;</div>
        <div class="info-desc" id="healthQbitDesc">qBittorrent</div>
      </div>
    </div>
    <div class="info-card clickable" onclick="openCommandsModal()">
      <div class="info-icon mint" id="healthSonarrIcon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
      </div>
      <div>
        <div class="info-label">Sonarr command queue</div>
        <div class="info-title" id="healthSonarrTitle">Checking&hellip;</div>
        <div class="info-desc" id="healthSonarrDesc">Sonarr queue</div>
      </div>
    </div>
    <div class="info-card clickable" onclick="openHealthCheckModal()">
      <div class="info-icon mint" id="healthCheckIcon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
      </div>
      <div>
        <div class="info-label">Health check</div>
        <div class="info-title" id="healthCheckCountdown">--:--</div>
        <div class="info-desc">Runs every 10 minutes</div>
      </div>
    </div>
    <div class="info-card clickable" onclick="openQueueCleanerModal()">
      <div class="info-icon mint" id="queueCleanerIcon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M21 3v5h-5M3 21v-5h5"/></svg>
      </div>
      <div>
        <div class="info-label">Arr queue cleaner</div>
        <div class="info-title" id="queueCleanerCountdown">--:--</div>
        <div class="info-desc">Runs every 30 minutes</div>
      </div>
    </div>
  </div>

  <div class="body-grid">
    <div class="col">
      <div class="col-title">Recent fixes <span class="chev">&rsaquo;</span></div>
      <div class="thumb-row" id="recentThumbs"><div class="thumb-card"><div class="thumb-time">Loading&hellip;</div></div></div>
      <a class="view-more" href="/history">View full history</a>
    </div>

    <div class="col">
      <div class="col-title">Now downloading</div>
      <div class="dl-carousel" id="dlCarousel" onclick="openDownloadsModal()"><div class="dl-card visible"><div class="dl-title">Loading&hellip;</div></div></div>
      <div class="dl-hint">Click to see everything downloading</div>
    </div>

    <div class="col">
      <div class="col-title">At a glance</div>
      <div class="info-card">
        <div class="info-icon mint">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
        </div>
        <div>
          <div class="info-title" id="statTotal">-</div>
          <div class="info-desc">Runs logged in the visible history window</div>
        </div>
      </div>
      <div class="info-card">
        <div class="info-icon coral">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a4 4 0 0 1-5.4 5.4L4 17l1 2 2 1 5.3-5.3a4 4 0 0 1 5.4-5.4L14.7 6.3z"/></svg>
        </div>
        <div>
          <div class="info-title" id="statActions">-</div>
          <div class="info-desc">Runs where something actually got fixed</div>
        </div>
      </div>
      <div class="info-card">
        <div class="info-icon star">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/></svg>
        </div>
        <div>
          <div class="info-title" id="statLast">-</div>
          <div class="info-desc">Timestamp of the most recent run</div>
        </div>
      </div>
    </div>
  </div>

  <div class="section-header">
    <h2>Recent runs</h2>
    <a class="section-link" href="/history">Full history &rarr;</a>
  </div>
  <div id="runs"><div class="empty">Loading...</div></div>
</div>

<div class="modal-backdrop" id="downloadsModal" onclick="closeDownloadsModal(event)">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div class="modal-title">Now downloading</div>
      <div class="modal-close" onclick="closeDownloadsModal()">&times;</div>
    </div>
    <div class="modal-list" id="modalList"></div>
  </div>
</div>

<div class="modal-backdrop" id="healthCheckModal" onclick="closeHealthCheckModal(event)">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div class="modal-title">Health check</div>
      <div class="modal-close" onclick="closeHealthCheckModal()">&times;</div>
    </div>
    <div class="modal-list">
      <div class="modal-row">
        <div class="modal-row-title">Stalled downloads</div>
        <div class="modal-row-subtitle">Watches qBittorrent for torrents stuck with zero seeds for 20 minutes or more. When it finds one, it blocklists that release in Sonarr and starts a fresh search for a replacement automatically.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Transfer speed</div>
        <div class="modal-row-subtitle">Watches overall qBittorrent transfer speed. If it stays near zero for 20 minutes while downloads are supposedly active, it flags a possible bottleneck.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Sonarr command queue</div>
        <div class="modal-row-subtitle">Watches Sonarr's own search and scan commands. If one reports the same status for 5 minutes it flags it as frozen, and if it stays wedged past 30 minutes it restarts Sonarr automatically to clear the jam (with a cooldown so it can't loop).</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">VPN watchdog</div>
        <div class="modal-row-subtitle">qBittorrent shares the VPN container's network. If the VPN restarts on its own, qBittorrent is restarted automatically so downloads keep working without anyone stepping in.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Runs every 10 minutes</div>
        <div class="modal-row-subtitle">It only logs something when it actually finds a problem, so a quiet history here means everything checked out fine.</div>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="queueCleanerModal" onclick="closeQueueCleanerModal(event)">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div class="modal-title">Arr queue cleaner</div>
      <div class="modal-close" onclick="closeQueueCleanerModal()">&times;</div>
    </div>
    <div class="modal-list">
      <div class="modal-row">
        <div class="modal-row-title">Not an upgrade limbo</div>
        <div class="modal-row-subtitle">Removes downloads stuck waiting to import because they would be a downgrade from what's already there, blocklists them, and never grabs that exact release again.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Dead torrents</div>
        <div class="modal-row-subtitle">Watches for torrents making zero progress across two 30 minute checks in a row. When it finds one, it blocklists the release in Sonarr or Radarr and starts a fresh search for a replacement.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Disguised executables</div>
        <div class="modal-row-subtitle">Deletes any download that turns out to be a bare .exe or similar file instead of a real video, then blocklists it. It only checks the file type, it never opens or runs anything.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Stuck imports</div>
        <div class="modal-row-subtitle">Rescues completed downloads that Sonarr or Radarr never imported, but only when it is highly confident about the match. Anything uncertain is left for manual review instead of guessed at.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Redundant downloads</div>
        <div class="modal-row-subtitle">Stops torrents still running for a series or movie that Sonarr or Radarr already has every file for, freeing up the download slot they were sitting on.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Disk space</div>
        <div class="modal-row-subtitle">Checks free space on qBittorrent's disk every run and logs a warning if it is getting low, since a full disk is one of the quietest ways downloads can silently stop working.</div>
      </div>
      <div class="modal-row">
        <div class="modal-row-title">Runs every 30 minutes</div>
        <div class="modal-row-subtitle">It only logs something when it actually finds and fixes a problem, so a quiet history here means everything checked out fine.</div>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="commandsModal" onclick="closeCommandsModal(event)">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div class="modal-title">Sonarr command queue &middot; <span id="commandsLive" style="font-size:0.72rem;font-weight:600;color:var(--mint)">live</span></div>
      <div class="modal-close" onclick="closeCommandsModal()">&times;</div>
    </div>
    <div class="modal-list" id="commandsModalList"><div class="modal-empty">Loading&hellip;</div></div>
  </div>
</div>

<script>
let nextRunTime = null;
let nextHealthCheckRunTime = null;

function pad(n)
{
  return String(n).padStart(2, "0");
}

function countdownText(target)
{
  const diffMs = target - new Date();
  if (diffMs <= 0)
  {
    return "due now";
  }
  const totalSeconds = Math.floor(diffMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return pad(minutes) + ":" + pad(seconds);
}

function tick()
{
  if (nextRunTime)
  {
    document.getElementById("queueCleanerCountdown").textContent = countdownText(nextRunTime);
  }
  if (nextHealthCheckRunTime)
  {
    document.getElementById("healthCheckCountdown").textContent = countdownText(nextHealthCheckRunTime);
  }
}

function fmtTime(iso)
{
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function loadStatus()
{
  const res = await fetch("/api/status");
  const data = await res.json();
  nextRunTime = new Date(data.next_run);
  nextHealthCheckRunTime = new Date(data.next_health_check_run);
}

function hadAction(lines)
{
  const markers = ["DEAD:", "SECURITY", "removed", "imported", "NEEDS MANUAL REVIEW", "ERROR"];
  const text = lines.join("\\n");
  return markers.some(function (m) { return text.indexOf(m) !== -1; });
}

function highlight(line)
{
  const esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  if (esc.indexOf("SECURITY") !== -1)
  {
    return '<span class="flag-security">' + esc + '</span>';
  }
  if (esc.indexOf("ERROR") !== -1)
  {
    return '<span class="flag-error">' + esc + '</span>';
  }
  if (esc.indexOf("DEAD:") !== -1 || esc.indexOf("removed") !== -1 || esc.indexOf("imported") !== -1 || esc.indexOf("NEEDS MANUAL REVIEW") !== -1)
  {
    return '<span class="flag-action">' + esc + '</span>';
  }
  return esc;
}

const MAIN_PAGE_RUN_LIMIT = 3;

function expandRun(idx)
{
  if (idx >= MAIN_PAGE_RUN_LIMIT)
  {
    window.location.href = "/history";
    return;
  }
  const el = document.getElementById("run-" + idx);
  if (!el)
  {
    return;
  }
  el.classList.add("open");
  el.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function loadRuns()
{
  const res = await fetch("/api/log");
  const data = await res.json();
  const runs = data.runs || [];

  const container = document.getElementById("runs");
  if (runs.length === 0)
  {
    container.innerHTML = '<div class="empty">No runs logged yet.</div>';
    document.getElementById("recentThumbs").innerHTML = '<div class="thumb-card"><div class="thumb-time">Nothing yet</div></div>';
    return;
  }

  let actionCount = 0;
  const flags = runs.map(function (run) { return hadAction(run.lines); });
  actionCount = flags.filter(Boolean).length;

  const html = runs.slice(0, MAIN_PAGE_RUN_LIMIT).map(function (run, idx)
  {
    const action = flags[idx];
    const badgeClass = action ? "badge-action" : "badge-clean";
    const badgeText = action ? "action taken" : "clean";
    const bodyLines = run.lines.map(highlight).join("\\n");
    return (
      '<div class="run">' +
        '<div class="run-header" onclick="toggleRun(' + idx + ')">' +
          '<span class="run-time">' + run.timestamp + '</span>' +
          '<span class="run-badge ' + badgeClass + '">' + badgeText + '</span>' +
        '</div>' +
        '<div class="run-body" id="run-' + idx + '">' + bodyLines + '</div>' +
      '</div>'
    );
  }).join("");
  container.innerHTML = html;

  document.getElementById("statTotal").textContent = runs.length;
  document.getElementById("statActions").textContent = actionCount;
  document.getElementById("statLast").textContent = runs[0] ? fmtTime(runs[0].timestamp.replace(" ", "T")) : "-";

  // recent fixes thumbnails -- the last 2 runs that actually did something,
  // falling back to the 2 most recent runs if nothing needed fixing lately
  let recent = runs.filter(function (_, idx) { return flags[idx]; }).slice(0, 2);
  if (recent.length === 0)
  {
    recent = runs.slice(0, 2);
  }
  document.getElementById("recentThumbs").innerHTML = recent.map(function (run)
  {
    const idx = runs.indexOf(run);
    const action = flags[idx];
    return (
      '<div class="thumb-card" onclick="expandRun(' + idx + ')" style="cursor:pointer">' +
        '<div class="thumb-time">' + run.timestamp + '</div>' +
        '<div class="thumb-tag">' + (action ? "Action taken" : "Clean pass") + '</div>' +
      '</div>'
    );
  }).join("");
}

function toggleRun(idx)
{
  const el = document.getElementById("run-" + idx);
  el.classList.toggle("open");
}

document.getElementById("addMenuBtn").addEventListener("click", function (e)
{
  e.stopPropagation();
  document.getElementById("addDropdown").classList.toggle("open");
});

document.addEventListener("click", function ()
{
  document.getElementById("addDropdown").classList.remove("open");
});

function escapeHtml(s)
{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

let downloadItems = [];
let dlIndex = 0;

function renderHero()
{
  const track = document.getElementById("heroProgressTrack");
  const fill = document.getElementById("heroProgressFill");

  if (downloadItems.length === 0)
  {
    document.getElementById("heroSource").textContent = "NOW DOWNLOADING";
    document.getElementById("heroDlTitle").textContent = "Nothing downloading right now";
    document.getElementById("heroDlSubtitle").textContent = "";
    document.getElementById("heroProgressPct").textContent = "";
    track.style.display = "none";
    return;
  }

  const item = downloadItems[dlIndex % downloadItems.length];
  document.getElementById("heroSource").textContent = item.source.toUpperCase();
  document.getElementById("heroDlTitle").textContent = item.title;
  document.getElementById("heroDlSubtitle").textContent = item.subtitle;
  document.getElementById("heroProgressPct").textContent = item.progress + "% complete";
  fill.style.width = item.progress + "%";
  track.style.display = "block";
}

function renderDownloadCard()
{
  const container = document.getElementById("dlCarousel");
  renderHero();
  if (downloadItems.length === 0)
  {
    container.innerHTML = '<div class="dl-card visible"><div class="dl-title">Nothing downloading right now</div></div>';
    return;
  }

  const item = downloadItems[dlIndex % downloadItems.length];
  const dots = downloadItems.map(function (_, i)
  {
    return '<span class="dl-dot' + (i === (dlIndex % downloadItems.length) ? " current" : "") + '"></span>';
  }).join("");

  container.innerHTML =
    '<div class="dl-card" id="dlCard">' +
      '<span class="dl-source ' + item.source + '">' + item.source.toUpperCase() + '</span>' +
      '<div class="dl-title">' + escapeHtml(item.title) + '</div>' +
      '<div class="dl-subtitle">' + escapeHtml(item.subtitle) + '</div>' +
      '<div class="dl-progress-track"><div class="dl-progress-fill" style="width:' + item.progress + '%"></div></div>' +
      '<div class="dl-progress-pct">' + item.progress + '% complete</div>' +
      '<div class="dl-dots">' + dots + '</div>' +
    '</div>';

  requestAnimationFrame(function ()
  {
    const card = document.getElementById("dlCard");
    if (card)
    {
      card.classList.add("visible");
    }
  });
}

async function loadDownloads()
{
  const res = await fetch("/api/downloads");
  const data = await res.json();
  downloadItems = data.items || [];
  if (dlIndex >= downloadItems.length)
  {
    dlIndex = 0;
  }
  renderDownloadCard();
  renderModalList();
}

function renderModalList()
{
  const list = document.getElementById("modalList");
  if (downloadItems.length === 0)
  {
    list.innerHTML = '<div class="modal-empty">Nothing downloading right now</div>';
    return;
  }

  list.innerHTML = downloadItems.map(function (item)
  {
    return (
      '<div class="modal-row">' +
        '<div class="modal-row-top">' +
          '<div>' +
            '<span class="dl-source ' + item.source + '">' + item.source.toUpperCase() + '</span>' +
            '<div class="modal-row-title">' + escapeHtml(item.title) + '</div>' +
            '<div class="modal-row-subtitle">' + escapeHtml(item.subtitle) + '</div>' +
          '</div>' +
          '<div class="modal-row-pct">' + item.progress + '%</div>' +
        '</div>' +
        '<div class="dl-progress-track"><div class="dl-progress-fill" style="width:' + item.progress + '%"></div></div>' +
      '</div>'
    );
  }).join("");
}

function openDownloadsModal()
{
  renderModalList();
  document.getElementById("downloadsModal").classList.add("open");
}

function closeDownloadsModal()
{
  document.getElementById("downloadsModal").classList.remove("open");
}

function openHealthCheckModal()
{
  document.getElementById("healthCheckModal").classList.add("open");
}

function closeHealthCheckModal()
{
  document.getElementById("healthCheckModal").classList.remove("open");
}

function openQueueCleanerModal()
{
  document.getElementById("queueCleanerModal").classList.add("open");
}

function closeQueueCleanerModal()
{
  document.getElementById("queueCleanerModal").classList.remove("open");
}

// --- Sonarr command queue modal (live) ---
let commandsData = null;
let commandsFetchedAt = 0;   // client clock when the data was fetched, for smooth count-up
let commandsPollTimer = null;
let commandsTickTimer = null;

function fmtDuration(secs)
{
  secs = Math.max(0, Math.floor(secs));
  if (secs < 60) { return secs + "s"; }
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) { return m + "m " + pad(s) + "s"; }
  const h = Math.floor(m / 60);
  return h + "h " + pad(m % 60) + "m";
}

async function loadCommands()
{
  try
  {
    const res = await fetch("/api/commands");
    commandsData = await res.json();
    commandsFetchedAt = Date.now();
  }
  catch (e)
  {
    commandsData = { reachable: false };
  }
  renderCommandsModal();
}

function renderCommandsModal()
{
  const list = document.getElementById("commandsModalList");
  if (!commandsData || !commandsData.reachable)
  {
    document.getElementById("commandsLive").textContent = "offline";
    list.innerHTML = '<div class="modal-empty">Couldn&rsquo;t reach Sonarr</div>';
    return;
  }
  document.getElementById("commandsLive").textContent = "live";

  const running = commandsData.running || [];
  const queued = commandsData.queued || [];
  const elapsedBonus = (Date.now() - commandsFetchedAt) / 1000;  // seconds since fetch, for live count-up

  let html = "";

  html += '<div class="cmd-section-label">Running now (' + running.length + ')</div>';
  if (running.length === 0)
  {
    html += '<div class="modal-empty" style="padding:1rem">Nothing running &mdash; Sonarr is idle</div>';
  }
  running.forEach(function (c)
  {
    const secs = (c.running_seconds || 0) + elapsedBonus;
    html +=
      '<div class="modal-row">' +
        '<div class="modal-row-top">' +
          '<div>' +
            '<div class="modal-row-title">' + escapeHtml(c.friendly) + '</div>' +
            (c.detail ? '<div class="modal-row-subtitle">' + escapeHtml(c.detail) + '</div>' : '') +
          '</div>' +
          '<div class="modal-row-pct cmd-elapsed">' + fmtDuration(secs) + '</div>' +
        '</div>' +
      '</div>';
  });

  html += '<div class="cmd-section-label">Waiting in line (' + queued.length + ')</div>';
  if (queued.length === 0)
  {
    html += '<div class="modal-empty" style="padding:1rem">Nothing waiting</div>';
  }
  queued.forEach(function (c, idx)
  {
    html +=
      '<div class="modal-row">' +
        '<div class="modal-row-top">' +
          '<div>' +
            '<div class="modal-row-title">' + escapeHtml(c.friendly) + '</div>' +
            (c.detail ? '<div class="modal-row-subtitle">' + escapeHtml(c.detail) + '</div>' : '') +
          '</div>' +
          '<div class="modal-row-pct" style="color:var(--dim)">#' + (idx + 1) + '</div>' +
        '</div>' +
      '</div>';
  });

  list.innerHTML = html;
}

function tickCommands()
{
  // recompute the running-command elapsed labels every second without refetching
  if (!commandsData || !commandsData.reachable) { return; }
  const running = commandsData.running || [];
  const nodes = document.querySelectorAll("#commandsModalList .cmd-elapsed");
  const elapsedBonus = (Date.now() - commandsFetchedAt) / 1000;
  running.forEach(function (c, i)
  {
    if (nodes[i]) { nodes[i].textContent = fmtDuration((c.running_seconds || 0) + elapsedBonus); }
  });
}

function openCommandsModal()
{
  document.getElementById("commandsModal").classList.add("open");
  loadCommands();
  clearInterval(commandsPollTimer);
  clearInterval(commandsTickTimer);
  commandsPollTimer = setInterval(loadCommands, 3000);  // refetch every 3s
  commandsTickTimer = setInterval(tickCommands, 1000);  // smooth count-up
}

function closeCommandsModal()
{
  document.getElementById("commandsModal").classList.remove("open");
  clearInterval(commandsPollTimer);
  clearInterval(commandsTickTimer);
  commandsPollTimer = null;
  commandsTickTimer = null;
}

function advanceCarousel()
{
  if (downloadItems.length === 0)
  {
    return;
  }
  dlIndex = (dlIndex + 1) % downloadItems.length;
  renderDownloadCard();
}

// hero splash-screen: crossfades the hero banner's background through
// poster/fanart art for whatever's currently downloading, falling back to
// the static night-sky image when nothing has art or nothing is downloading
let heroImages = ["/bg.png"];
let heroIndex = 0;
let heroShowingA = true;

function buildHeroImages()
{
  const fromDownloads = downloadItems.map(function (i) { return i.image; }).filter(Boolean);
  heroImages = fromDownloads.length > 0 ? fromDownloads : ["/bg.png"];
  if (heroIndex >= heroImages.length)
  {
    heroIndex = 0;
  }
}

function advanceHero()
{
  if (heroImages.length <= 1)
  {
    return;
  }
  heroIndex = (heroIndex + 1) % heroImages.length;
  const nextUrl = heroImages[heroIndex];
  const incoming = document.getElementById(heroShowingA ? "heroBgB" : "heroBgA");
  const outgoing = document.getElementById(heroShowingA ? "heroBgA" : "heroBgB");
  incoming.style.backgroundImage = "url('" + nextUrl + "')";
  incoming.classList.add("current");
  outgoing.classList.remove("current");
  heroShowingA = !heroShowingA;
}

function formatSpeed(bytesPerSec)
{
  return (bytesPerSec / 1024 / 1024).toFixed(1) + " MB/s";
}

async function loadHealth()
{
  let data;
  try
  {
    const res = await fetch("/api/health");
    data = await res.json();
  }
  catch (e)
  {
    data = {};
  }

  const qbit = data.qbittorrent;
  const qbitIcon = document.getElementById("healthQbitIcon");
  if (!qbit || !qbit.reachable)
  {
    document.getElementById("healthQbitSpeed").textContent = "Unreachable";
    document.getElementById("healthQbitDesc").textContent = "qBittorrent did not respond";
    qbitIcon.className = "info-icon warn";
  }
  else if (qbit.stalled_count > 0)
  {
    document.getElementById("healthQbitSpeed").textContent = formatSpeed(qbit.dl_speed);
    document.getElementById("healthQbitDesc").textContent = qbit.stalled_count + " torrent" + (qbit.stalled_count === 1 ? "" : "s") + " stalled 20+ min";
    qbitIcon.className = "info-icon warn";
  }
  else
  {
    document.getElementById("healthQbitSpeed").textContent = formatSpeed(qbit.dl_speed);
    document.getElementById("healthQbitDesc").textContent = "Downloading, nothing stalled";
    qbitIcon.className = "info-icon mint";
  }

  const sonarr = data.sonarr;
  const sonarrIcon = document.getElementById("healthSonarrIcon");
  if (!sonarr || !sonarr.reachable)
  {
    document.getElementById("healthSonarrTitle").textContent = "Unreachable";
    document.getElementById("healthSonarrDesc").textContent = "Sonarr did not respond";
    sonarrIcon.className = "info-icon warn";
  }
  else if (sonarr.longest_running_minutes > 15)
  {
    document.getElementById("healthSonarrTitle").textContent = sonarr.started_count + " running";
    document.getElementById("healthSonarrDesc").textContent = sonarr.queued_count + " queued, longest running " + Math.round(sonarr.longest_running_minutes) + " min";
    sonarrIcon.className = "info-icon warn";
  }
  else
  {
    document.getElementById("healthSonarrTitle").textContent = sonarr.started_count + " running";
    document.getElementById("healthSonarrDesc").textContent = sonarr.queued_count + " queued";
    sonarrIcon.className = "info-icon mint";
  }
}

let lastSuccessTime = null;
let liveHasErrored = false;

function updateLivePill()
{
  const pill = document.getElementById("livePill");
  const dot = document.getElementById("liveDot");
  const text = document.getElementById("liveText");

  if (!lastSuccessTime)
  {
    text.textContent = "Starting…";
    return;
  }

  const seconds = Math.floor((new Date() - lastSuccessTime) / 1000);
  let label;
  if (seconds < 5)
  {
    label = "Updated just now";
  }
  else if (seconds < 60)
  {
    label = "Updated " + seconds + "s ago";
  }
  else
  {
    label = "Updated " + Math.floor(seconds / 60) + "m ago";
  }

  // stale past 3 missed 30s refresh cycles, or the last attempt itself failed
  const stale = liveHasErrored || seconds > 90;
  pill.classList.toggle("warn", stale);
  dot.classList.toggle("warn", stale);
  text.textContent = stale && liveHasErrored ? "Connection issue" : label;
}

async function refreshAll()
{
  try
  {
    await loadHealth();
    await loadStatus();
    await loadRuns();
    await loadDownloads();
    buildHeroImages();
    lastSuccessTime = new Date();
    liveHasErrored = false;
  }
  catch (e)
  {
    liveHasErrored = true;
  }
  updateLivePill();
}

refreshAll();
setInterval(tick, 1000);
setInterval(updateLivePill, 1000);
setInterval(refreshAll, 30000);
setInterval(advanceCarousel, 4000);
setInterval(advanceHero, 6000);
</script>
</body>
</html>
"""

PAGE = PAGE.replace("/bg.png", f"/bg.png?v={BG_VERSION}")

PAGE_HISTORY = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>arr-queue-cleaner &mdash; full history</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root
  {
    --ink: #1c234c;
    --glass: rgba(28, 32, 66, 0.52);
    --glass-border: rgba(255, 255, 255, 0.16);
    --text: #eef0ff;
    --dim: #b9bfe6;
    --coral: #f2b6ae;
    --coral-strong: #f5978b;
    --mint: #a6e3c8;
    --star: #fff6df;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body
  {
    margin: 0;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 0 1.25rem 4rem;
    background-image:
      radial-gradient(1.5px 1.5px at 12% 18%, var(--star) 50%, transparent 55%),
      radial-gradient(1.5px 1.5px at 24% 9%, var(--star) 50%, transparent 55%),
      radial-gradient(1px 1px at 33% 26%, var(--star) 50%, transparent 55%),
      url("/bg.png");
    background-repeat: no-repeat;
    background-size: auto, auto, auto, cover;
    background-position: center;
    background-attachment: fixed;
  }
  body::before
  {
    content: "";
    position: fixed;
    inset: 0;
    background: linear-gradient(180deg, rgba(12, 15, 40, 0.35) 0%, rgba(12, 15, 40, 0.15) 40%, rgba(12, 15, 40, 0.45) 100%);
    pointer-events: none;
    z-index: 0;
  }
  .wrap { max-width: 900px; margin: 0 auto; position: relative; z-index: 1; padding-top: 2.5rem; }
  .topbar
  {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 0 -1.25rem 2rem;
    padding: 1.5rem 2rem 1.75rem;
  }
  .topbar-nav { display: flex; gap: 2rem; font-size: 0.95rem; color: var(--dim); }
  .topbar-nav a { color: inherit; text-decoration: none; }
  .topbar-nav a.active { color: var(--text); font-weight: 600; border-bottom: 2px solid var(--coral); padding-bottom: 0.3rem; }
  .back-link { color: var(--star); font-size: 0.9rem; font-weight: 600; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }
  h1
  {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 1.5rem;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.35);
  }
  .run
  {
    background: var(--glass);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    margin-bottom: 0.75rem;
    overflow: hidden;
    box-shadow: 0 8px 32px rgba(10, 12, 40, 0.25);
  }
  .run-header
  {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.8rem 1.1rem;
    cursor: pointer;
    user-select: none;
  }
  .run-header:hover { background: rgba(255, 255, 255, 0.06); }
  .run-time { font-variant-numeric: tabular-nums; color: var(--text); font-size: 0.9rem; }
  .run-badge
  {
    font-size: 0.72rem;
    padding: 0.2rem 0.65rem;
    border-radius: 999px;
    border: 1px solid var(--glass-border);
  }
  .badge-clean { color: var(--mint); border-color: rgba(166, 227, 200, 0.5); background: rgba(166, 227, 200, 0.12); }
  .badge-action { color: var(--coral-strong); border-color: rgba(245, 151, 139, 0.5); background: rgba(245, 151, 139, 0.14); }
  .run-body
  {
    display: none;
    padding: 0 1.1rem 1rem;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.82rem;
    white-space: pre-wrap;
    color: var(--dim);
    border-top: 1px solid var(--glass-border);
  }
  .run-body.open { display: block; padding-top: 0.75rem; }
  .run-body .flag-error { color: var(--coral-strong); }
  .run-body .flag-security { color: var(--coral-strong); font-weight: 600; }
  .run-body .flag-action { color: var(--coral); }
  .empty { color: var(--dim); text-align: center; padding: 2rem; }

  @media (max-width: 820px)
  {
    .topbar-nav { display: none; }
  }
</style>
</head>
<body>

<header class="topbar">
  <a class="back-link" href="/">&larr; Overview</a>
  <nav class="topbar-nav">
    <a href="/">Overview</a>
    <a href="/calendar">Calendar</a>
    <a href="/library">Library</a>
    <a href="/history" class="active">History</a>
  </nav>
  <div style="width: 90px"></div>
</header>

<div class="wrap">
  <h1>Full run history</h1>
  <div id="runs"><div class="empty">Loading&hellip;</div></div>
</div>

<script>
function hadAction(lines)
{
  const markers = ["DEAD:", "SECURITY", "removed", "imported", "NEEDS MANUAL REVIEW", "ERROR"];
  const text = lines.join("\\n");
  return markers.some(function (m) { return text.indexOf(m) !== -1; });
}

function highlight(line)
{
  const esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  if (esc.indexOf("SECURITY") !== -1)
  {
    return '<span class="flag-security">' + esc + '</span>';
  }
  if (esc.indexOf("ERROR") !== -1)
  {
    return '<span class="flag-error">' + esc + '</span>';
  }
  if (esc.indexOf("DEAD:") !== -1 || esc.indexOf("removed") !== -1 || esc.indexOf("imported") !== -1 || esc.indexOf("NEEDS MANUAL REVIEW") !== -1)
  {
    return '<span class="flag-action">' + esc + '</span>';
  }
  return esc;
}

function toggleRun(idx)
{
  document.getElementById("run-" + idx).classList.toggle("open");
}

async function loadRuns()
{
  const res = await fetch("/api/log");
  const data = await res.json();
  const runs = data.runs || [];
  const container = document.getElementById("runs");

  if (runs.length === 0)
  {
    container.innerHTML = '<div class="empty">No runs logged yet.</div>';
    return;
  }

  container.innerHTML = runs.map(function (run, idx)
  {
    const action = hadAction(run.lines);
    const badgeClass = action ? "badge-action" : "badge-clean";
    const badgeText = action ? "action taken" : "clean";
    const bodyLines = run.lines.map(highlight).join("\\n");
    return (
      '<div class="run">' +
        '<div class="run-header" onclick="toggleRun(' + idx + ')">' +
          '<span class="run-time">' + run.timestamp + '</span>' +
          '<span class="run-badge ' + badgeClass + '">' + badgeText + '</span>' +
        '</div>' +
        '<div class="run-body" id="run-' + idx + '">' + bodyLines + '</div>' +
      '</div>'
    );
  }).join("");
}

loadRuns();
setInterval(loadRuns, 30000);
</script>
</body>
</html>
"""

PAGE_HISTORY = PAGE_HISTORY.replace("/bg.png", f"/bg.png?v={BG_VERSION}")

PAGE_CALENDAR = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>arr-queue-cleaner &mdash; calendar</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root
  {
    --ink: #1c234c;
    --glass: rgba(28, 32, 66, 0.52);
    --glass-border: rgba(255, 255, 255, 0.16);
    --text: #eef0ff;
    --dim: #b9bfe6;
    --coral: #f2b6ae;
    --coral-strong: #f5978b;
    --mint: #a6e3c8;
    --star: #fff6df;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body
  {
    margin: 0;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 0 1.25rem 4rem;
    background-image:
      radial-gradient(1.5px 1.5px at 12% 18%, var(--star) 50%, transparent 55%),
      radial-gradient(1.5px 1.5px at 24% 9%, var(--star) 50%, transparent 55%),
      radial-gradient(1px 1px at 33% 26%, var(--star) 50%, transparent 55%),
      url("/bg.png");
    background-repeat: no-repeat;
    background-size: auto, auto, auto, cover;
    background-position: center;
    background-attachment: fixed;
  }
  body::before
  {
    content: "";
    position: fixed;
    inset: 0;
    background: linear-gradient(180deg, rgba(12, 15, 40, 0.35) 0%, rgba(12, 15, 40, 0.15) 40%, rgba(12, 15, 40, 0.45) 100%);
    pointer-events: none;
    z-index: 0;
  }
  .wrap { max-width: 1080px; margin: 0 auto; position: relative; z-index: 1; padding-top: 2.5rem; }
  .topbar
  {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 0 -1.25rem 2rem;
    padding: 1.5rem 2rem 1.75rem;
  }
  .topbar-nav { display: flex; gap: 2rem; font-size: 0.95rem; color: var(--dim); }
  .topbar-nav a { color: inherit; text-decoration: none; }
  .topbar-nav a.active { color: var(--text); font-weight: 600; border-bottom: 2px solid var(--coral); padding-bottom: 0.3rem; }
  .back-link { color: var(--star); font-size: 0.9rem; font-weight: 600; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }

  .cal-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.25rem; flex-wrap: wrap; gap: 1rem; }
  .cal-month-label { font-size: 1.4rem; font-weight: 700; color: var(--text); text-shadow: 0 1px 8px rgba(0, 0, 0, 0.35); }
  .cal-nav { display: flex; align-items: center; gap: 0.6rem; }
  .cal-nav-btn
  {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 1rem;
  }
  .cal-nav-btn:hover { background: rgba(255, 255, 255, 0.1); }
  .cal-today-btn
  {
    padding: 0.5rem 1.1rem;
    border-radius: 999px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    color: var(--text);
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
  }
  .cal-today-btn:hover { background: rgba(255, 255, 255, 0.1); }

  .cal-weekdays { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; margin-bottom: 6px; }
  .cal-weekday { text-align: center; font-size: 0.72rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.06em; padding-bottom: 0.3rem; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
  .cal-cell
  {
    min-height: 108px;
    background: var(--glass);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border: 1px solid var(--glass-border);
    border-radius: 10px;
    padding: 0.45rem;
    overflow-y: auto;
  }
  .cal-cell.dim { opacity: 0.35; }
  .cal-cell.today { border-color: var(--coral); }
  .cal-daynum { font-size: 0.78rem; color: var(--dim); margin-bottom: 0.3rem; }
  .cal-cell.today .cal-daynum { color: var(--coral); font-weight: 700; }
  .cal-event { border-left: 3px solid; padding: 0.2rem 0.4rem; margin-bottom: 0.25rem; background: rgba(255, 255, 255, 0.05); border-radius: 4px; }
  .cal-event.has-file { opacity: 0.55; }
  .cal-event-title { font-size: 0.72rem; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cal-event-sub { font-size: 0.64rem; color: var(--dim); }
  .cal-empty { grid-column: 1 / -1; text-align: center; color: var(--dim); padding: 2rem; }

  @media (max-width: 820px)
  {
    .topbar-nav { display: none; }
    .cal-cell { min-height: 70px; font-size: 0.8rem; }
    .cal-event-title { font-size: 0.65rem; }
  }
</style>
</head>
<body>

<header class="topbar">
  <a class="back-link" href="/">&larr; Overview</a>
  <nav class="topbar-nav">
    <a href="/">Overview</a>
    <a href="/calendar" class="active">Calendar</a>
    <a href="/history">History</a>
  </nav>
  <div style="width: 90px"></div>
</header>

<div class="wrap">
  <div class="cal-header">
    <div class="cal-month-label" id="calMonthLabel">&nbsp;</div>
    <div class="cal-nav">
      <div class="cal-nav-btn" onclick="shiftMonth(-1)">&#8249;</div>
      <div class="cal-today-btn" onclick="goToday()">Today</div>
      <div class="cal-nav-btn" onclick="shiftMonth(1)">&#8250;</div>
    </div>
  </div>

  <div class="cal-weekdays">
    <div class="cal-weekday">Sun</div>
    <div class="cal-weekday">Mon</div>
    <div class="cal-weekday">Tue</div>
    <div class="cal-weekday">Wed</div>
    <div class="cal-weekday">Thu</div>
    <div class="cal-weekday">Fri</div>
    <div class="cal-weekday">Sat</div>
  </div>
  <div class="cal-grid" id="calGrid"></div>
</div>

<script>
let calYear;
let calMonth;

function pad2(n)
{
  return String(n).padStart(2, "0");
}

function isoDate(d)
{
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

function escapeHtml(s)
{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function colorForTitle(title)
{
  let hash = 0;
  for (let i = 0; i < title.length; i++)
  {
    hash = title.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return "hsl(" + hue + ", 65%, 62%)";
}

async function loadCalendar()
{
  const firstOfMonth = new Date(calYear, calMonth, 1);
  const lastOfMonth = new Date(calYear, calMonth + 1, 0);

  const gridStart = new Date(firstOfMonth);
  gridStart.setDate(gridStart.getDate() - gridStart.getDay());
  const gridEnd = new Date(lastOfMonth);
  gridEnd.setDate(gridEnd.getDate() + (6 - gridEnd.getDay()));

  document.getElementById("calMonthLabel").textContent =
    firstOfMonth.toLocaleDateString([], { month: "long", year: "numeric" });

  let events = [];
  try
  {
    const res = await fetch("/api/calendar?start=" + isoDate(gridStart) + "&end=" + isoDate(gridEnd));
    const data = await res.json();
    events = data.events || [];
  }
  catch (e)
  {
    events = [];
  }

  renderCalendar(gridStart, gridEnd, events);
}

function renderCalendar(gridStart, gridEnd, events)
{
  const byDate = {};
  events.forEach(function (e)
  {
    (byDate[e.date] = byDate[e.date] || []).push(e);
  });

  const todayStr = isoDate(new Date());
  const cells = [];
  const cursor = new Date(gridStart);

  while (cursor <= gridEnd)
  {
    const dateStr = isoDate(cursor);
    const dayEvents = (byDate[dateStr] || []).slice().sort(function (a, b)
    {
      return (a.time || "").localeCompare(b.time || "");
    });
    const inMonth = cursor.getMonth() === calMonth;
    const isToday = dateStr === todayStr;

    const eventsHtml = dayEvents.map(function (e)
    {
      const color = colorForTitle(e.title);
      const sub = e.subtitle + (e.time ? " &middot; " + e.time : "");
      return (
        '<div class="cal-event' + (e.has_file ? " has-file" : "") + '" style="border-left-color:' + color + '" title="' + escapeHtml(e.title) + " (" + escapeHtml(e.subtitle) + ')">' +
          '<div class="cal-event-title">' + escapeHtml(e.title) + '</div>' +
          '<div class="cal-event-sub">' + escapeHtml(sub) + '</div>' +
        '</div>'
      );
    }).join("");

    cells.push(
      '<div class="cal-cell' + (inMonth ? "" : " dim") + (isToday ? " today" : "") + '">' +
        '<div class="cal-daynum">' + cursor.getDate() + '</div>' +
        eventsHtml +
      '</div>'
    );
    cursor.setDate(cursor.getDate() + 1);
  }

  document.getElementById("calGrid").innerHTML = cells.join("");
}

let viewingCurrentMonth = true;

function shiftMonth(delta)
{
  viewingCurrentMonth = false;
  calMonth += delta;
  if (calMonth < 0)
  {
    calMonth = 11;
    calYear -= 1;
  }
  if (calMonth > 11)
  {
    calMonth = 0;
    calYear += 1;
  }
  loadCalendar();
}

function goToday()
{
  viewingCurrentMonth = true;
  const now = new Date();
  calYear = now.getFullYear();
  calMonth = now.getMonth();
  loadCalendar();
}

goToday();
setInterval(function ()
{
  // only auto-follow the date rollover if the user hasn't manually browsed
  // to a different month -- don't yank them out of one they chose to view
  const now = new Date();
  if (viewingCurrentMonth && (now.getFullYear() !== calYear || now.getMonth() !== calMonth))
  {
    goToday();
  }
  else
  {
    loadCalendar();
  }
}, 60000);
</script>
</body>
</html>
"""

PAGE_CALENDAR = PAGE_CALENDAR.replace("/bg.png", f"/bg.png?v={BG_VERSION}")

PAGE_ADD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>arr-queue-cleaner &mdash; add</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root
  {
    --ink: #1c234c;
    --glass: rgba(28, 32, 66, 0.52);
    --glass-border: rgba(255, 255, 255, 0.16);
    --text: #eef0ff;
    --dim: #b9bfe6;
    --coral: #f2b6ae;
    --coral-strong: #f5978b;
    --mint: #a6e3c8;
    --star: #fff6df;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body
  {
    margin: 0;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 0 1.25rem 4rem;
    background-image:
      radial-gradient(1.5px 1.5px at 12% 18%, var(--star) 50%, transparent 55%),
      radial-gradient(1.5px 1.5px at 24% 9%, var(--star) 50%, transparent 55%),
      radial-gradient(1px 1px at 33% 26%, var(--star) 50%, transparent 55%),
      url("/bg.png");
    background-repeat: no-repeat;
    background-size: auto, auto, auto, cover;
    background-position: center;
    background-attachment: fixed;
  }
  body::before
  {
    content: "";
    position: fixed;
    inset: 0;
    background: linear-gradient(180deg, rgba(12, 15, 40, 0.35) 0%, rgba(12, 15, 40, 0.15) 40%, rgba(12, 15, 40, 0.45) 100%);
    pointer-events: none;
    z-index: 0;
  }
  .wrap { max-width: 760px; margin: 0 auto; position: relative; z-index: 1; padding-top: 2.5rem; }
  .topbar
  {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 0 -1.25rem 2rem;
    padding: 1.5rem 2rem 1.75rem;
  }
  .topbar-nav { display: flex; gap: 2rem; font-size: 0.95rem; color: var(--dim); }
  .topbar-nav a { color: inherit; text-decoration: none; }
  .topbar-nav a.active { color: var(--text); font-weight: 600; border-bottom: 2px solid var(--coral); padding-bottom: 0.3rem; }
  .back-link { color: var(--star); font-size: 0.9rem; font-weight: 600; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }

  .tabs { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; }
  .tab
  {
    flex: 1;
    text-align: center;
    padding: 0.7rem;
    border-radius: 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    color: var(--dim);
    font-weight: 600;
    cursor: pointer;
  }
  .tab.active { color: var(--text); border-color: var(--coral); background: rgba(242, 182, 174, 0.12); }

  .search-row { display: flex; gap: 0.6rem; margin-bottom: 1rem; }
  .search-input
  {
    flex: 1;
    padding: 0.8rem 1rem;
    border-radius: 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    color: var(--text);
    font-size: 0.95rem;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
  .search-input::placeholder { color: var(--dim); }
  .search-input:focus { outline: none; border-color: var(--coral); }

  .settings-row
  {
    display: flex;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
    padding: 0.9rem 1rem;
    border-radius: 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    align-items: center;
    flex-wrap: wrap;
  }
  .settings-row label { font-size: 0.78rem; color: var(--dim); }
  .settings-row select
  {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid var(--glass-border);
    color: var(--text);
    padding: 0.4rem 0.6rem;
    border-radius: 8px;
    font-size: 0.85rem;
  }

  .result-card
  {
    display: flex;
    gap: 1rem;
    padding: 1rem;
    border-radius: 14px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    margin-bottom: 0.9rem;
  }
  .result-poster { width: 64px; height: 96px; border-radius: 8px; object-fit: cover; flex: none; background: rgba(255, 255, 255, 0.05); }
  .result-info { flex: 1; min-width: 0; }
  .result-title { font-size: 0.98rem; font-weight: 700; color: var(--text); }
  .result-year { color: var(--dim); font-weight: 400; }
  .result-overview { font-size: 0.8rem; color: var(--dim); margin-top: 0.3rem; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .result-action { flex: none; display: flex; align-items: center; }
  .add-btn
  {
    padding: 0.55rem 1.1rem;
    border-radius: 999px;
    background: rgba(166, 227, 200, 0.16);
    border: 1px solid rgba(166, 227, 200, 0.5);
    color: var(--mint);
    font-weight: 600;
    font-size: 0.85rem;
    cursor: pointer;
    white-space: nowrap;
  }
  .add-btn:hover { background: rgba(166, 227, 200, 0.26); }
  .add-btn:disabled { opacity: 0.6; cursor: default; }
  .add-btn.added { color: var(--dim); border-color: var(--glass-border); background: transparent; }
  .add-btn.failed { color: var(--coral-strong); border-color: rgba(245, 151, 139, 0.5); background: rgba(245, 151, 139, 0.12); }
  .empty { color: var(--dim); text-align: center; padding: 2.5rem; }
</style>
</head>
<body>

<header class="topbar">
  <a class="back-link" href="/">&larr; Overview</a>
  <nav class="topbar-nav">
    <a href="/">Overview</a>
    <a href="/calendar">Calendar</a>
    <a href="/library">Library</a>
    <a href="/history">History</a>
  </nav>
  <div style="width: 90px"></div>
</header>

<div class="wrap">
  <div class="tabs">
    <div class="tab" id="tabShow" onclick="switchType('show')">TV Show</div>
    <div class="tab" id="tabMovie" onclick="switchType('movie')">Movie</div>
  </div>

  <div class="search-row">
    <input class="search-input" id="searchInput" type="text" placeholder="Search for a title&hellip;" oninput="onSearchInput()">
  </div>

  <div class="settings-row">
    <label>Quality</label>
    <select id="profileSelect"></select>
    <label>Folder</label>
    <select id="folderSelect" onchange="onFolderChange()"></select>
    <span id="typeField" style="display: inline-flex; align-items: center; gap: 0.75rem;">
      <label>Type</label>
      <select id="typeSelect">
        <option value="standard">Standard</option>
        <option value="anime">Anime</option>
      </select>
    </span>
  </div>

  <div id="results"><div class="empty">Start typing to search</div></div>
</div>

<script>
let currentType = "show";
let searchTimer = null;
let searchToken = 0;

function escapeHtml(s)
{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function getUrlType()
{
  const params = new URLSearchParams(window.location.search);
  const t = params.get("type");
  return t === "movie" ? "movie" : "show";
}

let folderSeriesTypeMap = {};

async function loadDefaults()
{
  const res = await fetch("/api/add-defaults?type=" + currentType);
  const data = await res.json();

  const profileSelect = document.getElementById("profileSelect");
  const folderSelect = document.getElementById("folderSelect");

  profileSelect.innerHTML = (data.profiles || []).map(function (p)
  {
    return '<option value="' + p.id + '"' + (p.id === data.default_profile_id ? " selected" : "") + '>' + escapeHtml(p.name) + '</option>';
  }).join("");

  folderSelect.innerHTML = (data.folders || []).map(function (f)
  {
    return '<option value="' + escapeHtml(f.path) + '"' + (f.path === data.default_folder ? " selected" : "") + '>' + escapeHtml(f.path) + '</option>';
  }).join("");

  document.getElementById("typeField").style.display = currentType === "show" ? "" : "none";
  folderSeriesTypeMap = {};
  (data.folders || []).forEach(function (f) { folderSeriesTypeMap[f.path] = f.default_series_type || "standard"; });
  document.getElementById("typeSelect").value = data.default_series_type || "standard";
}

function onFolderChange()
{
  if (currentType !== "show") return;
  const folder = document.getElementById("folderSelect").value;
  document.getElementById("typeSelect").value = folderSeriesTypeMap[folder] || "standard";
}

function switchType(type)
{
  currentType = type;
  document.getElementById("tabShow").classList.toggle("active", type === "show");
  document.getElementById("tabMovie").classList.toggle("active", type === "movie");
  const url = new URL(window.location);
  url.searchParams.set("type", type);
  window.history.replaceState({}, "", url);
  loadDefaults();
  const term = document.getElementById("searchInput").value.trim();
  if (term.length >= 2)
  {
    runSearch(term);
  }
  else
  {
    document.getElementById("results").innerHTML = '<div class="empty">Start typing to search</div>';
  }
}

function onSearchInput()
{
  const term = document.getElementById("searchInput").value.trim();
  clearTimeout(searchTimer);
  if (term.length < 2)
  {
    document.getElementById("results").innerHTML = '<div class="empty">Start typing to search</div>';
    return;
  }
  searchTimer = setTimeout(function () { runSearch(term); }, 400);
}

async function runSearch(term)
{
  const myToken = ++searchToken;
  document.getElementById("results").innerHTML = '<div class="empty">Searching&hellip;</div>';

  let data;
  try
  {
    const res = await fetch("/api/lookup?type=" + currentType + "&term=" + encodeURIComponent(term));
    data = await res.json();
  }
  catch (e)
  {
    data = { error: String(e) };
  }

  if (myToken !== searchToken)
  {
    return;  // a newer search superseded this one
  }

  if (data.error)
  {
    document.getElementById("results").innerHTML = '<div class="empty">Search failed: ' + escapeHtml(data.error) + '</div>';
    return;
  }

  const results = data.results || [];
  if (results.length === 0)
  {
    document.getElementById("results").innerHTML = '<div class="empty">No matches found</div>';
    return;
  }

  document.getElementById("results").innerHTML = results.map(function (r, idx)
  {
    const poster = r.poster ? '<img class="result-poster" src="' + r.poster + '">' : '<div class="result-poster"></div>';
    const btn = r.already_added
      ? '<button class="add-btn added" disabled>In library</button>'
      : '<button class="add-btn" id="add-btn-' + idx + '" onclick="doAdd(' + idx + ')">Add</button>';
    return (
      '<div class="result-card">' +
        poster +
        '<div class="result-info">' +
          '<div class="result-title">' + escapeHtml(r.title) + ' <span class="result-year">' + (r.year || "") + '</span></div>' +
          '<div class="result-overview">' + escapeHtml(r.overview || "") + '</div>' +
        '</div>' +
        '<div class="result-action">' + btn + '</div>' +
      '</div>'
    );
  }).join("");

  window.currentResults = results;
}

async function doAdd(idx)
{
  const r = window.currentResults[idx];
  const btn = document.getElementById("add-btn-" + idx);
  btn.disabled = true;
  btn.textContent = "Adding…";

  const payload = {
    type: currentType,
    tvdb_id: r.tvdb_id,
    tmdb_id: r.tmdb_id,
    profile_id: document.getElementById("profileSelect").value,
    root_folder: document.getElementById("folderSelect").value,
    series_type: currentType === "show" ? document.getElementById("typeSelect").value : undefined,
  };

  try
  {
    const res = await fetch("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok)
    {
      btn.textContent = "Added";
      btn.classList.add("added");
    }
    else
    {
      btn.textContent = "Failed";
      btn.classList.add("failed");
      btn.disabled = false;
    }
  }
  catch (e)
  {
    btn.textContent = "Failed";
    btn.classList.add("failed");
    btn.disabled = false;
  }
}

currentType = getUrlType();
document.getElementById("tabShow").classList.toggle("active", currentType === "show");
document.getElementById("tabMovie").classList.toggle("active", currentType === "movie");
loadDefaults();
</script>
</body>
</html>
"""

PAGE_ADD = PAGE_ADD.replace("/bg.png", f"/bg.png?v={BG_VERSION}")

PAGE_LIBRARY = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>arr-queue-cleaner &mdash; library</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root
  {
    --ink: #1c234c;
    --glass: rgba(28, 32, 66, 0.52);
    --glass-border: rgba(255, 255, 255, 0.16);
    --text: #eef0ff;
    --dim: #b9bfe6;
    --coral: #f2b6ae;
    --coral-strong: #f5978b;
    --mint: #a6e3c8;
    --star: #fff6df;
    --blue: #93bdf2;
    --orange: #f2c48a;
    --purple: #c6a8f0;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body
  {
    margin: 0;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 0 1.25rem 4rem;
    background-image:
      radial-gradient(1.5px 1.5px at 12% 18%, var(--star) 50%, transparent 55%),
      radial-gradient(1.5px 1.5px at 24% 9%, var(--star) 50%, transparent 55%),
      radial-gradient(1px 1px at 33% 26%, var(--star) 50%, transparent 55%),
      url("/bg.png");
    background-repeat: no-repeat;
    background-size: auto, auto, auto, cover;
    background-position: center;
    background-attachment: fixed;
  }
  body::before
  {
    content: "";
    position: fixed;
    inset: 0;
    background: linear-gradient(180deg, rgba(12, 15, 40, 0.35) 0%, rgba(12, 15, 40, 0.15) 40%, rgba(12, 15, 40, 0.45) 100%);
    pointer-events: none;
    z-index: 0;
  }
  .wrap { max-width: 1200px; margin: 0 auto; position: relative; z-index: 1; padding-top: 2.5rem; }
  .topbar
  {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 0 -1.25rem 2rem;
    padding: 1.5rem 2rem 1.75rem;
  }
  .topbar-nav { display: flex; gap: 2rem; font-size: 0.95rem; color: var(--dim); }
  .topbar-nav a { color: inherit; text-decoration: none; }
  .topbar-nav a.active { color: var(--text); font-weight: 600; border-bottom: 2px solid var(--coral); padding-bottom: 0.3rem; }
  .back-link { color: var(--star); font-size: 0.9rem; font-weight: 600; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }

  .lib-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.25rem; gap: 1rem; flex-wrap: wrap; }
  .tabs { display: flex; gap: 0.5rem; }
  .tab
  {
    padding: 0.6rem 1.3rem;
    border-radius: 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    color: var(--dim);
    font-weight: 600;
    cursor: pointer;
    font-size: 0.9rem;
  }
  .tab.active { color: var(--text); border-color: var(--coral); background: rgba(242, 182, 174, 0.12); }
  .lib-count { font-size: 0.85rem; color: var(--dim); }

  .filter-input
  {
    width: 100%;
    padding: 0.75rem 1rem;
    border-radius: 12px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    color: var(--text);
    font-size: 0.9rem;
    margin-bottom: 1.5rem;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
  .filter-input::placeholder { color: var(--dim); }
  .filter-input:focus { outline: none; border-color: var(--coral); }

  .lib-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 1.1rem; }
  .lib-card { border-radius: 12px; overflow: hidden; background: var(--glass); border: 1px solid var(--glass-border); }
  .lib-poster { width: 100%; aspect-ratio: 2 / 3; object-fit: cover; display: block; background: rgba(255, 255, 255, 0.05); }
  .lib-info { padding: 0.55rem 0.65rem; }
  .lib-title { font-size: 0.8rem; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lib-year { font-size: 0.7rem; color: var(--dim); margin-bottom: 0.35rem; }
  .lib-bar-track { height: 4px; border-radius: 999px; background: rgba(255, 255, 255, 0.1); overflow: hidden; margin-bottom: 0.3rem; }
  .lib-bar-fill { height: 100%; border-radius: 999px; }
  .lib-bar-fill.continuing { background: var(--blue); }
  .lib-bar-fill.ended { background: var(--mint); }
  .lib-bar-fill.downloaded { background: var(--mint); }
  .lib-bar-fill.missing_monitored { background: var(--coral-strong); }
  .lib-bar-fill.missing_unmonitored { background: var(--orange); }
  .lib-bar-fill.downloading { background: var(--purple); }
  .lib-meta { font-size: 0.65rem; color: var(--dim); display: flex; justify-content: space-between; }
  .empty { color: var(--dim); text-align: center; padding: 2.5rem; grid-column: 1 / -1; }

  .legend-stats
  {
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    justify-content: space-between;
    padding: 1rem 1.25rem;
    border-radius: 14px;
    background: var(--glass);
    border: 1px solid var(--glass-border);
    margin-bottom: 1.5rem;
    font-size: 0.8rem;
  }
  .legend { display: flex; flex-direction: column; gap: 0.4rem; }
  .legend-item { display: flex; align-items: center; gap: 0.5rem; color: var(--dim); }
  .legend-swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .swatch-continuing { background: var(--blue); }
  .swatch-ended { background: var(--mint); }
  .swatch-downloaded { background: var(--mint); }
  .swatch-missing_monitored { background: var(--coral-strong); }
  .swatch-missing_unmonitored { background: var(--orange); }
  .swatch-downloading { background: var(--purple); }
  .stat-cols { display: flex; gap: 2rem; flex-wrap: wrap; }
  .stat-col { display: flex; flex-direction: column; gap: 0.3rem; }
  .stat-row { display: flex; justify-content: space-between; gap: 1rem; color: var(--dim); }
  .stat-row strong { color: var(--text); font-weight: 700; }
</style>
</head>
<body>

<header class="topbar">
  <a class="back-link" href="/">&larr; Overview</a>
  <nav class="topbar-nav">
    <a href="/">Overview</a>
    <a href="/calendar">Calendar</a>
    <a href="/library" class="active">Library</a>
    <a href="/history">History</a>
  </nav>
  <div style="width: 90px"></div>
</header>

<div class="wrap">
  <div class="lib-header">
    <div class="tabs">
      <div class="tab" id="tabShow" onclick="switchType('show')">Series</div>
      <div class="tab" id="tabMovie" onclick="switchType('movie')">Movies</div>
    </div>
    <div class="lib-count" id="libCount">&nbsp;</div>
  </div>

  <div class="legend-stats" id="legendStats"></div>

  <input class="filter-input" id="filterInput" type="text" placeholder="Filter by title&hellip;" oninput="renderGrid()">

  <div class="lib-grid" id="libGrid"><div class="empty">Loading&hellip;</div></div>
</div>

<script>
let currentType = "show";
let libraryItems = [];

function escapeHtml(s)
{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function getUrlType()
{
  const params = new URLSearchParams(window.location.search);
  const t = params.get("type");
  return t === "movie" ? "movie" : "show";
}

function switchType(type)
{
  currentType = type;
  document.getElementById("tabShow").classList.toggle("active", type === "show");
  document.getElementById("tabMovie").classList.toggle("active", type === "movie");
  const url = new URL(window.location);
  url.searchParams.set("type", type);
  window.history.replaceState({}, "", url);
  document.getElementById("filterInput").value = "";
  loadLibrary();
}

async function loadLibrary()
{
  document.getElementById("libGrid").innerHTML = '<div class="empty">Loading&hellip;</div>';
  document.getElementById("libCount").textContent = "";

  let data;
  try
  {
    const res = await fetch("/api/library?type=" + currentType);
    data = await res.json();
  }
  catch (e)
  {
    data = { error: String(e) };
  }

  if (data.error)
  {
    document.getElementById("libGrid").innerHTML = '<div class="empty">Failed to load: ' + escapeHtml(data.error) + '</div>';
    return;
  }

  libraryItems = data.items || [];
  document.getElementById("libCount").textContent = libraryItems.length + (currentType === "show" ? " series" : " movies");
  renderLegendStats(data.stats || {});
  renderGrid();
}

function renderLegendStats(stats)
{
  const legendItems = currentType === "show"
    ? [
        ["continuing", "Continuing (All episodes downloaded)"],
        ["ended", "Ended (All episodes downloaded)"],
        ["missing_monitored", "Missing Episodes (Series monitored)"],
        ["missing_unmonitored", "Missing Episodes (Series not monitored)"],
        ["downloading", "Downloading (One or more episodes)"],
      ]
    : [
        ["downloaded", "Downloaded"],
        ["missing_monitored", "Missing (Monitored)"],
        ["missing_unmonitored", "Missing (Not monitored)"],
        ["downloading", "Downloading"],
      ];

  const legendHtml = legendItems.map(function (li)
  {
    return '<div class="legend-item"><span class="legend-swatch swatch-' + li[0] + '"></span>' + li[1] + '</div>';
  }).join("");

  const statCols = currentType === "show"
    ? [
        [["Series", stats.total], ["Monitored", stats.monitored], ["Unmonitored", stats.unmonitored]],
        [["Continuing", stats.continuing], ["Ended", stats.ended]],
        [["Episodes", stats.episodes], ["Files", stats.files]],
        [["Total File Size", stats.size_display]],
      ]
    : [
        [["Movies", stats.total], ["Monitored", stats.monitored], ["Unmonitored", stats.unmonitored]],
        [["Downloaded", stats.downloaded], ["Missing", stats.missing_monitored + stats.missing_unmonitored]],
        [["Total File Size", stats.size_display]],
      ];

  const statsHtml = statCols.map(function (col)
  {
    const rows = col.map(function (pair)
    {
      return '<div class="stat-row"><span>' + pair[0] + '</span><strong>' + pair[1] + '</strong></div>';
    }).join("");
    return '<div class="stat-col">' + rows + '</div>';
  }).join("");

  document.getElementById("legendStats").innerHTML =
    '<div class="legend">' + legendHtml + '</div>' +
    '<div class="stat-cols">' + statsHtml + '</div>';
}

function renderGrid()
{
  const term = document.getElementById("filterInput").value.trim().toLowerCase();
  const filtered = term
    ? libraryItems.filter(function (i) { return (i.title || "").toLowerCase().indexOf(term) !== -1; })
    : libraryItems;

  const grid = document.getElementById("libGrid");
  if (filtered.length === 0)
  {
    grid.innerHTML = '<div class="empty">No matches</div>';
    return;
  }

  grid.innerHTML = filtered.map(function (item)
  {
    const poster = item.poster ? '<img class="lib-poster" src="' + item.poster + '" loading="lazy">' : '<div class="lib-poster"></div>';
    const pct = item.total > 0 ? Math.round(100 * item.has_file / item.total) : 0;
    const barClass = item.category;
    const countLabel = currentType === "show" ? (item.has_file + "/" + item.total) : (item.has_file ? "Downloaded" : "Missing");

    return (
      '<div class="lib-card">' +
        poster +
        '<div class="lib-info">' +
          '<div class="lib-title" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</div>' +
          '<div class="lib-year">' + (item.year || "") + '</div>' +
          '<div class="lib-bar-track"><div class="lib-bar-fill ' + barClass + '" style="width:' + pct + '%"></div></div>' +
          '<div class="lib-meta"><span>' + escapeHtml(item.profile) + '</span><span>' + countLabel + '</span></div>' +
        '</div>' +
      '</div>'
    );
  }).join("");
}

currentType = getUrlType();
document.getElementById("tabShow").classList.toggle("active", currentType === "show");
document.getElementById("tabMovie").classList.toggle("active", currentType === "movie");
loadLibrary();
</script>
</body>
</html>
"""

PAGE_LIBRARY = PAGE_LIBRARY.replace("/bg.png", f"/bg.png?v={BG_VERSION}")


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        query = urllib.parse.parse_qs(split.query)

        if path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/history":
            body = PAGE_HISTORY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/calendar":
            body = PAGE_CALENDAR.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/calendar":
            start = query.get("start", [""])[0]
            end = query.get("end", [""])[0]
            if not start or not end:
                self.send_response(400)
                self.end_headers()
                return
            self._send_json(get_calendar_events(start, end))
        elif path == "/add":
            body = PAGE_ADD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/lookup":
            kind = query.get("type", [""])[0]
            term = query.get("term", [""])[0]
            if kind not in ("show", "movie") or not term:
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._send_json(search_lookup(kind, term))
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/api/add-defaults":
            kind = query.get("type", [""])[0]
            if kind not in ("show", "movie"):
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._send_json(get_add_defaults(kind))
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/library":
            body = PAGE_LIBRARY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/library":
            kind = query.get("type", [""])[0]
            if kind not in ("show", "movie"):
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._send_json(get_library(kind))
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/api/status":
            self._send_json({
                "next_run": next_run_time().isoformat(),
                "next_health_check_run": next_run_time(minutes=HEALTH_CHECK_CRON_MINUTES).isoformat(),
                "server_time": datetime.now().isoformat(),
            })
        elif path == "/api/log":
            self._send_json({"runs": parse_log()[:50]})
        elif path == "/api/downloads":
            self._send_json({"items": get_active_downloads()})
        elif path == "/api/health":
            self._send_json(get_server_health())
        elif path == "/api/commands":
            self._send_json(get_command_queue())
        elif path == "/bg.png":
            self.send_response(200)
            self.send_header("Content-Type", BG_IMAGE_CONTENT_TYPE)
            self.send_header("Content-Length", str(len(BG_IMAGE_BYTES)))
            # the URL is content-hash-versioned (?v=...), so it's always safe
            # to cache hard -- a changed background gets a new URL entirely
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(BG_IMAGE_BYTES)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path

        if path == "/api/add":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                kind = body.get("type")
                if kind not in ("show", "movie"):
                    raise ValueError("type must be 'show' or 'movie'")
                result = add_media(kind, body)
                self._send_json({"ok": True, "title": result.get("title")})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep the console quiet, this runs as a background service


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()
