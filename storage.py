"""
PostgreSQL-backed storage for the combined DEAC bot.

Uses a single-row JSONB "blob" table — keeps the exact same load()/save()
API the rest of bot.py already relies on, so nothing else needs to
change. This is the fix for data disappearing every time the bot
redeploys: a database survives redeploys, an on-disk file on Railway's
default filesystem does not.

Setup on Railway:
  1. In your project, click "+ New" -> "Database" -> "Add PostgreSQL".
  2. Railway auto-creates a DATABASE_URL variable on the Postgres
     service. Reference it in your bot service's Variables tab as:
         DATABASE_URL = ${{Postgres.DATABASE_URL}}
     (Railway usually offers this as an autocomplete suggestion.)

If DATABASE_URL isn't set (e.g. running locally without a database),
this quietly falls back to a local JSON file so testing still works.
"""
import json
import os
import threading
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")

_DEFAULT = {
    "drivers": {},
    "maintenance_group_id": None,
    "pti_session": None,
    "history": {},
    "report_interval_days": 1,
    "last_report_date": None,
    "update_group_id": None,
    "update_draft": {},
    "update_missing_msg_id": None,
    "companies": [],
}

# driver record:
# {
#   "name": str,
#   "group_id": int,
#   "user_id": int | None,
#   "active_bol": {"kind": "photo|document|link", "ref": str, "ts": iso} | None,
#   "last_status": "EMPTY #12345" | "BOBTAIL" | None,
#   "pending": {"service_msg_id": int, "ts": iso, "last_reminder_ts": iso, "resolved": bool} | None,
# }

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------
if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import Json

    def _get_conn():
        return psycopg2.connect(DATABASE_URL)

    def _init_db():
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kv_store (
                        key TEXT PRIMARY KEY,
                        value JSONB NOT NULL
                    )
                    """
                )
            conn.commit()

    _init_db()

    def load():
        with _lock:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM kv_store WHERE key = 'main'")
                    row = cur.fetchone()
            if not row:
                return json.loads(json.dumps(_DEFAULT))
            d = row[0]
            for k, v in _DEFAULT.items():
                d.setdefault(k, v)
            return d

    def save(data):
        with _lock:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO kv_store (key, value) VALUES ('main', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                        """,
                        (Json(data),),
                    )
                conn.commit()

# ---------------------------------------------------------------------------
# Local JSON fallback (no DATABASE_URL set - e.g. local testing)
# ---------------------------------------------------------------------------
else:
    DATA_DIR = os.environ.get("DATA_DIR", ".")
    DATA_FILE = os.path.join(DATA_DIR, "deac_data.json")

    def load():
        with _lock:
            if not os.path.exists(DATA_FILE):
                return json.loads(json.dumps(_DEFAULT))
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                try:
                    d = json.load(f)
                except json.JSONDecodeError:
                    return json.loads(json.dumps(_DEFAULT))
            for k, v in _DEFAULT.items():
                d.setdefault(k, v)
            return d

    def save(data):
        with _lock:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Shared helpers (same for both backends)
# ---------------------------------------------------------------------------
def ensure_driver(data, group_id, name=None):
    gid = str(group_id)
    if gid not in data["drivers"]:
        data["drivers"][gid] = {
            "name": name,
            "group_id": group_id,
            "user_id": None,
            "active_bol": None,
            "last_status": None,
            "pending": None,
        }
    elif name:
        data["drivers"][gid]["name"] = name
    return data["drivers"][gid]


def timestamp():
    return datetime.utcnow().isoformat()
