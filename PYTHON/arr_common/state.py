import json
import os


def load_json_state(path):
    """Any failure -- missing, empty (a prior run killed mid-write), or corrupt
    JSON -- returns {} so a bad state file can never permanently wedge a run.
    This whole pipeline is meant to survive unattended, so nothing here is
    allowed to hard-fail on its own bookkeeping."""
    try:
        with open(path) as f:
            content = f.read().strip()
        return json.loads(content) if content else {}
    except Exception:
        return {}


def save_json_state(path, state):
    """Atomic write (temp file + rename) so a kill mid-write can never leave a
    truncated/half-written file that the next run would choke on."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)
