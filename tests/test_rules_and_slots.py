from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db, policy, rules, slots
from tests.conftest import NOW, F


@pytest.fixture
def pol():
    db.init(":memory:")
    return policy.load()


def full(**over):
    f = {"name": F("Asha", "I'm Asha"), "service": F("dashboards", "need dashboards"),
         "budget": F(20000, "budget is £20k", currency="GBP"), "timeline_months": F(1, "start next month")}
    f.update(over)
    return f


def test_qualified_and_default_route(pol):
    d = rules.evaluate(pol, full())
    assert d["status"] == "qualified"
    assert d["rep_id"] == "priya" and "Default" in d["rep_reason"]


def test_route_rule_order(pol):
    assert rules.evaluate(pol, full(service=F("ml", "ml")))["rep_id"] == "tom"
    big = rules.evaluate(pol, full(service=F("ml", "ml"), budget=F(80000, "80k", currency="GBP")))
    assert big["rep_id"] == "priya" and big["rep_reason"].startswith("Rule 1")


def test_missing_fields_need_info(pol):
    f = full()
    del f["budget"], f["timeline_months"]
    d = rules.evaluate(pol, f)
    assert d["status"] == "needs_info" and set(d["missing"]) == {"budget", "timeline_months"}


def test_low_budget_not_qualified(pol):
    d = rules.evaluate(pol, full(budget=F(1000, "only £1000", currency="GBP")))
    assert d["status"] == "not_qualified" and any("minimum" in r for r in d["reasons"])


def test_other_service_and_long_timeline(pol):
    d = rules.evaluate(pol, full(service=F("other", "build a mobile app"), timeline_months=F(12, "next year")))
    assert d["status"] == "not_qualified" and len(d["reasons"]) == 2


def test_wrong_currency_asks_again(pol):
    d = rules.evaluate(pol, full(budget=F(20000, "$20k", currency="USD")))
    assert d["status"] == "needs_info" and "budget" in d["missing"]


def test_policy_validation_rejects_unknown_rep(pol):
    bad = policy.get_text().replace("default: priya", "default: ghost")
    with pytest.raises(Exception):
        policy.save(bad)


def test_slots_respect_hours_notice_and_busy(pol):
    tz = ZoneInfo(pol.timezone)
    busy = [(datetime(2026, 11, 23, 12, 0, tzinfo=tz), datetime(2026, 11, 23, 13, 0, tzinfo=tz))]
    out = slots.generate(pol, busy, NOW)
    assert len(out) == 3
    for s in out:
        st = datetime.fromisoformat(s["start"])
        assert st >= NOW + timedelta(hours=4)
        local = st.astimezone(tz)
        assert local.weekday() < 5 and (9, 0) <= (local.hour, local.minute) and (local.hour, local.minute) <= (17, 0)
        assert not slots.overlaps(st, datetime.fromisoformat(s["end"]), busy)
    assert len({datetime.fromisoformat(s["start"]).date() for s in out}) == 3  # spread across days
