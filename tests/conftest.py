"""Test doubles for the LLM, calendar and channel senders.

These fakes are ONLY used in automated tests. The running app always uses the real Groq API,
Google Calendar and Meta / email providers.
"""
from datetime import datetime, timezone

import pytest

from app import db, engine
from app.calendar_google import CalendarAuthError, CalendarUncertain, EventAlreadyExists

NOW = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)  # a Monday


def F(value, quote, **extra):
    return {"value": value, "quote": quote, **extra}


class FakeLLM:
    def __init__(self):
        self.extractions = []   # queue of dicts returned by extract_json
        self.reply_text = "Thanks! Could you share a few more details?"
        self.extract_calls = 0

    def queue(self, fields=None, slot_choice=None, confirmation=None):
        self.extractions.append({"fields": fields or {}, "slot_choice": slot_choice, "confirmation": confirmation})

    def extract_json(self, system, user):
        self.extract_calls += 1
        return self.extractions.pop(0), {"prompt_tokens": 500, "completion_tokens": 80}

    def write(self, system, user):
        return self.reply_text, {"prompt_tokens": 300, "completion_tokens": 40}


class FakeCalendar:
    def __init__(self):
        self.busy = []
        self.events = {}
        self.insert_calls = 0
        self.fail_insert = None   # None | "uncertain" | "auth"
        self.fail_freebusy = None

    def configured(self):
        return True

    def connected(self):
        return True

    def freebusy(self, start, end):
        if self.fail_freebusy == "auth":
            raise CalendarAuthError("Google Calendar connection expired or was revoked")
        return list(self.busy)

    def insert_event(self, event_id, start, end, tz, summary, description):
        self.insert_calls += 1
        if self.fail_insert == "uncertain":
            raise CalendarUncertain("insert: ReadTimeout")
        if self.fail_insert == "auth":
            raise CalendarAuthError("calendar returned 401")
        if event_id in self.events:
            raise EventAlreadyExists(event_id)
        self.events[event_id] = {"id": event_id, "start": start, "end": end, "summary": summary, "htmlLink": "https://cal/x"}
        self.busy.append((start, end))
        return self.events[event_id]

    def get_event(self, event_id):
        return self.events.get(event_id)


class FakeSender:
    def __init__(self):
        self.sent = []

    def __call__(self, contact, text):
        self.sent.append((contact["channel"], contact["external_id"], text))
        return f"out-{len(self.sent)}"


@pytest.fixture
def env(monkeypatch):
    db.init(":memory:")
    llm, cal, sender = FakeLLM(), FakeCalendar(), FakeSender()
    monkeypatch.setattr(engine.deps, "llm", llm)
    monkeypatch.setattr(engine.deps, "calendar", cal)
    monkeypatch.setattr(engine.deps, "send", sender)
    monkeypatch.setattr(engine.deps, "now", staticmethod(lambda: NOW))
    return llm, cal, sender
