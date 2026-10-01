"""In-memory activity log shared across backend modules.

Lets the frontend poll for task status (started/done/failed, model
switches, etc.) instead of only seeing it in the server console.
"""

import itertools
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_events: list[dict] = []
_id_counter = itertools.count(1)

MAX_EVENTS = 200


def log_event(level: str, message: str) -> None:
    """level: 'info' | 'success' | 'warning' | 'error'"""

    event = {
        "id": next(_id_counter),
        "time": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    with _lock:
        _events.append(event)
        if len(_events) > MAX_EVENTS:
            del _events[: len(_events) - MAX_EVENTS]


def get_events(since_id: int = 0) -> list[dict]:
    with _lock:
        return [e for e in _events if e["id"] > since_id]
