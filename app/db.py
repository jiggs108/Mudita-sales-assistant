"""SQLite persistence. Everything (conversations, leads, bookings, action log) survives restarts and page refreshes."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,              -- whatsapp | instagram | email
  external_id TEXT NOT NULL,          -- wa_id / IG-scoped id / email address
  display_name TEXT,
  meta_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(channel, external_id)        -- identity is per channel; never merged across channels
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER NOT NULL REFERENCES contacts(id),
  channel TEXT NOT NULL,
  direction TEXT NOT NULL,            -- in | out
  author TEXT NOT NULL,               -- customer | bot | human
  text TEXT NOT NULL,
  external_id TEXT,                   -- provider message id (dedupes webhook retries)
  status TEXT DEFAULT 'ok',           -- ok | failed
  created_at TEXT NOT NULL,
  UNIQUE(channel, external_id)
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER NOT NULL UNIQUE REFERENCES contacts(id),
  status TEXT NOT NULL DEFAULT 'new',
  qualification TEXT,                 -- qualified | not_qualified | needs_info
  fields_json TEXT DEFAULT '{}',
  decision_json TEXT DEFAULT '{}',
  assigned_rep TEXT,
  offered_slots_json TEXT DEFAULT '[]',
  pending_slot_json TEXT,
  meeting_json TEXT,
  takeover INTEGER NOT NULL DEFAULT 0,
  needs_review INTEGER NOT NULL DEFAULT 0,
  review_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY,
  idem_key TEXT NOT NULL UNIQUE,      -- one booking per (lead, slot) — repeat confirmations can't duplicate
  lead_id INTEGER NOT NULL,
  slot_start TEXT NOT NULL,
  slot_end TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  event_id TEXT,
  status TEXT NOT NULL,               -- pending | confirmed | uncertain | conflict | failed
  detail TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JSON_COLS = {
    "fields_json": "fields", "decision_json": "decision", "offered_slots_json": "offered_slots",
    "pending_slot_json": "pending_slot", "meeting_json": "meeting", "meta_json": "meta",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(path: str | None = None) -> None:
    global _conn
    path = path or os.environ.get("DATABASE_PATH", "data/app.db")
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
    _conn.executescript(SCHEMA)
    _conn.commit()


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col, name in JSON_COLS.items():
        if col in d:
            raw = d.pop(col)
            d[name] = json.loads(raw) if raw else None
    return d


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
        return [_decode(r) for r in rows]


def one(sql: str, args: tuple = ()) -> dict | None:
    r = q(sql, args)
    return r[0] if r else None


def ex(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


# ---- settings -------------------------------------------------------------
def get_setting(key: str, default=None):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_json(key: str, default=None):
    v = get_setting(key)
    return json.loads(v) if v else default


def set_json(key: str, value) -> None:
    set_setting(key, json.dumps(value))


def set_status(name: str, state: str, detail: str = "") -> None:
    """Integration health shown in the dashboard header (ok | error | not_configured)."""
    set_json(f"status:{name}", {"state": state, "detail": detail, "at": now_iso()})


def get_status(name: str) -> dict:
    return get_json(f"status:{name}", {"state": "unknown", "detail": "", "at": None})


# ---- contacts / messages / leads -----------------------------------------
def get_or_create_contact(channel: str, external_id: str, display_name: str | None = None) -> dict:
    with _lock:
        ex("INSERT OR IGNORE INTO contacts(channel,external_id,display_name,created_at) VALUES(?,?,?,?)",
           (channel, external_id, display_name, now_iso()))
        c = one("SELECT * FROM contacts WHERE channel=? AND external_id=?", (channel, external_id))
        if display_name and not c["display_name"]:
            ex("UPDATE contacts SET display_name=? WHERE id=?", (display_name, c["id"]))
            c["display_name"] = display_name
        return c


def update_contact_meta(contact_id: int, meta: dict) -> None:
    with _lock:
        c = one("SELECT meta_json FROM contacts WHERE id=?", (contact_id,))
        merged = {**(c["meta"] or {}), **meta}
        ex("UPDATE contacts SET meta_json=? WHERE id=?", (json.dumps(merged), contact_id))


def get_contact(contact_id: int) -> dict:
    return one("SELECT * FROM contacts WHERE id=?", (contact_id,))


def add_message(contact_id: int, channel: str, direction: str, author: str, text: str,
                external_id: str | None = None, status: str = "ok") -> int | None:
    """Returns the new id, or None if this provider message id was already stored (webhook retry)."""
    cur = ex("INSERT OR IGNORE INTO messages(contact_id,channel,direction,author,text,external_id,status,created_at) "
             "VALUES(?,?,?,?,?,?,?,?)", (contact_id, channel, direction, author, text, external_id, status, now_iso()))
    return cur.lastrowid if cur.rowcount else None


def messages_for(contact_id: int, limit: int = 30) -> list[dict]:
    rows = q("SELECT * FROM messages WHERE contact_id=? ORDER BY id DESC LIMIT ?", (contact_id, limit))
    return list(reversed(rows))


def get_or_create_lead(contact_id: int) -> dict:
    with _lock:
        ts = now_iso()
        ex("INSERT OR IGNORE INTO leads(contact_id,created_at,updated_at) VALUES(?,?,?)", (contact_id, ts, ts))
        return one("SELECT * FROM leads WHERE contact_id=?", (contact_id,))


def get_lead(lead_id: int) -> dict | None:
    return one("SELECT * FROM leads WHERE id=?", (lead_id,))


def update_lead(lead_id: int, **fields) -> dict:
    sets, args = [], []
    for k, v in fields.items():
        col = next((c for c, n in JSON_COLS.items() if n == k), k)
        if col in JSON_COLS:
            v = None if v is None else json.dumps(v)
        sets.append(f"{col}=?")
        args.append(v)
    sets.append("updated_at=?")
    args.append(now_iso())
    ex(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", (*args, lead_id))
    return get_lead(lead_id)


def log(lead_id: int | None, kind: str, detail: str) -> None:
    ex("INSERT INTO actions(lead_id,ts,kind,detail) VALUES(?,?,?,?)", (lead_id, now_iso(), kind, detail))
