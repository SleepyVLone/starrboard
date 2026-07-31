# Changelog

Semantic versioning, `MAJOR.MINOR.PATCH`:

- **MAJOR** for a breaking change, such as one needing a config migration or changing an existing folder/file layout scripts depend on.
- **MINOR** for a new feature that does not break anything.
- **PATCH** for a bug fix.

Newest first. Each release is tagged `vX.Y.Z`.

---

## v1.0.0

**Added: first public release, split out of the private production copy.**

Pulled the dashboard and its two companion scripts (`arr-queue-cleaner.py`, `arr-health-check.py`) off the home Proxmox host they run on unattended, scrubbed every hardcoded credential (Sonarr/Radarr API keys, qBittorrent password) into environment variables, and restructured the whole project into its own repo: `HTML/`, `CSS/`, `JS/`, and `PYTHON/` folders, one file per dashboard page, with a shared `arr_common` package (config, qBittorrent client, atomic state persistence) removing the duplication that existed across all three scripts.

**Added: Radarr coverage alongside Sonarr throughout.**

The dashboard's live command-queue view and its health card previously only reported Sonarr's search/scan commands. Both now cover Sonarr and Radarr together, each entry tagged with its source, and `arr-health-check.py`'s frozen-command watchdog now watches and can restart either service independently, not just Sonarr.

**Added: a stdlib test suite.**

`PYTHON/tests.py`, 13 `unittest` tests (no pip dependencies, matching the rest of the project) covering byte formatting, next-run-time scheduling, log parsing, and the atomic/corrupt-safe state file pattern every script relies on.

**Added: startup config validation.**

A blank required environment variable now logs a clear warning naming it, instead of every page just silently reading "Unreachable" with no explanation of why.

**Fixed: the Calendar page's nav bar was missing the Library link entirely**, and separately, the whole top nav bar visibly shifted position when moving from the Overview page to any other page. The first was a missing link in that one page's markup; the second was `justify-content: space-between` centering the nav based on the width of whatever sat to its left and right, which differed page to page. The nav is now absolutely centered against the bar itself, so its position can never depend on its neighbours again.

**Fixed: Library and Add pages showed a raw Python exception string on failure** (e.g. `urlopen error [Errno 61] Connection refused`) instead of a plain-English message, and the Add page's quality/folder dropdowns had no error handling at all, so a failed fetch there silently left them empty.
