#!/usr/bin/env python3
# 10-minute health check, complementary to arr-queue-cleaner.py's 30-minute
# cleanup pass. Silent when nothing is wrong -- only writes a log entry (which
# then shows up in the existing dashboard History page, since it shares
# arr-queue-cleaner.log's exact "=== run ===" header format) when something
# actually needs attention.
#
# 1) qBittorrent 0-seed stall: a tv-sonarr torrent sitting in metaDL/stalledDL
#    with 0 seeds for 20+ minutes (state persisted by hash, first-seen
#    timestamp) is dead weight -- blocklist the matching Sonarr queue entry
#    and fire a fresh episode/series search. This is a faster, seed-count-
#    based companion to arr-queue-cleaner's 30-minute byte-progress dead-
#    torrent check; seed count can hit zero and stay there well before a
#    30-minute cycle would catch it.
# 2) Transfer speed bottleneck: qBittorrent reports active downloads but total
#    speed stays near-zero across two consecutive checks (20 min) -- points to
#    something wrong beyond any single stuck torrent (throttling, connectivity).
# 3) Sonarr/Radarr command queue freeze: a command stuck in "started" with the
#    exact same status message across 5+ minutes is genuinely frozen, not just
#    working through a long list -- legitimate multi-episode/movie searches
#    update their message every request as they move to the next item. Watched
#    independently for both services, so a wedge in one doesn't need the other
#    to also be stuck before it's caught.

import json
import re
import subprocess
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from arr_common import config
from arr_common.config import SONARR_URL, SONARR_API_KEY, RADARR_URL, RADARR_API_KEY, QBIT_BASE
from arr_common.qbittorrent import login as qbit_login
from arr_common.qbittorrent import get as qbit_get
from arr_common.state import load_json_state, save_json_state

SONARR = SONARR_URL
SONARR_KEY = SONARR_API_KEY

STATE_FILE = "/tmp/arr-health-check-state.json"
LOG_PATH = "/var/log/arr-queue-cleaner.log"

STALL_MIN_AGE_SECONDS = 20 * 60
LOW_SPEED_THRESHOLD_BYTES = 50 * 1024  # 50 KB/s
LOW_SPEED_MIN_AGE_SECONDS = 20 * 60
STUCK_COMMAND_MIN_AGE_SECONDS = 5 * 60
# A search command stuck on the exact same message this long isn't slow, it's
# wedged on a hung network call to an indexer that never returned (seen live: a
# single search stuck ~2 hours behind a transient indexer blip, blocking every
# other queued search). Sonarr's API can't cancel these, so the only real fix
# is restarting the container -- which is safe (downloads live in qBittorrent)
# and clears the jam.
STUCK_COMMAND_RESTART_SECONDS = 30 * 60
# A command can also monopolise one of Sonarr's few execution slots for a very
# long time WITHOUT ever freezing on one identical message -- seen live: a
# SeriesSearch on one long-running anime series that kept advancing one
# episode at a time (so the message genuinely kept changing, never tripping
# the freeze check above) but took
# over 2 hours total, apparently hanging hard on a handful of individual
# episodes along the way. The whole time, it starved 24 other queued commands
# (RSS sync, import list sync, housekeeping) that never got a turn. Measured
# independently via Sonarr's own started timestamp, not our own tracking, so
# it catches this even though the message never repeats identically. Set well
# above the freeze threshold so a large-but-healthy search (many missing
# episodes, each taking real seconds) has plenty of room before this fires.
TOTAL_COMMAND_AGE_RESTART_SECONDS = 60 * 60
# Never restart a service more than once per this window, so a command that
# keeps wedging can't turn into a restart loop -- it gets one automatic
# attempt, then is left logged for a human if it's somehow still stuck an
# hour later. Shared by both Sonarr and Radarr, tracked separately per service.
RESTART_COOLDOWN_SECONDS = 60 * 60

# (service label, base URL, API key, docker container name) for the generic
# stuck-command watchdog below.
ARR_INSTANCES = [
    ("Sonarr", SONARR_URL, SONARR_API_KEY, "sonarr"),
    ("Radarr", RADARR_URL, RADARR_API_KEY, "radarr"),
]

APPS = (("Sonarr", SONARR_URL, SONARR_API_KEY), ("Radarr", RADARR_URL, RADARR_API_KEY))


