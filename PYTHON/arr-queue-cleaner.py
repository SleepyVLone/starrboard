#!/usr/bin/env python3
# Auto-clean several distinct classes of stuck or wasteful arr queue items,
# plus a passive disk-space check. Designed to run unattended indefinitely --
# every fix here is conservative and reversible (blocklist + fresh search,
# never a guess at what to import without high confidence).
#
# 1) "Not an upgrade" limbo: redundant lower-quality grabs for content we already
#    have at equal/better quality. They sit in the queue as importBlocked forever
#    and never self-resolve. Safe to remove + blocklist (we never want a downgrade).
#    Numbering-mismatch stuck items are deliberately left alone (they may need a
#    manual import decision).
#
# 2) Dead torrents: qBittorrent shows a torrent actively downloading/stalled/
#    fetching-metadata that hasn't downloaded a single additional byte across
#    two consecutive 30-min checks (a seedless/dead magnet that can never
#    complete). These don't show up as "importBlocked" in Sonarr/Radarr at all
#    -- their trackedDownloadStatus reads "ok" the whole time, they just never
#    make progress. Tracked via persisted state (/tmp/arr-queue-cleaner-torrent-
#    state.json) comparing bytes downloaded run-over-run, not an instantaneous
#    0-seeds-and-0-speed snapshot -- a torrent can show a brief nonzero seed or
#    speed blip without ever making real progress, which let a real one sit
#    stalled 15+ hours undetected under the old single-snapshot check.
#    Deliberately excludes "queuedDL" torrents waiting on the concurrency cap
#    for a free slot -- that's expected, not dead.
#    Fix: remove + blocklist via the matching Sonarr/Radarr queue entry (so the
#    exact same release won't be re-grabbed), then trigger a fresh search for the
#    affected series/movie so it's replaced, not just deleted.
#
# 3) Malicious/disguised executables: a release whose entire downloaded item is
#    a single .exe/.scr/.bat/etc file rather than a real video -- a
#    disguised-malware pattern, not a legitimate release regardless of file
#    size. Hit live with a TV episode release padded out to a bare .exe
#    (1.2GB, plausible size for a real episode, but TV/movie releases are
#    never shipped as a bare executable).
#    Deleted from disk directly, blocklisted, fresh search triggered. Never
#    inspects or runs the file -- detection is path-based only.
#
# 4) Orphaned completed downloads that were never imported: a torrent finishes
#    100% in qBittorrent but Sonarr/Radarr never imports it, usually because it
#    was added directly to qBittorrent (manual grab) rather than through
#    Sonarr/Radarr's own search+grab, so there's no queue entry to trigger the
#    post-download import. Two safety tiers, both requiring high confidence
#    before acting -- anything less certain is logged for manual review, never
#    guessed at:
#      Tier 1 (any instance): manual-import scan comes back with zero rejections
#      for the file -- it's already a clean, unambiguous match. Import as-is.
#      Tier 2 (Sonarr only): the file is rejected (commonly "not an upgrade")
#      because Sonarr misparsed the season/episode from the filename, but a
#      plain SxxEyy/Sx-yy pattern in the filename gives a different, specific
#      (season, episode) than Sonarr's guess, AND that specific episode doesn't
#      already have a file. Only then is the episode ID override applied --
#      this is exactly the bug class hit live when a release's filename used
#      `S2 - 01.mkv` instead of the usual `S02E01` pattern and got misread as
#      season 1, and is safe because it only fires when the
#      regex-derived target is both different from Sonarr's guess and
#      genuinely missing, never overriding a legitimate "not an upgrade" call.
#    Radarr gets Tier 1 only -- movie identity isn't safely guessable from a
#    filename pattern the way an episode number is, so anything Radarr can't
#    already match cleanly is left for manual review rather than risked.
#
# 5) Redundant downloads: a torrent still actively downloading (real seeds,
#    real speed -- clean_dead_torrents never flags these) for a series/movie
#    Sonarr/Radarr already has every file for. Usually a leftover duplicate
#    grab from an earlier batch search; left alone it occupies a download
#    slot for its entire runtime doing nothing. skipRedownload=true, no
#    fresh search triggered -- the content is already satisfied.
#
# 6) Disk space: passively checked via qBittorrent's own free_space_on_disk
#    every run. Only warns in the log -- a full disk is one of the quietest
#    ways this whole pipeline stops working, and nothing else here would
#    ever explain why torrents stall or imports fail once it happens.
#
# 7) Bonus-only releases (Sonarr only): some anime batch releases bundle raw
#    BD disc dumps ("Vol 1.mkv", "Vol 2.mkv" -- each containing multiple
#    episodes as one undivided file) and/or promotional extras ("PV01.mkv",
#    "NCOP.mkv", etc.) instead of one file per episode. Sonarr can never
#    auto-match these to a single episode number -- not a numbering-scheme
#    mismatch fixable by regex like the similar case described in
#    rescue_stuck_imports below, but a fundamentally different file-per-episode
#    structure. Hit live with an anime season's BDRip batch (Vol 1/Vol 2 +
#    6 PV files, zero real per-episode files in the release at all).
#    Conservative by design: only fires when EVERY file Sonarr rejected with
#    "Invalid season or episode" matches a known bonus/disc-dump filename
#    pattern. If even one rejected file looks like a real episode, the whole
#    release is left alone for manual review -- that's a different, higher-
#    stakes failure (an actual numbering problem) this isn't meant to touch.
#
# 8) Recovered blocklist entries: a release blocklisted while genuinely dead
#    (0 seeds) stays permanently unusable even if real seeders show up
#    later -- Sonarr/Radarr refuse a blocklisted release outright without
#    ever re-checking its current seeder count (hit live: one TV episode's
#    release was blocklisted while seedless, then had 13
#    healthy seeders weeks later and just kept getting rejected as "Release
#    is blocklisted" forever). This removes blocklist entries for content
#    that's STILL missing a file, so the next automatic search re-evaluates
#    them fresh -- if a release is genuinely still dead, Sonarr/Radarr's own
#    live seeder/quality checks reject it again at grab time exactly as
#    before, so nothing unsafe can get through either way. Two things this
#    never touches, both permanently: (a) any release this script has ever
#    logged as SECURITY (disguised-malware-executable), matched by title,
#    and (b) anything blocklisted as "not an upgrade" -- which is naturally
#    excluded for free, since that only ever fires when the episode/movie
#    already HAS a file, so it never appears in the "still missing" set.
#    Only reconsiders entries blocklisted 24+ hours ago (BLOCKLIST_RECOVERY_
#    MIN_AGE_HOURS): without that gate this fights the 10-minute health
#    check's 0-seed-stall watchdog directly -- the watchdog blocklists a
#    genuinely dead release, this recovers it minutes later since the
#    episode's still missing, Sonarr re-grabs the same dead release, the
#    watchdog blocklists it again. Hit live with a multi-episode batch
#    cycling grab/fail every 20-30 minutes for hours straight before this
#    was caught. A release that's actually recovered has plenty of real-world
#    time to still be there a day later.
#
# 9) Phantom-complete downloads: qBittorrent can report a torrent at 100%
#    progress ("stoppedUP") while the actual files are missing or incomplete
#    on disk -- Sonarr/Radarr then sit forever at "Downloaded - Waiting to
#    Import" / "No files found are eligible for import", since as far as
#    they know the download genuinely finished. Discovered live with a 23GB
#    TV batch: forcing a qBittorrent recheck immediately dropped
#    it from 100% to 0% and moved it back to the incomplete folder, proving
#    the "complete" state was stale, not real (root cause unconfirmed --
#    ruled out disk space exhaustion, a qBittorrent/gluetun container
#    restart, and qBittorrent's own ratio-limit action, which is Pause here,
#    not delete). Almost certainly also explains two earlier mystery
#    failures (two other batches both went from grabbed to
#    entirely gone with no script of ours touching them). Only fires after a
#    download has sat stuck this way for 45+ minutes (its own persisted
#    state, /tmp/arr-queue-cleaner-phantom-state.json) and qBittorrent still
#    claims 100% -- forces a recheck to get the true state, then gets out of
#    the way: if the recheck confirms it's genuinely complete (a real
#    wrong-content case, not phantom), nothing here changes and it's left
#    for rescue_stuck_imports' per-file review as before; if the recheck
#    exposes it as fake, the existing dead-torrent check (byte-progress
#    across 2 checks) naturally blocklists and re-searches it on its own.

