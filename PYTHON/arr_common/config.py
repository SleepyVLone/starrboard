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
    return missing