def sonarr_get(path):
    req = urllib.request.Request(f"{SONARR}{path}", headers={"X-Api-Key": SONARR_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sonarr_post(path, body):
    req = urllib.request.Request(
        f"{SONARR}{path}",
        data=json.dumps(body).encode(),
        headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sonarr_delete_queue(queue_id):
    url = (
        f"{SONARR}/api/v3/queue/{queue_id}?removeFromClient=true"
        f"&blocklist=true&skipRedownload=true"
    )
    req = urllib.request.Request(url, method="DELETE", headers={"X-Api-Key": SONARR_KEY})
    urllib.request.urlopen(req, timeout=30)


def check_stalled_torrents(state, now):
    cookie = qbit_login()
    torrents = qbit_get("/api/v2/torrents/info", cookie)
    queue = sonarr_get("/api/v3/queue?pageSize=1000").get("records", [])
    queue_by_hash = {r["downloadId"].lower(): r for r in queue if r.get("downloadId")}

    prev = state.get("stalled_torrents", {})
    current = {}
    results = []

    for t in torrents:
        if t.get("category") != "tv-sonarr":
            continue
        # "stoppedDL" included alongside the active-looking states: found live
        # that genuinely dead (0 seeders confirmed by the tracker itself, not
        # just our own view) torrents can sit stopped rather than actively
        # retrying, and would otherwise never be caught by this check at all.
        if t.get("state") not in ("metaDL", "stalledDL", "stoppedDL"):
            continue
        if t.get("num_seeds", 0) != 0:
            continue

        h = t["hash"].lower()
        first_seen = prev.get(h, {}).get("first_seen", now)
        elapsed = now - first_seen

        if elapsed < STALL_MIN_AGE_SECONDS:
            current[h] = {"first_seen": first_seen, "name": t["name"]}
            continue

        qrec = queue_by_hash.get(h)
        if qrec is None:
            current[h] = {"first_seen": first_seen, "name": t["name"]}
            results.append(f"0-seed stall (no matching Sonarr queue entry, left alone): {t['name'][:70]}")
            continue

        try:
            sonarr_delete_queue(qrec["id"])
            episode_id = qrec.get("episodeId")
            if episode_id:
                sonarr_post("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [episode_id]})
            else:
                sonarr_post("/api/v3/command", {"name": "SeriesSearch", "seriesId": qrec["seriesId"]})
            results.append(f"0-seed stall {int(elapsed / 60)}min -> blocklisted + re-searched: {t['name'][:70]}")
            # resolved -- don't carry forward, entry is gone from qBit/queue now
        except Exception as e:
            current[h] = {"first_seen": first_seen, "name": t["name"]}
            results.append(f"0-seed stall {int(elapsed / 60)}min -> ERROR blocklisting: {e}: {t['name'][:70]}")

    state["stalled_torrents"] = current
    return results


def check_transfer_speed(state, now):
    cookie = qbit_login()
    info = qbit_get("/api/v2/transfer/info", cookie)
    torrents = qbit_get("/api/v2/torrents/info", cookie)
    active = sum(1 for t in torrents if t.get("state") == "downloading")
    speed = info.get("dl_info_speed", 0)

    if active == 0 or speed >= LOW_SPEED_THRESHOLD_BYTES:
        state["low_speed_since"] = None
        return []

    since = state.get("low_speed_since")
    if since is None:
        state["low_speed_since"] = now
        return []

    elapsed = now - since
    if elapsed >= LOW_SPEED_MIN_AGE_SECONDS:
        return [
            f"Transfer speed bottleneck: {active} active downloads but only "
            f"{speed / 1024:.1f} KB/s total, for {int(elapsed / 60)}min"
        ]
    return []


def check_stuck_commands_for(state, now, service_name, base, key, container_name):
    """Watches one Sonarr/Radarr instance's command queue for either way it can
    wedge, and auto-restarts its container to clear the jam. State is kept
    per-service (state[f"{container_name}_stuck_commands"], etc.) so Sonarr and
    Radarr are tracked independently -- one wedging never masks or restarts
    the other."""
    req = urllib.request.Request(f"{base}/api/v3/command", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        commands = json.loads(r.read())

    stuck_key = f"{container_name}_stuck_commands"
    restart_key = f"{container_name}_last_restart"

    prev = state.get(stuck_key, {})
    current = {}
    results = []
    now_utc = datetime.now(timezone.utc)
    oldest_running_seconds = 0
    oldest_running_desc = ""

    for c in commands:
        if c.get("status") != "started":
            continue
        cid = str(c["id"])
        message = c.get("message", "")
        prev_entry = prev.get(cid)

        if prev_entry and prev_entry.get("message") == message:
            first_seen = prev_entry["first_seen"]
            current[cid] = {"message": message, "first_seen": first_seen}
            elapsed = now - first_seen
            if elapsed >= STUCK_COMMAND_MIN_AGE_SECONDS:
                results.append(
                    f"{service_name} command queue frozen: '{c.get('commandName')}' (id {cid}) "
                    f"unchanged for {int(elapsed / 60)}min: {message}"
                )
        else:
            current[cid] = {"message": message, "first_seen": now}

        started_on = c.get("started") or c.get("queued")
        if started_on:
            try:
                started_dt = datetime.fromisoformat(started_on.replace("Z", "+00:00"))
                age = (now_utc - started_dt).total_seconds()
                if age > oldest_running_seconds:
                    oldest_running_seconds = age
                    oldest_running_desc = f"'{c.get('commandName')}': {message}"
            except ValueError:
                pass

    state[stuck_key] = current

    # Auto-remediate two distinct ways a command can wedge the queue:
    # (a) truly frozen -- identical message for STUCK_COMMAND_RESTART_SECONDS,
    #     a hung network call that will never resolve on its own.
    # (b) monopolising a slot for TOTAL_COMMAND_AGE_RESTART_SECONDS even while
    #     technically still progressing (message keeps changing), starving
    #     everything queued behind it just as badly as a true freeze.
    # Neither service's API can cancel either case, so a container restart is
    # the fix for both. Guarded by a per-service cooldown so it can't turn
    # into a restart loop.
    worst_frozen = max((now - v["first_seen"] for v in current.values()), default=0)
    trigger_reason = None
    if worst_frozen >= STUCK_COMMAND_RESTART_SECONDS:
        trigger_reason = f"frozen on one message for {int(worst_frozen / 60)}min"
    elif oldest_running_seconds >= TOTAL_COMMAND_AGE_RESTART_SECONDS:
        trigger_reason = f"running {int(oldest_running_seconds / 60)}min total ({oldest_running_desc}), starving the rest of the queue"

    if trigger_reason:
        last_restart = state.get(restart_key, 0)
        if now - last_restart >= RESTART_COOLDOWN_SECONDS:
            try:
                subprocess.run(
                    ["/usr/sbin/pct", "exec", "100", "--", "docker", "restart", container_name],
                    capture_output=True, text=True, timeout=120,
                )
                state[restart_key] = now
                state[stuck_key] = {}  # queue is cleared by the restart
                results.append(f"{service_name} command queue: {trigger_reason} -> restarted {service_name} to clear it")
            except Exception as e:
                results.append(f"{service_name} command queue: {trigger_reason} -> ERROR restarting {service_name}: {e}")
        else:
            results.append(
                f"{service_name} command queue: {trigger_reason}, but a {service_name} restart happened "
                f"{int((now - last_restart) / 60)}min ago (<{RESTART_COOLDOWN_SECONDS // 60}min cooldown) "
                f"-- leaving for manual review to avoid a restart loop"
            )

    return results


def check_stuck_commands(state, now):
    results = []
    for service_name, base, key, container_name in ARR_INSTANCES:
        try:
            results.extend(check_stuck_commands_for(state, now, service_name, base, key, container_name))
        except Exception as e:
            results.append(f"{service_name} command check: ERROR: {e}")
    return results


def _container_started_at(container):
    """UTC start time of a docker container in LXC 100, to second resolution.
    Docker reports RFC3339Nano (e.g. 2026-07-07T12:47:00.123681833Z); the first
    19 chars are YYYY-MM-DDTHH:MM:SS, which is all we need and sidesteps the
    variable fractional-digit parsing that would break a naive string compare."""
    out = subprocess.run(
        ["/usr/sbin/pct", "exec", "100", "--", "docker", "inspect",
         "-f", "{{.State.StartedAt}}", container],
        capture_output=True, text=True, timeout=60,
    )
    raw = out.stdout.strip()
    return datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S") if len(raw) >= 19 else None


def check_vpn_orphan():
    """qBittorrent shares the gluetun VPN container's network namespace
    (network_mode: service:gluetun). If gluetun restarts on its own -- VPN
    re-init, image update, crash -- qBittorrent keeps running but is stranded
    in the old, now-dead namespace: it looks "up" yet has no working network
    and its WebUI/port stop responding, so downloads silently stall until
    someone runs `docker restart qbittorrent` by hand. Detect that exact case
    (gluetun started strictly later than qBittorrent) and restart qBittorrent
    so it rejoins the live tunnel. Fires at most once per gluetun restart
    (qBittorrent's start time then becomes the newer one) and never during a
    normal boot, where qBittorrent already starts after gluetun. This is the
    one failure mode in the whole stack that wouldn't otherwise self-recover."""
    g = _container_started_at("gluetun")
    q = _container_started_at("qbittorrent")
    if g and q and g > q:
        subprocess.run(
            ["/usr/sbin/pct", "exec", "100", "--", "docker", "restart", "qbittorrent"],
            capture_output=True, text=True, timeout=120,
        )
        return [
            f"VPN orphan: gluetun restarted at {g} after qBittorrent at {q} "
            f"-> restarted qBittorrent so it rejoins the VPN tunnel"
        ]
    return []


def check_qbit_port_sync():
    """qBittorrent shares gluetun's network stack, and ProtonVPN periodically
    rotates gluetun's forwarded port on its own (observed 3 times in 3 days:
    56120 -> 39510 -> 41827, no container restart involved each time).
    Nothing keeps qBittorrent's own listen_port in sync with that automatically,
    so it silently drifts and qBittorrent becomes unreachable for incoming
    peer connections -- it can still connect outbound, but for anything less
    than heavily-seeded, that's the difference between finding peers and not.
    The visible symptom was several unrelated torrents (different shows,
    different indexers) all sitting at 0 peers in metaDL simultaneously --
    a network-reachability signature, not a content-availability one.
    Reads gluetun's own forwarded_port file directly (the source of truth)
    and pushes it into qBittorrent's WebUI preferences whenever they diverge."""
    try:
        result = subprocess.run(
            ["/usr/sbin/pct", "exec", "100", "--", "docker", "exec", "gluetun",
             "cat", "/tmp/gluetun/forwarded_port"],
            capture_output=True, text=True, timeout=30,
        )
        forwarded = int(result.stdout.strip())
    except Exception as e:
        return [f"qBittorrent port sync: ERROR reading gluetun forwarded port: {e}"]

    cookie = qbit_login()
    prefs = qbit_get("/api/v2/app/preferences", cookie)
    current = prefs.get("listen_port")

    if current == forwarded:
        return []

    try:
        body = urllib.parse.urlencode(
            {"json": json.dumps({"listen_port": forwarded, "random_port": False, "upnp": False})}
        ).encode()
        req = urllib.request.Request(
            f"{QBIT_BASE}/api/v2/app/setPreferences", data=body, method="POST", headers={"Cookie": cookie},
        )
        urllib.request.urlopen(req, timeout=30)
        return [
            f"qBittorrent port sync: listen_port was {current}, VPN now forwards {forwarded} "
            f"-> corrected (was unreachable for incoming peer connections)"
        ]
    except Exception as e:
        return [f"qBittorrent port sync: listen_port {current} != VPN forwarded {forwarded} -> ERROR correcting: {e}"]


def app_get(base, key, path):
    req = urllib.request.Request(f"{base}{path}", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def app_post(base, key, path, body):
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(),
        headers={"X-Api-Key": key, "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


INDEXER_HEALTH_SOURCES = ("IndexerStatusCheck", "IndexerRssCheck", "IndexerSearchCheck")


def check_app_health(state):
    """Sonarr/Radarr's own built-in health page (IndexerStatusCheck etc.) is
    exactly the kind of thing a non-technical family member would never think
    to look at, let alone know what to do about -- added live 2026-08-02
    after Knaben (accessed through Prowlarr's proxy) briefly returned
    Cloudflare 522s during a burst of searches. That correctly flagged
    'Indexers unavailable due to failures: Knaben' in Radarr, but the flag
    doesn't clear itself the moment the indexer recovers -- it sits there for
    Radarr's own internal backoff window (can be a couple of hours) looking
    exactly as broken as a real, permanent problem would, with no visible
    difference between "will fix itself" and "needs a person."

    Originally only handled the single-named-indexer message shape
    ("Indexers unavailable due to failures: Knaben"). Found live 2026-08-03
    that a real rate-limit event can also produce three blanket,
    no-name-attached warnings at once (IndexerStatusCheck's "All indexers
    are unavailable due to failures", plus matching IndexerRssCheck/
    IndexerSearchCheck ones) -- the original name-extraction regex had
    nothing to grab from those, so it silently tested nothing and never
    cleared them; confirmed by hand that once every indexer was actually
    healthy again, they needed a direct per-indexer test (not just a
    CheckHealth command) before the flags would clear at all.

    So instead of trying to parse which indexer(s) a message names, this
    just tests every currently-enabled indexer once per run, for any of the
    three warning sources above -- cheap, and correct regardless of which
    message shape shows up. If all of them test healthy, fires CheckHealth +
    RssSync so the warning has the best chance of clearing immediately
    instead of sitting stale for its own backoff window. If any are still
    genuinely down, leaves it alone and logs which ones plainly -- a real
    external tracker outage isn't something to paper over or retry
    aggressively.

    (Sonarr/Radarr's UpdateCheck warning is handled separately, in
    arr-queue-cleaner.py's check_app_updates() -- applying an update means
    pulling a new image and a container recreate, a heavier action that
    belongs on the 30-minute maintenance cadence, not this 10-minute one.)"""
    results = []

    for name, base, key in APPS:
        try:
            health = app_get(base, key, "/api/v3/health")
        except Exception as e:
            results.append(f"{name} health check: ERROR: {e}")
            continue

        indexer_items = [item for item in health if item.get("source") in INDEXER_HEALTH_SOURCES]
        if not indexer_items:
            continue

        try:
            indexers = app_get(base, key, "/api/v3/indexer")
        except Exception as e:
            results.append(f"{name} health: ERROR listing indexers to test: {e}")
            continue

        enabled = [
            idx for idx in indexers
            if idx.get("enableRss") or idx.get("enableAutomaticSearch") or idx.get("enableInteractiveSearch")
        ]
        still_down = []
        for idx in enabled:
            try:
                test_result = app_post(base, key, "/api/v3/indexer/test", idx)
                if test_result:  # a non-empty response means validation failures
                    still_down.append(idx["name"])
            except Exception:
                still_down.append(idx["name"])

        messages = "; ".join(item["message"] for item in indexer_items)

        if not still_down:
            try:
                app_post(base, key, "/api/v3/command", {"name": "CheckHealth"})
                app_post(base, key, "/api/v3/command", {"name": "RssSync"})
                results.append(
                    f"{name} health: {messages} -- every enabled indexer tests healthy again right now "
                    f"-> triggered a health/RSS refresh so the warning clears itself"
                )
            except Exception as e:
                results.append(f"{name} health: indexers recovered but ERROR triggering refresh: {e}")
        else:
            results.append(
                f"{name} health: {messages} (still down when tested just now: {', '.join(still_down)} -- "
                f"external tracker outage, not a config issue; will clear on its own once the tracker recovers)"
            )

    return results


INDEXER_NAME_RE = re.compile(r"[Ii]ndexer (\w[\w. ]*?)(?:\s*\(Prowlarr\)|:|$)")
RATE_LIMIT_WINDOW_SECONDS = 10 * 60  # matches this check's own 10-minute cadence


def check_indexer_rate_limits(state):
    """Sonarr/Radarr's own health page never flags this at all -- discovered
    live 2026-08-03 when a freshly-added movie's automatic on-add search
    genuinely found real, well-seeded releases (confirmed separately via a
    direct interactive release check) but failed to grab a single one,
    because Knaben (reached through Prowlarr) was returning HTTP 429 (Too
    Many Requests) / "API Grab Limit reached" for every attempt. The item
    just sat in Wanted looking exactly like nothing had happened, with
    nothing anywhere saying why -- not a health warning, not an error a
    non-technical family member would ever think to go looking for.

    Scans each app's own recent log for that exact signature. Deliberately
    does NOT retry immediately -- the indexer is (correctly) refusing more
    requests right now, so hammering it again would just repeat the same
    failure. The existing daily missing-content search in
    arr-queue-cleaner.py already retries anything still missing once the
    rate-limit window has reset on its own, so this is visibility-only,
    deduped per hour so a rate-limit burst that spans several 10-minute
    checks in a row is only logged once, not every cycle.

    Also: Knaben, LimeTorrents, Nyaa.si, and YTS all had conservative
    query/grab limits configured directly in Prowlarr the same day this was
    found (100-150 queries and 50-75 grabs per day, self-throttling well
    below whatever the trackers' own hard limits turn out to be), so this is
    meant to catch the rare case that still slips through, not to be the
    primary defence."""
    results = []
    now_utc = datetime.now(timezone.utc)
    ledger = state.get("rate_limit_ledger", {})
    hour_bucket = now_utc.strftime("%Y-%m-%d-%H")

    for name, base, key in APPS:
        try:
            log = app_get(base, key, "/api/v3/log?pageSize=200&sortKey=time&sortDirection=descending")
        except Exception as e:
            results.append(f"{name} indexer rate-limit check: ERROR reading log: {e}")
            continue

        hits = []
        named_entries = []  # every recent entry, regardless of exact wording, to pull the indexer name from
        for entry in log.get("records", []):
            try:
                ts = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            if (now_utc - ts).total_seconds() > RATE_LIMIT_WINDOW_SECONDS:
                continue
            named_entries.append(entry)
            message = entry.get("message", "")
            if "429" in message or "Grab Limit" in message:
                hits.append(entry)

        if not hits:
            continue

        # The line that actually says "429"/"Grab Limit" is usually a raw
        # HTTP-layer message with no indexer name in it at all -- the name
        # only shows up on the neighbouring "Couldn't add release ... from
        # Indexer X" line a few log entries away, so the name has to come
        # from the whole recent window, not just the hit lines themselves.
        indexer_names = set()
        for entry in named_entries:
            m = INDEXER_NAME_RE.search(entry.get("message", ""))
            if m:
                indexer_names.add(m.group(1).strip())
        who = ", ".join(sorted(indexer_names)) if indexer_names else "an indexer"

        ledger_key = f"{hour_bucket}:{name}:{who}"
        if ledger_key in ledger:
            continue
        ledger[ledger_key] = True

        results.append(
            f"{name}: {who} is rate-limiting requests ({len(hits)} failed grab/search attempt(s) in the "
            f"last {RATE_LIMIT_WINDOW_SECONDS // 60}min) -- searches will keep finding releases but failing "
            f"to grab them until the indexer's own limit window resets. Nothing to fix by hand; the daily "
            f"missing-content search will pick up anything still missing automatically once it clears."
        )

    state["rate_limit_ledger"] = ledger
    return results


def main():
    now = time.time()
    state = load_json_state(STATE_FILE)
    results = []

    missing = config.missing_required()
    if missing:
        results.append(f"WARNING: missing required environment variables: {', '.join(missing)} -- see .env.example")

    for label, fn in (
        # VPN-orphan first: if it restarts qBittorrent, the qBittorrent-dependent
        # checks below simply error out for this one run (caught + logged) and
        # are healthy again next run -- better than reading a stranded qBittorrent.
        ("VPN orphan check", check_vpn_orphan),
        ("qBittorrent port sync check", check_qbit_port_sync),
        ("0-seed stall check", lambda: check_stalled_torrents(state, now)),
        ("Transfer speed check", lambda: check_transfer_speed(state, now)),
        ("Command queue check", lambda: check_stuck_commands(state, now)),
        ("App health check", lambda: check_app_health(state)),
        ("Indexer rate-limit check", lambda: check_indexer_rate_limits(state)),
    ):
        try:
            results.extend(fn())
        except Exception as e:
            results.append(f"{label}: ERROR: {e}")

    save_json_state(STATE_FILE, state)

    if results:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} === run ===\n")
            for line in results:
                f.write(line + "\n")


if __name__ == "__main__":
    main()
