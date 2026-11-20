"""Turn policy working hours + calendar busy times into bookable slots. Pure functions (easy to test)."""
from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from .policy import DAYS, Policy


def overlaps(a_start: datetime, a_end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(a_start < b_end and b_start < a_end for b_start, b_end in busy)


def search_window(policy: Policy, now: datetime) -> tuple[datetime, datetime]:
    return now, now + timedelta(days=policy.meeting.days_ahead + 1)


def generate(policy: Policy, busy: list[tuple[datetime, datetime]], now: datetime) -> list[dict]:
    tz = ZoneInfo(policy.timezone)
    m = policy.meeting
    dur = timedelta(minutes=m.duration_minutes)
    earliest = now + timedelta(hours=m.min_notice_hours)
    sh, sm = map(int, policy.working_hours.start.split(":"))
    eh, em = map(int, policy.working_hours.end.split(":"))

    by_day: list[list[tuple[datetime, datetime]]] = []
    today = now.astimezone(tz).date()
    for d in range(m.days_ahead + 1):
        day = today + timedelta(days=d)
        if DAYS[day.weekday()] not in policy.working_hours.days:
            continue
        cur = datetime.combine(day, time(sh, sm), tz)
        end_of_day = datetime.combine(day, time(eh, em), tz)
        free = []
        while cur + dur <= end_of_day:
            if cur >= earliest and not overlaps(cur, cur + dur, busy):
                free.append((cur, cur + dur))
            cur += dur
        if free:
            by_day.append(free)

    # Spread offers across days: first free slot of each day, then afternoons, etc.
    def preference(day):  # earliest slot, then mid-day, then the rest in order
        order = [0, len(day) // 2] + list(range(len(day)))
        seen, out = set(), []
        for i in order:
            if i not in seen:
                seen.add(i)
                out.append(day[i])
        return out

    queues = [preference(d) for d in by_day]
    picked: list[tuple[datetime, datetime]] = []
    rnd = 0
    while len(picked) < m.slots_to_offer and any(len(qq) > rnd for qq in queues):
        for qq in queues:
            if len(qq) > rnd and len(picked) < m.slots_to_offer:
                picked.append(qq[rnd])
        rnd += 1
    picked.sort()
    return [{"start": s.isoformat(), "end": e.isoformat()} for s, e in picked]


def fmt(slot: dict, policy: Policy) -> str:
    s = datetime.fromisoformat(slot["start"]).astimezone(ZoneInfo(policy.timezone))
    return s.strftime("%a %d %b, %H:%M") + f" ({policy.timezone})"