#
# 10) Orphaned additions: a monitored series/movie that has aired/released
#     content but has NEVER had a single grab or import, days after being
#     added. Nothing else here would ever catch this -- it produces no queue
#     entry, no history, no error, it just silently never gets searched
#     successfully. Discovered live: a live-action car-review series was added
#     with seriesType 'anime' and an anime root folder/quality profile by
#     mistake -- anime-type release parsing never
#     matches normal SxxEyy-named releases, so it sat at 0 episodes for a full
#     week with zero errors anywhere until a human noticed it missing from
#     Jellyfin. Deliberately flag-only, never auto-fixes: the correct fix
#     (seriesType / root folder / quality profile) needs human judgement --
#     blindly flipping seriesType risks breaking a show that's genuinely
#     anime. Deduped via its own ledger so a still-unresolved item is reported
#     once, not every 30 minutes forever.

import json
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from arr_common import config
from arr_common.config import SONARR, RADARR, INSTANCES, QBIT_BASE
from arr_common.qbittorrent import login as qbit_login
from arr_common.qbittorrent import get as qbit_get
from arr_common.state import load_json_state, save_json_state

SELF_LOG_PATH = "/var/log/arr-queue-cleaner.log"  # this script's own cron-redirected output

# A torrent must be at least this old (seconds) before it's eligible to be
# flagged dead -- gives fresh grabs time to announce to trackers first.
DEAD_MIN_AGE_SECONDS = 20 * 60
# Deliberately excludes "queuedDL" (waiting on the concurrency cap for a free
# slot -- expected, not dead) and any "...UP" state (already finished). Also
# includes "stoppedDL": discovered live that several genuinely-dead torrents
# (a batch of anime episodes with zero seeders confirmed by their own
# tracker, not just our client) end up sitting in "stoppedDL" rather than actively
# retrying in "stalledDL" -- a torrent stuck there making zero byte progress
# for 20+ minutes is exactly as dead as one stuck in an active-looking state,
# it just never shows up if this only watches the active-looking ones.
# Also includes "error": found live 2026-07-31 -- a torrent that qBittorrent
# itself flags as errored (not stalled, not downloading) was invisible to
# this check entirely, so a genuinely-dead errored torrent sat stuck in the
# Sonarr queue indefinitely with no automation ever touching it.
DEAD_STATES = {"metaDL", "stalledDL", "downloading", "stoppedDL", "error"}
# Deliberately excludes "queuedDL" (waiting on the concurrency cap, expected) and
# any "...UP" state (already finished downloading -- never treat a completed
# torrent as dead regardless of its seed count).


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def delete_bulk(base, key, ids):
    body = json.dumps({"ids": ids}).encode()
    url = (
        f"{base}/api/v3/queue/bulk?removeFromClient=true"
        f"&blocklist=true&skipRedownload=true&apikey={key}"
    )
    req = urllib.request.Request(
        url, data=body, method="DELETE",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)


def trigger_search(base, key, name, entity_id):
    if name == "Sonarr":
        payload = {"name": "SeriesSearch", "seriesId": entity_id}
    else:
        payload = {"name": "MoviesSearch", "movieIds": [entity_id]}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base}/api/v3/command?apikey={key}", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)


def clean_not_an_upgrade(name, base, key):
    data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000")
    ids = []
    for r in data.get("records", []):
        msgs = " ".join(
            m for sm in r.get("statusMessages", []) for m in sm.get("messages", [])
        )
        if r.get("trackedDownloadState") == "importBlocked" and "Not an upgrade" in msgs:
            ids.append(r["id"])
    if not ids:
        return f"{name}: nothing to clean (not-an-upgrade)"
    delete_bulk(base, key, ids)
    return f"{name}: removed {len(ids)} 'not an upgrade' stuck items -> {ids}"


BONUS_FILENAME_PATTERN = re.compile(
    r'^(vol\.?\s*\d+|pv\d*|sp\d*|nced\d*|ncop\d*|ova\d*|menu|extra|special|omake|creditless|bonus)',
    re.IGNORECASE,
)


def clean_bonus_only_releases(name, base, key):
    if name != "Sonarr":
        return f"{name}: n/a (bonus-only releases is Sonarr-only)"

    data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000")

    by_download = {}
    for r in data.get("records", []):
        if r.get("trackedDownloadState") != "importBlocked":
            continue
        by_download.setdefault(r.get("downloadId"), []).append(r)

    results = []
    for download_id, rows in by_download.items():
        r0 = rows[0]
        file_messages = [
            sm for sm in r0.get("statusMessages", [])
            if sm.get("title") and sm.get("messages")
        ]
        if not file_messages:
            continue
        if not all(sm["messages"] == ["Invalid season or episode"] for sm in file_messages):
            continue
        if not all(BONUS_FILENAME_PATTERN.match(sm["title"].strip()) for sm in file_messages):
            continue  # at least one file looks like a real episode -- leave for manual review

        ids = [r["id"] for r in rows]
        title = r0.get("title", "")
        try:
            delete_bulk(base, key, ids)
            entity_id = r0.get("seriesId")
            if entity_id:
                trigger_search(base, key, name, entity_id)
            results.append(
                f"  BONUS-ONLY RELEASE: '{title[:70]}' -- every file was a disc-dump/PV/extra "
                f"({len(file_messages)} files, none map to a real episode) -> removed {len(ids)} "
                f"queue row(s), blocklisted, re-search triggered"
            )
        except Exception as e:
            results.append(f"  ERROR cleaning bonus-only release '{title[:60]}': {e}")

    if not results:
        return f"{name}: nothing to clean (bonus-only releases)"
    return f"{name}: bonus-only releases:\n" + "\n".join(results)


