"""Google Calendar integration via the REST API (OAuth 2.0 web flow, offline refresh token).

Why Google Calendar REST + OAuth (not a booking tool like Calendly):
- we need to *read* availability (freeBusy) and *create* events ourselves so we can recheck
  availability right before booking and make creation idempotent (client-supplied event id).
"""
from __future__ import annotations

import os
import secrets
import time
from datetime import datetime
from urllib.parse import quote, urlencode

import httpx

from . import db

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"


class CalendarError(Exception):
    """A definite failure: we know the action did NOT happen."""


class CalendarAuthError(CalendarError):
    """Connection missing/expired (refresh token revoked). Needs a human to reconnect."""


class CalendarUncertain(Exception):
    """We don't know whether the write happened (timeout / 5xx). Must NOT be blindly retried."""


class EventAlreadyExists(Exception):
    pass


class GoogleCalendar:
    def __init__(self):
        self.client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        self.redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "")
        self.calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
        self._access: tuple[str, float] | None = None

    # ---- OAuth ------------------------------------------------------------
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def connected(self) -> bool:
        return bool((db.get_json("google_tokens") or {}).get("refresh_token"))

    def auth_url(self) -> str:
        state = secrets.token_urlsafe(16)
        db.set_setting("google_oauth_state", state)
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
        })

    def exchange_code(self, code: str, state: str) -> None:
        if not state or state != db.get_setting("google_oauth_state"):
            raise CalendarAuthError("OAuth state mismatch")
        r = httpx.post(TOKEN_URL, timeout=15, data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"})
        if r.status_code != 200:
            raise CalendarAuthError(f"token exchange failed: {r.text[:200]}")
        tok = r.json()
        if not tok.get("refresh_token"):
            raise CalendarAuthError("no refresh_token returned (remove app access in Google account and retry)")
        db.set_json("google_tokens", {"refresh_token": tok["refresh_token"]})
        self._access = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
        db.set_status("calendar", "ok", f"Connected to calendar '{self.calendar_id}'")

    def disconnect(self, reason: str) -> None:
        db.set_json("google_tokens", {})
        self._access = None
        db.set_status("calendar", "error", reason)

    def _token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        refresh = (db.get_json("google_tokens") or {}).get("refresh_token")
        if not refresh:
            db.set_status("calendar", "error", "Not connected — click 'Connect Google Calendar'")
            raise CalendarAuthError("Google Calendar is not connected")
        try:
            r = httpx.post(TOKEN_URL, timeout=15, data={
                "refresh_token": refresh, "client_id": self.client_id,
                "client_secret": self.client_secret, "grant_type": "refresh_token"})
        except httpx.HTTPError as e:
            raise CalendarError(f"token refresh network error: {e}") from e
        if r.status_code != 200:
            if "invalid_grant" in r.text:
                self.disconnect("Connection expired or revoked — reconnect Google Calendar")
                raise CalendarAuthError("Google Calendar connection expired or was revoked")
            raise CalendarError(f"token refresh failed: HTTP {r.status_code}")
        tok = r.json()
        self._access = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
        return self._access[0]

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            try:
                r = httpx.request(method, API + path, timeout=15,
                                  headers={"Authorization": f"Bearer {self._token()}"}, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise CalendarUncertain(f"{method} {path}: {type(e).__name__}") from e
            if r.status_code == 401 and attempt == 1:
                self._access = None  # access token expired early — refresh once
                continue
            if r.status_code == 401:
                self.disconnect("Calendar rejected our credentials — reconnect")
                raise CalendarAuthError("calendar returned 401")
            return r
        raise CalendarError("unreachable")

    # ---- API ----------------------------------------------------------------
    def freebusy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Reading is safe, so any failure here is a definite CalendarError."""
        try:
            r = self._request("POST", "/freeBusy", json={
                "timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]})
        except CalendarUncertain as e:
            raise CalendarError(str(e)) from e
        if r.status_code != 200:
            raise CalendarError(f"freeBusy HTTP {r.status_code}: {r.text[:200]}")
        cal = r.json().get("calendars", {}).get(self.calendar_id, {})
        if cal.get("errors"):
            raise CalendarError(f"freeBusy error: {cal['errors']}")
        db.set_status("calendar", "ok", f"Connected to calendar '{self.calendar_id}'")
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in cal.get("busy", [])]

    def get_event(self, event_id: str) -> dict | None:
        try:
            r = self._request("GET", f"/calendars/{quote(self.calendar_id)}/events/{event_id}")
        except CalendarUncertain as e:
            raise CalendarError(str(e)) from e
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise CalendarError(f"get event HTTP {r.status_code}")
        ev = r.json()
        return None if ev.get("status") == "cancelled" else ev

    def insert_event(self, event_id: str, start: datetime, end: datetime, tz: str,
                     summary: str, description: str) -> dict:
        r = self._request("POST", f"/calendars/{quote(self.calendar_id)}/events", json={
            "id": event_id, "summary": summary, "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        })
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code == 409:
            raise EventAlreadyExists(event_id)
        if r.status_code >= 500 or r.status_code == 429:
            raise CalendarUncertain(f"insert HTTP {r.status_code}")
        raise CalendarError(f"insert HTTP {r.status_code}: {r.text[:200]}")
