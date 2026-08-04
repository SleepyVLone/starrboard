# Changelog

Semantic versioning, `MAJOR.MINOR.PATCH`:

- **MAJOR** for a breaking change, such as one needing a config migration or changing an existing folder/file layout scripts depend on.
- **MINOR** for a new feature that does not break anything.
- **PATCH** for a bug fix.

Newest first. Each release is tagged `vX.Y.Z`.

---

## v1.2.0

**Added: `check_indexer_rate_limits()` to `arr-health-check.py`.**

Found live 2026-08-03: a freshly-added movie's automatic on-add search genuinely found real, well-seeded releases (confirmed separately via a direct interactive release check) but failed to grab a single one, because one indexer was returning HTTP 429 (Too Many Requests) for every attempt. The item just sat in Wanted looking exactly like nothing had happened -- not a health warning, not an error anyone would think to go looking for, and (per a live report) something that had been happening more and more often lately. This scans each app's own recent log for that exact signature and logs plainly which indexer and how many attempts failed. Deliberately doesn't retry immediately -- the indexer is correctly refusing more requests right now, so hammering it again would just repeat the failure -- the existing daily missing-content search already retries anything still missing once the rate-limit window resets on its own. Deduped per hour so a burst spanning several 10-minute checks in a row is only logged once.

