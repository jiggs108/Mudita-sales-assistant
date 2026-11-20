"""Business policy: load, validate, save. The policy is the single source of truth for rules."""
from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, model_validator

from . import db

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALLOWED_FIELDS = ["name", "company", "email", "service", "budget", "timeline_months"]
DEFAULT_POLICY_PATH = Path(__file__).with_name("default_policy.yaml")


class WorkingHours(BaseModel):
    days: list[str]
    start: str
    end: str


class Meeting(BaseModel):
    title: str = "Discovery call"
    duration_minutes: int = Field(30, ge=10, le=240)
    min_notice_hours: int = Field(4, ge=0)
    days_ahead: int = Field(7, ge=1, le=30)
    slots_to_offer: int = Field(3, ge=1, le=6)


class Service(BaseModel):
    id: str
    name: str


class Qualification(BaseModel):
    required_fields: list[str]
    currency: str = "GBP"
    min_budget: float
    max_timeline_months: float


class Rep(BaseModel):
    id: str
    name: str
    email: str


class RouteWhen(BaseModel):
    services: list[str] | None = None
    min_budget: float | None = None


class RouteRule(BaseModel):
    when: RouteWhen
    assign: str
    reason: str = ""


class Routing(BaseModel):
    rules: list[RouteRule] = []
    default: str


class Policy(BaseModel):
    company: str
    timezone: str
    working_hours: WorkingHours
    meeting: Meeting
    services: list[Service]
    qualification: Qualification
    reps: list[Rep]
    routing: Routing

    @model_validator(mode="after")
    def _check(self):
        ZoneInfo(self.timezone)  # raises if the timezone is invalid
        for d in self.working_hours.days:
            if d not in DAYS:
                raise ValueError(f"working_hours.days: unknown day '{d}'")
        for t in (self.working_hours.start, self.working_hours.end):
            h, m = t.split(":")
            if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
                raise ValueError(f"bad time '{t}'")
        if self.working_hours.start >= self.working_hours.end:
            raise ValueError("working_hours.start must be before end")
        rep_ids = {r.id for r in self.reps}
        svc_ids = {s.id for s in self.services}
        if not rep_ids:
            raise ValueError("at least one rep is required")
        for f in self.qualification.required_fields:
            if f not in ALLOWED_FIELDS:
                raise ValueError(f"required field '{f}' must be one of {ALLOWED_FIELDS}")
        if self.routing.default not in rep_ids:
            raise ValueError(f"routing.default '{self.routing.default}' is not a rep")
        for i, r in enumerate(self.routing.rules, 1):
            if r.assign not in rep_ids:
                raise ValueError(f"routing rule {i} assigns unknown rep '{r.assign}'")
            for s in r.when.services or []:
                if s not in svc_ids:
                    raise ValueError(f"routing rule {i} uses unknown service '{s}'")
        return self

    def rep(self, rep_id: str) -> Rep:
        return next(r for r in self.reps if r.id == rep_id)

    def service_name(self, sid: str) -> str:
        return next((s.name for s in self.services if s.id == sid), sid)


def parse(text: str) -> Policy:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("policy must be a YAML mapping")
    return Policy.model_validate(data)


def get_text() -> str:
    text = db.get_setting("policy_yaml")
    if text is None:
        text = DEFAULT_POLICY_PATH.read_text()
        db.set_setting("policy_yaml", text)
    return text


def load() -> Policy:
    return parse(get_text())


def save(text: str) -> Policy:
    p = parse(text)  # validate before saving
    db.set_setting("policy_yaml", text)
    return p
