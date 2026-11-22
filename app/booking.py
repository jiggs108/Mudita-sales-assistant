"""Reliable, idempotent booking.

Guarantees:
1. One booking row per (lead, slot) → repeated "yes" can never create a second event.
2. Availability is rechecked immediately before creating the event.
3. The Google event id is derived from the booking key, so even a retried HTTP call can't duplicate.
4. If the outcome is uncertain (timeout / 5xx), we STOP: mark 'uncertain' and ask a human to reconcile.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime

from . import db
from .calendar_google import CalendarAuthError, CalendarError, CalendarUncertain, EventAlreadyExists
from .slots import overlaps

_book_lock = threading.Lock()  # serialise recheck+insert so two leads can't grab the same slot at once


def idem_key(lead_id: int, slot_start: str) -> str:
    return f"lead:{lead_id}|slot:{slot_start}"


def event_id(key: str, attempt: int) -> str:
    # Google event ids allow [a-v0-9]; hex digits are a subset.
    return "bk" + hashlib.sha1(f"{key}#{attempt}".encode()).hexdigest()


def _set(key: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    db.ex(f"UPDATE bookings SET {sets}, updated_at=? WHERE idem_key=?", (*fields.values(), db.now_iso(), key))


def book(cal, lead: dict, slot: dict, summary: str, description: str, tz: str, still_allowed) -> dict:
    """Returns {"result": confirmed|already|conflict|uncertain|in_progress|auth_error|error|stopped, ...}."""
    key = idem_key(lead["id"], slot["start"])
    row = db.one("SELECT * FROM bookings WHERE idem_key=?", (key,))
    if row and row["status"] == "confirmed":
        return {"result": "already", "event_id": row["event_id"]}
    if row and row["status"] in ("pending", "uncertain"):
        return {"result": "in_progress" if row["status"] == "pending" else "uncertain", "event_id": row["event_id"]}

    # Claim the booking key (atomic). A concurrent duplicate confirmation loses here.
    now = db.now_iso()
    if row is None:
        cur = db.ex("INSERT OR IGNORE INTO bookings(idem_key,lead_id,slot_start,slot_end,attempt,status,created_at,updated_at)"
                    " VALUES(?,?,?,?,1,'pending',?,?)", (key, lead["id"], slot["start"], slot["end"], now, now))
        attempt = 1
    else:  # previous attempt definitely failed (conflict/failed) → safe to try again with a fresh event id
        cur = db.ex("UPDATE bookings SET status='pending', attempt=attempt+1, updated_at=? "
                    "WHERE idem_key=? AND status IN ('conflict','failed')", (now, key))
        attempt = row["attempt"] + 1
    if cur.rowcount == 0:
        return {"result": "in_progress"}

    eid = event_id(key, attempt)
    _set(key, event_id=eid)
    start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])

    with _book_lock:
        try:
            busy = cal.freebusy(start, end)  # recheck right before booking
        except CalendarAuthError as e:
            _set(key, status="failed", detail=str(e))
            return {"result": "auth_error", "detail": str(e)}
        except CalendarError as e:
            _set(key, status="failed", detail=str(e))
            return {"result": "error", "detail": str(e)}
        if overlaps(start, end, busy):
            _set(key, status="conflict", detail="slot became busy before booking")
            return {"result": "conflict"}
        if not still_allowed():  # human took over between confirmation and booking
            _set(key, status="failed", detail="stopped: human takeover")
            return {"result": "stopped"}
        try:
            ev = cal.insert_event(eid, start, end, tz, summary, description)
        except EventAlreadyExists:
            existing = None
            try:
                existing = cal.get_event(eid)
            except CalendarError:
                pass
            if existing:
                _set(key, status="confirmed", detail="event already existed (idempotent)")
                return {"result": "confirmed", "event_id": eid, "link": existing.get("htmlLink")}
            _set(key, status="failed", detail="event id taken but event not readable")
            return {"result": "error", "detail": "event id conflict"}
        except CalendarUncertain as e:
            _set(key, status="uncertain", detail=str(e))
            return {"result": "uncertain", "detail": str(e), "event_id": eid}
        except CalendarAuthError as e:
            _set(key, status="failed", detail=str(e))
            return {"result": "auth_error", "detail": str(e)}
        except CalendarError as e:
            _set(key, status="failed", detail=str(e))
            return {"result": "error", "detail": str(e)}
    _set(key, status="confirmed", detail="created")
    return {"result": "confirmed", "event_id": eid, "link": ev.get("htmlLink")}


def reconcile(cal, lead_id: int) -> list[dict]:
    """Human-triggered: look up each uncertain booking's deterministic event id to learn what really happened."""
    out = []
    for b in db.q("SELECT * FROM bookings WHERE lead_id=? AND status IN ('uncertain','pending')", (lead_id,)):
        ev = cal.get_event(b["event_id"])
        status = "confirmed" if ev else "failed"
        _set(b["idem_key"], status=status, detail=f"reconciled: event {'exists' if ev else 'not found'}")
        out.append({**b, "status": status, "link": (ev or {}).get("htmlLink")})
    return out