TORRENT_STATE_PATH = "/tmp/arr-queue-cleaner-torrent-state.json"
# Remembers which "needs manual review" items have already been reported, so a
# genuinely unfixable file (e.g. a movie not in the library at all) is logged
# ONCE rather than re-spammed on every 30-minute run forever.
REVIEW_LEDGER_PATH = "/tmp/arr-queue-cleaner-review-ledger.json"
# When each download first started sitting in the "completed but no files
# eligible for import" state -- see item 9 in the module docstring.
PHANTOM_STATE_PATH = "/tmp/arr-queue-cleaner-phantom-state.json"
PHANTOM_MIN_AGE_SECONDS = 45 * 60


def load_torrent_state():
    return load_json_state(TORRENT_STATE_PATH)


def save_torrent_state(state):
    save_json_state(TORRENT_STATE_PATH, state)


def clean_dead_torrents():
    """A torrent used to be flagged dead from a single instantaneous
    snapshot (0 seeds AND 0 dlspeed at the exact moment of one 30-min
    check) -- but a torrent can show a brief nonzero seed/speed blip
    without ever making real progress, which let one real TV episode sit
    stalled 15+ hours across many checks completely undetected. Tracked
    now by actual bytes downloaded across two consecutive runs instead: a
    torrent in an active-but-not-progressing state that has downloaded
    zero additional bytes since the last check is dead, regardless of
    what its seeds/speed happened to read at either individual instant.
    First time a torrent is seen this way it just gets a baseline
    recorded, not flagged -- detection latency becomes one extra cycle,
    trading a little speed for actually being reliable."""
    cookie = qbit_login()
    torrents = qbit_get("/api/v2/torrents/info", cookie)
    now = time.time()

    prev_state = load_torrent_state()
    next_state = {}
    dead = []

    for t in torrents:
        if t.get("state") not in DEAD_STATES:
            continue
        if (t.get("progress") or 0) >= 1.0:
            continue  # already finished -- never treat a completed torrent as dead
        age = now - t.get("added_on", now)
        if age < DEAD_MIN_AGE_SECONDS:
            continue

        h = t["hash"]
        downloaded = t.get("downloaded", 0)
        prev = prev_state.get(h)

        if prev is not None and prev.get("downloaded") == downloaded:
            dead.append(t)
        else:
            next_state[h] = {"downloaded": downloaded}

    save_torrent_state(next_state)

    if not dead:
        return "Dead torrents: nothing to clean"

    results = []
    for t in dead:
        target_hash = t["hash"].upper()
        matched = False
        for name, base, key in INSTANCES:
            try:
                data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000")
            except Exception as e:
                results.append(f"  ERROR querying {name} queue: {e}")
                continue
            rows = [r for r in data.get("records", []) if r.get("downloadId") == target_hash]
            if not rows:
                continue
            matched = True
            ids = [r["id"] for r in rows]
            entity_id = rows[0].get("seriesId") if name == "Sonarr" else rows[0].get("movieId")
            try:
                delete_bulk(base, key, ids)
                trigger_search(base, key, name, entity_id)
                t_age_min = int((now - t.get("added_on", now)) / 60)
                results.append(
                    f"  DEAD: '{t['name'][:70]}' (no bytes downloaded across 2+ checks, state={t['state']}, age={t_age_min}m) "
                    f"-> removed {len(ids)} {name} queue row(s), blocklisted, re-search triggered"
                )
            except Exception as e:
                results.append(f"  ERROR cleaning '{t['name'][:70]}' via {name}: {e}")
        if not matched:
            results.append(
                f"  DEAD (no arr match, removing from qBittorrent directly): '{t['name'][:70]}'"
            )
            try:
                req = urllib.request.Request(
                    f"{QBIT_BASE}/api/v2/torrents/delete",
                    data=urllib.parse.urlencode(
                        {"hashes": t["hash"], "deleteFiles": "true"}
                    ).encode(),
                    method="POST",
                    headers={"Cookie": cookie},
                )
                urllib.request.urlopen(req, timeout=30)
            except Exception as e:
                results.append(f"    ERROR removing from qBittorrent: {e}")

    return "Dead torrents:\n" + "\n".join(results)


def clean_phantom_complete_downloads():
    """See item 9 in the module docstring above. Forces a qBittorrent recheck
    on downloads that have been stuck 'completed but nothing importable' for
    45+ minutes while qBittorrent still claims 100%, then gets out of the
    way -- clean_dead_torrents picks up the pieces on a later run if the
    recheck exposes it as fake."""
    cookie = qbit_login()
    state = load_json_state(PHANTOM_STATE_PATH)
    now = time.time()
    new_state = {}
    results = []

    for name, base, key in INSTANCES:
        try:
            data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000")
        except Exception as e:
            results.append(f"{name}: ERROR listing queue for phantom-completion check: {e}")
            continue

        for r in data.get("records", []):
            if r.get("status") != "completed" or r.get("trackedDownloadStatus") != "warning":
                continue
            download_id = (r.get("downloadId") or "").lower()
            if not download_id:
                continue

            state_key = f"{name}:{download_id}"
            first_seen = state.get(state_key, now)
            elapsed = now - first_seen

            if elapsed < PHANTOM_MIN_AGE_SECONDS:
                new_state[state_key] = first_seen  # keep tracking, not old enough to act on yet
                continue

            try:
                torrents = qbit_get(f"/api/v2/torrents/info?hashes={download_id}", cookie)
            except Exception as e:
                results.append(f"{name}: ERROR checking qBittorrent for '{r.get('title', '')[:50]}': {e}")
                new_state[state_key] = first_seen
                continue
            if not torrents or (torrents[0].get("progress") or 0) < 1.0:
                continue  # genuinely still downloading (or gone) -- not our phantom case, drop from tracking

            try:
                req = urllib.request.Request(
                    f"{QBIT_BASE}/api/v2/torrents/recheck",
                    data=urllib.parse.urlencode({"hashes": download_id}).encode(),
                    method="POST", headers={"Cookie": cookie},
                )
                urllib.request.urlopen(req, timeout=30)
                results.append(
                    f"{name}: '{r.get('title', '')[:65]}' stuck import-pending for "
                    f"{int(elapsed / 60)}min despite qBittorrent showing 100% complete -- "
                    f"forced a recheck to verify what's actually on disk"
                )
                # don't re-add to new_state -- give it a fresh 45min window post-recheck
                # rather than immediately re-triggering next run
            except Exception as e:
                results.append(f"{name}: ERROR forcing recheck for '{r.get('title', '')[:50]}': {e}")
                new_state[state_key] = first_seen

    save_json_state(PHANTOM_STATE_PATH, new_state)

    if not results:
        return "Phantom-completion check: nothing stuck"
    return "Phantom-completion check:\n" + "\n".join(results)