Also set conservative query/grab limits directly on every active indexer in Prowlarr (self-throttling well below whatever the trackers' own hard limits turn out to be), as the primary defence -- this new check is meant to catch the rare case that still slips through, not to be the only thing standing between normal use and a rate-limit ban.

## v1.1.0

**Added: a YouTube tab on the Add page, with its own download pipeline.**

Sonarr/Radarr have no concept of YouTube as a source -- there's no indexer or release to grab -- so this is a separate `yt-dlp`-based pipeline living alongside the existing Sonarr/Radarr add flow. Give it a title, a channel/playlist/video URL, and a resolution, and it downloads into its own `Season 01` folder structure, grabs each video's real thumbnail as the episode image (and the first one as the show's poster, only the first time, so it never overwrites a poster picked by hand later), and folds its progress into the same "Now downloading" card the Sonarr/Radarr queue already uses.

Jellyfin auto-identifies new items against IMDb/TMDb regardless of a library's own internet-provider settings -- confirmed live, even a real, correctly-matched library item came back with full provider IDs despite that setting being off -- so an invented YouTube title reliably gets fuzzy-matched to some unrelated real show or movie. Rather than trust that setting, the pipeline waits for Jellyfin to scan the new download in, then strips and hard-locks (`LockData`) whatever it auto-matched back to the plain local title, so no future rescan can silently rename it again either.

**Added: a paced, direct-search rescue for Sonarr's Wanted backlog.**

Neither Sonarr nor Radarr has a built-in recurring "search everything that's missing" task -- both only really catch new releases via RSS Sync as they're freshly posted, so anything RSS missed just sits in Wanted forever. Sonarr's own built-in bulk search command turned out unreliable at real backlog scale (firing hundreds of rapid-fire searches in minutes tripped indexer rate-limiting and silently swallowed most of the results), so this instead checks Sonarr's own per-episode release candidates directly, one at a time with a deliberate pause between each, and grabs the best clean match it finds -- capped per run so a large backlog drains over several days instead of hammering every indexer in one burst.

**Added: a smarter "why is this orphaned" diagnosis.**

When a monitored movie has zero grabs ever, the existing orphan check now also asks *why*: genuinely zero availability anywhere, releases existing but all wrongly rejected (naming the actual rejection reason), or a clean release existing that should already have been grabbed. That last case caught a real bug: a shared language-restriction setting was permanently rejecting perfectly good multi-audio releases because of naive filename-based language detection, for movies that had sat "unavailable" for weeks.

**Added: `check_app_health()` (indexer-outage auto-recovery) to `arr-health-check.py`.**

Sonarr/Radarr's own health page can flag an indexer as unavailable after a transient outage, and that flag doesn't clear itself the moment the indexer recovers -- it sits looking exactly as broken as a real, permanent problem for the app's own internal backoff window (can be hours). This live-tests the flagged indexer right now, and if it's actually fine again, triggers a health/RSS refresh so the warning clears immediately instead of sitting stale.

**Added: `check_app_updates()` (auto-apply Sonarr/Radarr updates) to `arr-queue-cleaner.py`.**

Backs up the app's config, pulls the new image, recreates the container, then polls until it actually responds healthy before declaring success -- one attempt per exact version string, ever, so a failed/incompatible update gets logged loudly for a person rather than retried every 30 minutes.

**Fixed: the YouTube tab could report success before the Jellyfin metadata correction had actually finished.**

The download-progress status only ever reached a terminal "done" state the moment the download itself completed, showing a hardcoded success message regardless of whether the metadata-correction step afterward had concluded, was still running, or had failed. It now keeps polling until that step has genuinely concluded and shows its real outcome.

## v1.0.1

**Fixed: code comments referenced real, specific show titles and an indexer by name.**

Several bug-report-style comments documenting real problems hit during development named the actual show and, in one case, the indexer involved. None of that changes what the code does, but it's an unnecessary and pointless thing to have sitting in a public repo, so every one of those references was swapped for a generic description of the same bug pattern (a TV episode, an anime season, a multi-episode batch, and so on), with the technical detail that actually matters (filenames, timings, thresholds) left untouched.

**Fixed: a handful of American spellings in comments and documentation.**

`monopolize`/`monopolizing` to `monopolise`/`monopolising`, `judgment` to `judgement`, and `centering`/`centered` to `centring`/`centred` in the changelog. Left every CSS/JS spelling that has to stay American alone (`color`, `center`, and the `behavior`/`block` keys `scrollIntoView` requires), since those aren't a style choice, they're what the browser actually expects.

## v1.0.0

**Added: first public release, split out of the private production copy.**

Pulled the dashboard and its two companion scripts (`arr-queue-cleaner.py`, `arr-health-check.py`) off the home Proxmox host they run on unattended, scrubbed every hardcoded credential (Sonarr/Radarr API keys, qBittorrent password) into environment variables, and restructured the whole project into its own repo: `HTML/`, `CSS/`, `JS/`, and `PYTHON/` folders, one file per dashboard page, with a shared `arr_common` package (config, qBittorrent client, atomic state persistence) removing the duplication that existed across all three scripts.

**Added: Radarr coverage alongside Sonarr throughout.**

The dashboard's live command-queue view and its health card previously only reported Sonarr's search/scan commands. Both now cover Sonarr and Radarr together, each entry tagged with its source, and `arr-health-check.py`'s frozen-command watchdog now watches and can restart either service independently, not just Sonarr.

**Added: a stdlib test suite.**

`PYTHON/tests.py`, 13 `unittest` tests (no pip dependencies, matching the rest of the project) covering byte formatting, next-run-time scheduling, log parsing, and the atomic/corrupt-safe state file pattern every script relies on.

**Added: startup config validation.**

A blank required environment variable now logs a clear warning naming it, instead of every page just silently reading "Unreachable" with no explanation of why.

**Fixed: the Calendar page's nav bar was missing the Library link entirely**, and separately, the whole top nav bar visibly shifted position when moving from the Overview page to any other page. The first was a missing link in that one page's markup; the second was `justify-content: space-between` centring the nav based on the width of whatever sat to its left and right, which differed page to page. The nav is now absolutely centred against the bar itself, so its position can never depend on its neighbours again.

**Fixed: Library and Add pages showed a raw Python exception string on failure** (e.g. `urlopen error [Errno 61] Connection refused`) instead of a plain-English message, and the Add page's quality/folder dropdowns had no error handling at all, so a failed fetch there silently left them empty.
