import os

SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
SONARR = ("Sonarr", SONARR_URL, SONARR_API_KEY)

RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
RADARR = ("Radarr", RADARR_URL, RADARR_API_KEY)

INSTANCES = [SONARR, RADARR]

QBIT_BASE = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://localhost:9696")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
PROWLARR = ("Prowlarr", PROWLARR_URL, PROWLARR_API_KEY)

# YouTube isn't Sonarr/Radarr-managed at all -- there's no indexer or release
# to grab, so this is a separate yt-dlp pipeline. YOUTUBE_ROOT is the path as
# seen from wherever this script actually runs; Jellyfin's own library path
# only needs to agree with it, not be identical (they can be different mounts
# of the same underlying storage).
YOUTUBE_ROOT = os.environ.get("YOUTUBE_ROOT", "/media/youtube")
YT_DLP_BIN = os.environ.get("YT_DLP_BIN", "yt-dlp")


def missing_required():
    """Names of required environment variables still left at their empty
    fallback. An empty list means everything looks configured. Used at
    startup so a blank API key shows up as a clear warning in the log instead
    of silently manifesting as "Unreachable" everywhere with no explanation."""
    missing = []
    if not SONARR_API_KEY:
        missing.append("SONARR_API_KEY")
    if not RADARR_API_KEY:
        missing.append("RADARR_API_KEY")
    if not QBIT_PASS:
        missing.append("QBIT_PASS")
    if not JELLYFIN_API_KEY:
        missing.append("JELLYFIN_API_KEY")
    if not PROWLARR_API_KEY:
        missing.append("PROWLARR_API_KEY")
    return missing
