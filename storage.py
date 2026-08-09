"""
JSON storage for the combined DEAC bot (Stage 3 = Stage 1 + Stage 2 merged).
NOTE: attach persistent storage (Render paid disk / VPS) for real use —
this JSON file resets on redeploy on Render's free tier.
"""
import json
import os
import threading
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "deac_data.json")

_lock = threading.Lock()

_DEFAULT = {
    "drivers": {},              # group_id (str) -> driver record (see below)
    "maintenance_group_id": None,
    "pti_session": None,
    "history": {},              # driver name -> [ {kind, ref, ts, type}, ... ]
}

# driver record:
# {
#   "name": str,
#   "group_id": int,
#   "active_bol": {"kind": "photo|document|link", "ref": str, "ts": iso} | None,
#   "last_status": "EMPTY #12345" | "BOBTAIL" | None,
#   "pending": {"service_msg_id": int, "ts": iso, "last_reminder_ts": iso, "resolved": bool} | None,
# }


def load():
    with _lock:
        if not os.path.exists(DATA_FILE):
            return json.loads(json.dumps(_DEFAULT))
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return json.loads(json.dumps(_DEFAULT))


def save(data):
    with _lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_driver(data, group_id, name=None):
    gid = str(group_id)
    if gid not in data["drivers"]:
        data["drivers"][gid] = {
            "name": name,
            "group_id": group_id,
            "active_bol": None,
            "last_status": None,
            "pending": None,
        }
    elif name:
        data["drivers"][gid]["name"] = name
    return data["drivers"][gid]


def timestamp():
    return datetime.utcnow().isoformat()