ORPHAN_MIN_AGE_DAYS = 3
ORPHAN_LEDGER_PATH = "/tmp/arr-queue-cleaner-orphan-ledger.json"


def check_orphaned_additions():
    """See item 10 in the module docstring above."""
    results = []
    ledger = load_json_state(ORPHAN_LEDGER_PATH)
    new_ledger = {}
    now_utc = datetime.now(timezone.utc)

    sonarr_name, sonarr_base, sonarr_key = SONARR
    try:
        series_list = get(f"{sonarr_base}/api/v3/series?apikey={sonarr_key}")
    except Exception as e:
        results.append(f"Sonarr: ERROR listing series for orphan check: {e}")
        series_list = []

    for s in series_list:
        if not s.get("monitored"):
            continue
        stats = s.get("statistics", {})
        if stats.get("episodeFileCount", 0) > 0:
            continue
        if stats.get("episodeCount", 0) == 0:
            continue  # nothing has aired yet -- a genuinely new show, not an orphan
        try:
            added = datetime.fromisoformat(s.get("added", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = (now_utc - added).total_seconds() / 86400
        if age_days < ORPHAN_MIN_AGE_DAYS:
            continue
        try:
            history = get(f"{sonarr_base}/api/v3/history/series?seriesId={s['id']}&apikey={sonarr_key}")
        except Exception as e:
            results.append(f"Sonarr: ERROR checking history for '{s['title']}': {e}")
            continue
        if history:
            continue  # has SOME history -- a real availability gap, not this bug class

        key_id = f"sonarr:{s['id']}"
        new_ledger[key_id] = True
        if key_id not in ledger:
            results.append(
                f"Sonarr: ORPHANED SERIES -- '{s['title']}' added {age_days:.0f}d ago, monitored, "
                f"{stats.get('episodeCount')} aired episode(s), but ZERO grabs/imports ever "
                f"(seriesType={s.get('seriesType')}, rootFolder={s.get('rootFolderPath')}, "
                f"qualityProfileId={s.get('qualityProfileId')}) -- check seriesType/root folder/quality "
                f"profile are correct for this show, then trigger a manual search"
            )

    radarr_name, radarr_base, radarr_key = RADARR
    try:
        movies = radarr_movies(radarr_base, radarr_key)
    except Exception as e:
        results.append(f"Radarr: ERROR listing movies for orphan check: {e}")
        movies = []

    for m in movies:
        if not m.get("monitored") or m.get("hasFile"):
            continue
        if not m.get("isAvailable"):
            continue  # not released/available yet -- not an orphan
        try:
            added = datetime.fromisoformat(m.get("added", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = (now_utc - added).total_seconds() / 86400
        if age_days < ORPHAN_MIN_AGE_DAYS:
            continue
        try:
            history = get(f"{radarr_base}/api/v3/history/movie?movieId={m['id']}&apikey={radarr_key}")
        except Exception as e:
            results.append(f"Radarr: ERROR checking history for '{m['title']}': {e}")
            continue
        if history:
            continue

        key_id = f"radarr:{m['id']}"
        new_ledger[key_id] = True
        if key_id not in ledger:
            results.append(
                f"Radarr: ORPHANED MOVIE -- '{m['title']}' added {age_days:.0f}d ago, monitored, "
                f"available, but ZERO grabs/imports ever (rootFolder={m.get('rootFolderPath')}, "
                f"qualityProfileId={m.get('qualityProfileId')}) -- check root folder/quality profile, "
                f"then trigger a manual search"
            )

    save_json_state(ORPHAN_LEDGER_PATH, new_ledger)

    if not results:
        return "Orphaned additions: nothing found"
    return "Orphaned additions:\n" + "\n".join(results)


EXECUTABLE_EXTENSIONS = (".exe", ".scr", ".bat", ".cmd", ".com", ".msi")


def clean_malicious_executables():
    """Detects releases where the entire downloaded item is a single executable
    file rather than a real video (outputPath itself ends in an executable
    extension) -- a disguised-malware pattern, not a legitimate release
    regardless of file size. Hit live with a TV episode padded out to a bare
    .exe (1.2GB, a plausible size for a real episode, but TV/movie releases
    are never shipped as a bare .exe -- always a direct .mkv/.mp4 or a folder
    of them).
    Deletes the file from disk directly via qBittorrent (deleteFiles=true,
    not just dequeues it), blocklists the release, and triggers a fresh
    search. Deliberately narrow: only fires when outputPath itself is the
    executable. A legitimate video folder that merely contains an incidental
    stray exe alongside real video files is a different, lower-confidence
    case and is left alone -- this never inspects or runs the file itself,
    only its path."""
    cookie = qbit_login()
    results = []

    for name, base, key in INSTANCES:
        try:
            data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000")
        except Exception as e:
            results.append(f"{name}: ERROR querying queue for executables: {e}")
            continue

        for r in data.get("records", []):
            output_path = (r.get("outputPath") or "").lower()
            if not output_path.endswith(EXECUTABLE_EXTENSIONS):
                continue

            title = r.get("title", "")
            target_hash = r.get("downloadId")

            if target_hash:
                try:
                    req = urllib.request.Request(
                        f"{QBIT_BASE}/api/v2/torrents/delete",
                        data=urllib.parse.urlencode(
                            {"hashes": target_hash, "deleteFiles": "true"}
                        ).encode(),
                        method="POST",
                        headers={"Cookie": cookie},
                    )
                    urllib.request.urlopen(req, timeout=30)
                except Exception as e:
                    results.append(f"{name}: ERROR deleting file from disk for '{title[:60]}': {e}")
                    continue

            try:
                url = (
                    f"{base}/api/v3/queue/bulk?removeFromClient=false"
                    f"&blocklist=true&skipRedownload=true&apikey={key}"
                )
                req = urllib.request.Request(
                    url, data=json.dumps({"ids": [r["id"]]}).encode(), method="DELETE",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=30)
            except Exception as e:
                results.append(f"{name}: ERROR blocklisting '{title[:60]}': {e}")
                continue

            entity_id = r.get("seriesId") if name == "Sonarr" else r.get("movieId")
            try:
                trigger_search(base, key, name, entity_id)
            except Exception as e:
                results.append(f"{name}: ERROR triggering re-search for '{title[:60]}': {e}")

            results.append(
                f"{name}: SECURITY -- deleted disguised-executable release '{title[:70]}' "
                f"(outputPath was a bare executable, not a real video), blocklisted, re-search triggered"
            )

    if not results:
        return "Malicious executables: nothing found"
    return "Malicious executables:\n" + "\n".join(results)


def clean_redundant_downloads():
    """A torrent can keep actively downloading (real seeds, real speed) for a
    series/movie Sonarr/Radarr already has every file for -- usually a
    leftover duplicate grab from an earlier batch search. clean_dead_torrents
    never catches these since they aren't dead, they're just pointless; left
    alone they'd occupy a download slot for their entire runtime doing
    nothing useful (hit live with two different completed shows both fully
    complete but still churning a duplicate download).
    skipRedownload=true throughout via delete_bulk, and deliberately no
    trigger_search call afterward -- the content is already satisfied, so a
    fresh search would be wrong, not just unnecessary."""
    results = []

    for name, base, key in INSTANCES:
        try:
            data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000&includeSeries=true&includeMovie=true")
        except Exception as e:
            results.append(f"{name}: ERROR querying queue for redundancy check: {e}")
            continue

        by_download = {}
        for r in data.get("records", []):
            if r.get("status") not in ("downloading", "queued"):
                continue
            if not r.get("downloadId"):
                continue
            by_download.setdefault(r["downloadId"], []).append(r)

        episode_hasfile_cache = {}  # seriesId -> {episodeId: hasFile}

        for rows in by_download.values():
            r0 = rows[0]
            title = r0.get("title", "")

            if name == "Sonarr":
                series_id = r0.get("seriesId")
                if series_id not in episode_hasfile_cache:
                    try:
                        eps = get(f"{base}/api/v3/episode?seriesId={series_id}&apikey={key}")
                        episode_hasfile_cache[series_id] = {e["id"]: e.get("hasFile") for e in eps}
                    except Exception:
                        episode_hasfile_cache[series_id] = {}
                hasfile_map = episode_hasfile_cache[series_id]
                episode_ids = {r.get("episodeId") for r in rows if r.get("episodeId")}
                if not episode_ids:
                    continue
                all_satisfied = all(hasfile_map.get(eid) for eid in episode_ids)
            else:  # Radarr
                movie = r0.get("movie") or {}
                all_satisfied = bool(movie.get("hasFile"))

            if not all_satisfied:
                continue

            try:
                ids = [r["id"] for r in rows]
                delete_bulk(base, key, ids)
                results.append(
                    f"{name}: REDUNDANT -- removed '{title[:70]}' "
                    f"(already fully satisfied, was occupying a download slot for nothing)"
                )
            except Exception as e:
                results.append(f"{name}: ERROR removing redundant '{title[:60]}': {e}")

    if not results:
        return "Redundant downloads: nothing to clean"
    return "Redundant downloads:\n" + "\n".join(results)


DISK_MIN_FREE_GB = 15


def check_disk_space():
    """A full disk is one of the quietest ways an unattended media server
    stops working -- torrents stall mid-write, imports fail, and nothing
    else in this script would ever explain why. Checked via qBittorrent's
    own free_space_on_disk (the space backing its actual save path) rather
    than a local filesystem check, since this script runs on the Proxmox
    host while the real files live inside the LXC -- asking qBittorrent
    sidesteps that filesystem-boundary mismatch entirely. Only warns; never
    attempts to free space itself, since deciding what's safe to delete
    needs human judgement."""
    try:
        cookie = qbit_login()
        data = qbit_get("/api/v2/sync/maindata", cookie)
        free_bytes = data.get("server_state", {}).get("free_space_on_disk")
        if free_bytes is None:
            return "Disk space: couldn't read free_space_on_disk from qBittorrent"
        free_gb = free_bytes / (1024 ** 3)
    except Exception as e:
        return f"Disk space: ERROR checking via qBittorrent: {e}"

    if free_gb < DISK_MIN_FREE_GB:
        return (
            f"Disk space: WARNING -- only {free_gb:.1f}GB free "
            f"(threshold {DISK_MIN_FREE_GB}GB) -- new downloads may start failing"
        )
    return f"Disk space: {free_gb:.1f}GB free -- OK"


MALICIOUS_TITLE_PATTERN = re.compile(r"SECURITY -- deleted disguised-executable release '([^']+)'")
# A blocklist entry has to sit for at least this long before it's eligible to
# be recovered. Without this, the 10-minute health check's fast 0-seed-stall
# watchdog and this recovery check fight each other: the watchdog correctly
# blocklists a genuinely-dead release, this check un-blocklists it minutes
# later because the episode is still missing, Sonarr re-grabs the exact same
# still-dead release, the watchdog blocklists it again -- a real infinite
# loop caught live with a multi-episode batch, cycling grab/fail every 20-30
# minutes for hours. A release that recovered seeders needs real-world time
# to do so anyway, so losing a few hours of recovery latency costs nothing.
BLOCKLIST_RECOVERY_MIN_AGE_HOURS = 24


def clean_recovered_blocklist_entries():
    """See item 8 in the module docstring above. Removes blocklist entries
    for content that's still missing a file, so a release that was
    blocklisted while genuinely dead gets a fair re-evaluation once real
    seeders show up -- while anything logged here as SECURITY stays
    permanently blocked no matter what, matched by title against this
    script's own log."""
    try:
        with open(SELF_LOG_PATH) as f:
            log_text = f.read()
    except Exception:
        log_text = ""
    malicious_titles = MALICIOUS_TITLE_PATTERN.findall(log_text)

    def is_malicious(source_title):
        return any(source_title.startswith(t) or t.startswith(source_title) for t in malicious_titles)

    results = []

    for name, base, key in INSTANCES:
        try:
            entries = []
            page = 1
            while True:
                data = get(f"{base}/api/v3/blocklist?apikey={key}&page={page}&pageSize=200&sortKey=date&sortDirection=descending")
                entries.extend(data.get("records", []))
                if len(entries) >= data.get("totalRecords", 0) or not data.get("records"):
                    break
                page += 1
        except Exception as e:
            results.append(f"{name}: ERROR listing blocklist: {e}")
            continue

        if not entries:
            continue

        if name == "Sonarr":
            try:
                still_missing_ids = set()
                page = 1
                while True:
                    data = get(f"{base}/api/v3/wanted/missing?apikey={key}&monitored=true&page={page}&pageSize=250")
                    still_missing_ids.update(r["id"] for r in data.get("records", []))
                    if len(still_missing_ids) >= data.get("totalRecords", 0) or not data.get("records"):
                        break
                    page += 1
            except Exception as e:
                results.append(f"{name}: ERROR loading missing episodes for blocklist review: {e}")
                continue
        else:  # Radarr
            try:
                missing_movie_ids = {m["id"] for m in radarr_movies(base, key) if not m.get("hasFile")}
            except Exception as e:
                results.append(f"{name}: ERROR loading movie library for blocklist review: {e}")
                continue

        now_utc = datetime.now(timezone.utc)
        removed_titles = []
        for entry in entries:
            source_title = entry.get("sourceTitle", "")
            if is_malicious(source_title):
                continue  # permanent, never reconsidered

            try:
                blocked_at = datetime.fromisoformat(entry.get("date", "").replace("Z", "+00:00"))
                age_hours = (now_utc - blocked_at).total_seconds() / 3600
            except ValueError:
                age_hours = 0  # no parseable date -- treat as too fresh to touch, safest default

            if age_hours < BLOCKLIST_RECOVERY_MIN_AGE_HOURS:
                continue  # too recent -- avoid fighting a check that JUST blocklisted this for a real reason

            if name == "Sonarr":
                still_wanted = bool(set(entry.get("episodeIds", [])) & still_missing_ids)
            else:
                still_wanted = entry.get("movieId") in missing_movie_ids

            if not still_wanted:
                continue  # already satisfied by something else, or n/a -- leave as-is

            try:
                req = urllib.request.Request(
                    f"{base}/api/v3/blocklist/{entry['id']}?apikey={key}", method="DELETE",
                )
                urllib.request.urlopen(req, timeout=30)
                removed_titles.append(source_title[:70])
            except Exception as e:
                results.append(f"{name}: ERROR removing blocklist entry {entry['id']} ({source_title[:50]}): {e}")

        if removed_titles:
            results.append(
                f"{name}: un-blocklisted {len(removed_titles)} release(s) still wanted, eligible for "
                f"re-search -> {'; '.join(removed_titles[:5])}" + (" ..." if len(removed_titles) > 5 else "")
            )

    if not results:
        return "Blocklist review: nothing to recover"
    return "Blocklist review:\n" + "\n".join(results)


SEASON_EP_PATTERNS = [
    re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})'),        # S02E01
    re.compile(r'[Ss](\d{1,2})\s*[-_]\s*(\d{1,3})'),  # S2 - 01, S2-01
]

# Fallback for release groups that put the season only in the folder name and
# a bare episode marker in each file (e.g. "[Group] Some Anime Season Name
# S02 [1080p].../... - E10 [1080p]...mkv" -- hit live with one long-running
# anime series). Season and episode are combined from two different strings,
# so this only applies when the primary same-string SEASON_EP_PATTERNS above
# don't match.
SEASON_FROM_FOLDER_PATTERN = re.compile(r'\bS(\d{1,2})\b')
EPISODE_ONLY_PATTERN = re.compile(r'-\s*E(\d{1,3})\s*[\[\.]')

# Some BDRip/DVDRip groups spell the episode marker "Ep.03" / "Ep 03" rather
# than "E03", with season given elsewhere in the same filename as a bare
# "S01" not immediately followed by the episode number (e.g.
# "Some.Anime.Title.S01.2014.x264.BDRip.1080p.Group.Ep.03.mkv" -- hit live
# with one anime series, all 27 files stuck in manual review every run).
EPISODE_MARKER_PATTERN = re.compile(r'\bEp\.?\s*(\d{1,3})\b', re.IGNORECASE)

# Categories Sonarr/Radarr actively manage -- only these get scanned for
# orphaned completed downloads. Manually-categorised one-off pulls (e.g.
# regularshow-manual) are left alone; those get handled by hand as they come up.
RESCUE_CATEGORIES = {"Sonarr": "tv-sonarr", "Radarr": "movies-radarr"}


def manualimport_scan(base, key, folder):
    q = urllib.parse.quote(folder)
    return get(f"{base}/api/v3/manualimport?folder={q}&apikey={key}")


def already_imported(base, key, name, target_hash):
    """True if Sonarr/Radarr history shows this exact download was already imported."""
    data = get(f"{base}/api/v3/history?pageSize=200&apikey={key}")
    for r in data.get("records", []):
        if r.get("downloadId") == target_hash and r.get("eventType") == "downloadFolderImported":
            return True
    return False


def sonarr_episode_map(base, key, series_id):
    """(seasonNumber, episodeNumber) -> {"id": ..., "hasFile": ...} for one series."""
    data = get(f"{base}/api/v3/episode?seriesId={series_id}&apikey={key}")
    return {(e["seasonNumber"], e["episodeNumber"]): e for e in data}


def radarr_movies(base, key):
    return get(f"{base}/api/v3/movie?apikey={key}")


def title_tokens(s):
    """Normalise a release name or movie title down to comparable word tokens:
    lowercase, drop bracketed groups, drop 4-digit years, keep only
    alphanumerics. 'A.Quiet.Place.Part.II.BDRip.1080p.pk' and
    'A Quiet Place Part II' both reduce to their word lists, so the library
    title becomes a clean prefix of the messier release name."""
    s = re.sub(r"[\[(].*?[\])]", " ", s)          # drop [..] and (..) groups
    s = re.sub(r"[._]+", " ", s.lower())           # dots/underscores -> spaces
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)        # drop years (1900-2099)
    s = re.sub(r"[^a-z0-9 ]", " ", s)              # keep alnum + space only
    return s.split()


def match_missing_movie(download_name, movies):
    """Find the library movie a bare, sparsely-named download most likely
    belongs to, WITHOUT free-form identification: only movies the user has
    already added count, and a match requires the movie's full title to be a
    leading prefix of the download name's tokens. The most specific (longest)
    title wins, so 'A Quiet Place Part II ...' matches 'A Quiet Place Part II'
    rather than the shorter 'A Quiet Place'. Returns the best-matching movie
    objects (possibly several if titles tie); the caller then filters by which
    are actually missing a file."""
    dl = title_tokens(download_name)
    if not dl:
        return []
    matched = []  # (title_length, movie)
    for m in movies:
        lt = title_tokens(m.get("title", ""))
        if lt and dl[: len(lt)] == lt:
            matched.append((len(lt), m))
    if not matched:
        return []
    longest = max(n for n, _ in matched)
    return [m for n, m in matched if n == longest]


def submit_manual_import(base, key, files, import_mode="copy"):
    """Submits the import and waits (up to ~60s) for the async command to finish,
    so callers can tell whether it's safe to clear the matching queue entry."""
    body = json.dumps({"name": "ManualImport", "files": files, "importMode": import_mode}).encode()
    req = urllib.request.Request(
        f"{base}/api/v3/command?apikey={key}", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        command_id = json.loads(r.read())["id"]

    for _ in range(12):  # ~60s at 5s intervals
        time.sleep(5)
        status = get(f"{base}/api/v3/command/{command_id}?apikey={key}")
        if status.get("status") == "completed":
            return True
        if status.get("status") == "failed":
            return False
    return False  # timed out -- treat as not-confirmed-successful


def clear_stale_queue_entries(base, key, target_hash):
    """After a successful manual import via explicit episode/movie ID override,
    Sonarr/Radarr don't always clear the matching queue entry on their own (hit
    live with one anime season -- files fully imported, but the queue kept
    showing it stuck 'importPending' indefinitely). Clean those up directly so
    the queue doesn't keep reporting something as stuck that's actually done.
    removeFromClient=false since the download itself is untouched -- this only
    clears Sonarr/Radarr's own bookkeeping."""
    data = get(f"{base}/api/v3/queue?apikey={key}&pageSize=1000")
    ids = [r["id"] for r in data.get("records", []) if r.get("downloadId") == target_hash]
    if not ids:
        return
    url = (
        f"{base}/api/v3/queue/bulk?removeFromClient=false"
        f"&blocklist=false&skipRedownload=true&apikey={key}"
    )
    req = urllib.request.Request(
        url, data=json.dumps({"ids": ids}).encode(), method="DELETE",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)


def rescue_stuck_imports():
    cookie = qbit_login()
    results = []

    # Only report a given needs-review item the first time it's seen. The
    # ledger is rebuilt fresh each run from whatever still needs review now, so
    # an item that gets fixed or removed naturally drops out (and would be
    # re-reported if it ever came back), while a genuinely unfixable one stays
    # suppressed instead of re-spamming the log every 30 minutes.
    old_ledger = load_json_state(REVIEW_LEDGER_PATH)
    new_ledger = {}

    for name, base, key in INSTANCES:
        category = RESCUE_CATEGORIES[name]
        try:
            torrents = qbit_get(f"/api/v2/torrents/info?category={category}", cookie)
        except Exception as e:
            results.append(f"{name} rescue: ERROR listing qBittorrent torrents: {e}")
            continue

        movies_cache = None  # Radarr library, fetched lazily on first Unknown Movie

        for t in torrents:
            if (t.get("progress") or 0) < 1.0:
                continue  # not finished yet, nothing to import

            target_hash = t["hash"].upper()
            try:
                if already_imported(base, key, name, target_hash):
                    continue
            except Exception as e:
                results.append(f"{name} rescue: ERROR checking history for '{t['name'][:60]}': {e}")
                continue

            try:
                scan = manualimport_scan(base, key, t["content_path"])
            except Exception as e:
                results.append(f"{name} rescue: ERROR scanning '{t['name'][:60]}': {e}")
                continue

            if not scan:
                continue

            safe_files = []
            needs_review = []
            redundant = False  # matched only already-satisfied content -> safe to remove
            episode_cache = {}

            for item in scan:
                rejections = item.get("rejections", [])

                # Sonarr/Radarr's own manual-import scan already recognises
                # non-episode bonus content (NC-OP/ED clips, PVs, samples
                # bundled in a batch release) and tags it with a "Sample"
                # rejection regardless of filename. That's never importable
                # no matter what else about the release looks like, so trust
                # it and skip silently instead of re-flagging the same
                # never-resolvable file as NEEDS MANUAL REVIEW every run
                # forever (hit live with one anime season's bundled
                # NCED/NCOP clips sitting in an Extra/ folder).
                if any((r.get("reason", "") if isinstance(r, dict) else str(r)).strip().lower() == "sample" for r in rejections):
                    continue

                if name == "Sonarr":
                    episodes = item.get("episodes", [])
                    if not rejections and episodes:
                        if any(not e.get("hasFile") for e in episodes):
                            safe_files.append({
                                "path": item["path"],
                                "seriesId": item["series"]["id"],
                                "episodeIds": [e["id"] for e in episodes],
                                "quality": item["quality"],
                                "languages": item["languages"],
                                "releaseType": item.get("releaseType"),
                                "indexerFlags": item.get("indexerFlags", 0),
                            })
                        # else: already fully satisfied, nothing to do -- not a
                        # review case, just silently skip this file.
                        continue
                else:  # Radarr
                    if not rejections and item.get("movie"):
                        if not item["movie"].get("hasFile"):
                            safe_files.append({
                                "path": item["path"],
                                "movieId": item["movie"]["id"],
                                "quality": item["quality"],
                                "languages": item["languages"],
                                "indexerFlags": item.get("indexerFlags", 0),
                            })
                        continue

                    # "Unknown Movie": Radarr couldn't parse a movie identity
                    # from a bare, sparsely-named file. Rather than punt to
                    # manual review, match it against movies the user has
                    # already added -- bounded and safe, since we only ever
                    # fill a movie that's genuinely missing a file and only
                    # when the match is unambiguous.
                    if movies_cache is None:
                        try:
                            movies_cache = radarr_movies(base, key)
                        except Exception as e:
                            needs_review.append(f"{item.get('relativePath')}: {rejections} (ERROR loading movie library: {e})")
                            continue

                    best = match_missing_movie(item.get("relativePath") or item.get("name", ""), movies_cache)
                    missing = [m for m in best if not m.get("hasFile")]
                    have = [m for m in best if m.get("hasFile")]

                    if len(missing) == 1:
                        safe_files.append({
                            "path": item["path"],
                            "movieId": missing[0]["id"],
                            "quality": item["quality"],
                            "languages": item["languages"],
                            "indexerFlags": item.get("indexerFlags", 0),
                        })
                        continue
                    if not missing and have:
                        # the only library title this matches is already
                        # satisfied -> this download is a redundant duplicate.
                        redundant = True
                        continue
                    # zero matches (movie not in library) or several equally-good
                    # missing candidates (genuinely ambiguous) -> needs a human.
                    needs_review.append(f"{item.get('relativePath')}: {rejections}")
                    continue

                if not item.get("series"):
                    needs_review.append(f"{item.get('relativePath')}: {rejections}")
                    continue

                # Tier 2: try to correct a Sonarr season/episode misparse using
                # the filename itself, but only act if the regex-derived target
                # differs from Sonarr's guess AND is genuinely missing.
                fname = item.get("relativePath") or item.get("name", "")
                match = None
                for pattern in SEASON_EP_PATTERNS:
                    m = pattern.search(fname)
                    if m:
                        match = (int(m.group(1)), int(m.group(2)))
                        break

                if not match:
                    # season can be a bare "S01" in either the folder name or
                    # the filename itself, depending on the release group; try
                    # the filename first since it's the more specific source.
                    season_m = SEASON_FROM_FOLDER_PATTERN.search(fname) or SEASON_FROM_FOLDER_PATTERN.search(item.get("folderName", ""))
                    episode_m = EPISODE_ONLY_PATTERN.search(fname) or EPISODE_MARKER_PATTERN.search(fname)
                    if season_m and episode_m:
                        match = (int(season_m.group(1)), int(episode_m.group(1)))

                if not match:
                    needs_review.append(f"{fname}: {rejections} (no SxxEyy pattern found)")
                    continue

                series_id = item["series"]["id"]
                parsed = {(e["seasonNumber"], e["episodeNumber"]) for e in episodes}
                # Only trust "Sonarr already had it right" when its guess is a
                # single specific episode. A guess spanning many episodes (the
                # "single episode file contains all episodes in seasons" bug,
                # hit live with one long-running anime series) trivially
                # contains our regex match without meaning Sonarr actually
                # identified it -- fall through and let the regex-derived
                # target decide instead.
                if match in parsed and len(parsed) == 1:
                    # Sonarr already had it right; a genuine "not an upgrade" -- leave it.
                    needs_review.append(f"{fname}: {rejections} (Sonarr's parse matches filename, real rejection)")
                    continue

                if series_id not in episode_cache:
                    try:
                        episode_cache[series_id] = sonarr_episode_map(base, key, series_id)
                    except Exception as e:
                        needs_review.append(f"{fname}: ERROR fetching episode list: {e}")
                        continue

                target = episode_cache[series_id].get(match)
                if not target:
                    # If the parsed episode number runs past the last real
                    # episode of that season, it's almost certainly an OVA /
                    # special where the release used one continuous numbering
                    # that spills past the TV run (hit live with a 24-episode
                    # anime season where Ep.25/26/27 were OVAs living under
                    # Sonarr's unmonitored specials). Those aren't wanted, so
                    # skip silently rather than flagging manual review forever.
                    # A genuinely missing regular episode would fall within the
                    # season's known episode count, so this can't swallow one.
                    season_eps = [ep for (s, ep) in episode_cache[series_id] if s == match[0]]
                    if season_eps and match[1] > max(season_eps):
                        continue
                    needs_review.append(f"{fname}: regex found S{match[0]}E{match[1]} but no such episode exists")
                    continue
                if target.get("hasFile"):
                    continue  # already imported, nothing to do -- not a review case

                safe_files.append({
                    "path": item["path"],
                    "seriesId": series_id,
                    "episodeIds": [target["id"]],
                    "quality": item["quality"],
                    "languages": item["languages"],
                    "releaseType": item.get("releaseType"),
                    "indexerFlags": item.get("indexerFlags", 0),
                })

            if safe_files:
                try:
                    confirmed = submit_manual_import(base, key, safe_files)
                    if confirmed:
                        try:
                            clear_stale_queue_entries(base, key, target_hash)
                        except Exception as e:
                            results.append(f"{name} rescue: import OK but ERROR clearing stale queue entries: {e}")
                        results.append(
                            f"{name} rescue: imported {len(safe_files)} file(s) from orphaned completed "
                            f"download '{t['name'][:70]}'"
                        )
                    else:
                        results.append(
                            f"{name} rescue: submitted import for '{t['name'][:70]}' but it didn't confirm "
                            f"completed within 60s -- leaving queue alone, check manually"
                        )
                except Exception as e:
                    results.append(f"{name} rescue: ERROR submitting import for '{t['name'][:60]}': {e}")

            # A finished download that matched only already-satisfied content
            # (and produced nothing to import or review) is a pure duplicate --
            # remove it so it stops being rescanned every run and stops wasting
            # disk. Only fires when we're certain: safe_files/needs_review both
            # empty and at least one file matched a movie that already has its
            # file.
            if redundant and not safe_files and not needs_review:
                try:
                    req = urllib.request.Request(
                        f"{QBIT_BASE}/api/v2/torrents/delete",
                        data=urllib.parse.urlencode({"hashes": t["hash"], "deleteFiles": "true"}).encode(),
                        method="POST", headers={"Cookie": cookie},
                    )
                    urllib.request.urlopen(req, timeout=30)
                    results.append(
                        f"{name} rescue: REDUNDANT -- removed completed duplicate "
                        f"'{t['name'][:70]}' (library already has this content)"
                    )
                except Exception as e:
                    results.append(f"{name} rescue: ERROR removing redundant '{t['name'][:60]}': {e}")

            if needs_review:
                new_ledger[target_hash] = needs_review
                already = set(old_ledger.get(target_hash, []))
                fresh = [r for r in needs_review if r not in already]
                if fresh:
                    results.append(
                        f"{name} rescue: NEEDS MANUAL REVIEW in '{t['name'][:60]}':\n    "
                        + "\n    ".join(fresh)
                    )

    save_json_state(REVIEW_LEDGER_PATH, new_ledger)

    if not results:
        return "Stuck imports: nothing to rescue"
    return "Stuck imports:\n" + "\n".join(results)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} === run ===")

    missing = config.missing_required()
    if missing:
        print(f"WARNING: missing required environment variables: {', '.join(missing)} -- see .env.example")

    for name, base, key in INSTANCES:
        try:
            print(clean_not_an_upgrade(name, base, key))
        except Exception as e:
            print(f"{name}: ERROR: {e}")
        try:
            print(clean_bonus_only_releases(name, base, key))
        except Exception as e:
            print(f"{name}: ERROR (bonus-only releases): {e}")

    try:
        print(clean_dead_torrents())
    except Exception as e:
        print(f"Dead torrents: ERROR: {e}")

    try:
        print(clean_phantom_complete_downloads())
    except Exception as e:
        print(f"Phantom-completion check: ERROR: {e}")

    try:
        print(clean_malicious_executables())
    except Exception as e:
        print(f"Malicious executables: ERROR: {e}")

    try:
        print(clean_redundant_downloads())
    except Exception as e:
        print(f"Redundant downloads: ERROR: {e}")

    try:
        print(rescue_stuck_imports())
    except Exception as e:
        print(f"Stuck imports: ERROR: {e}")

    try:
        print(clean_recovered_blocklist_entries())
    except Exception as e:
        print(f"Blocklist review: ERROR: {e}")

    try:
        print(check_disk_space())
    except Exception as e:
        print(f"Disk space: ERROR: {e}")

    try:
        print(check_orphaned_additions())
    except Exception as e:
        print(f"Orphaned additions: ERROR: {e}")


if __name__ == "__main__":
    main()
