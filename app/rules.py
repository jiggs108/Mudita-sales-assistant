"""Deterministic qualification and routing. Plain code + the policy file; the LLM has no say here."""
from __future__ import annotations

from .policy import Policy

FIELD_LABELS = {
    "name": "your name",
    "company": "your company name",
    "email": "your email address",
    "service": "which service you need",
    "budget": "your approximate budget",
    "timeline_months": "when you'd like to start",
}


def _val(fields: dict, key: str):
    f = fields.get(key)
    return f.get("value") if isinstance(f, dict) else None


def evaluate(policy: Policy, fields: dict) -> dict:
    """Returns {status, checks, missing, reasons, rep_id, rep_reason}."""
    q = policy.qualification
    checks: list[dict] = []   # every rule that was applied and its result — shown as evidence in the UI
    missing: list[str] = []
    disqualify: list[str] = []

    for f in q.required_fields:
        if _val(fields, f) in (None, ""):
            missing.append(f)

    service = _val(fields, "service")
    if service is not None:
        offered = {s.id for s in policy.services}
        ok = service in offered
        checks.append({"rule": "Service is one we offer", "value": policy.service_name(service), "pass": ok})
        if not ok:
            disqualify.append("the requested service isn't one we currently offer")

    budget = _val(fields, "budget")
    if budget is not None:
        currency = (fields["budget"].get("currency") or q.currency).upper()
        if currency != q.currency.upper():
            checks.append({"rule": f"Budget stated in {q.currency}", "value": f"{budget} {currency}", "pass": None})
            if "budget" not in missing:
                missing.append("budget")
        else:
            ok = float(budget) >= q.min_budget
            checks.append({"rule": f"Budget ≥ {q.min_budget:,.0f} {q.currency}",
                           "value": f"{float(budget):,.0f} {q.currency}", "pass": ok})
            if not ok:
                disqualify.append(f"our minimum project size is {q.min_budget:,.0f} {q.currency}")

    timeline = _val(fields, "timeline_months")
    if timeline is not None:
        ok = float(timeline) <= q.max_timeline_months
        checks.append({"rule": f"Start within {q.max_timeline_months:g} months",
                       "value": f"{float(timeline):g} months", "pass": ok})
        if not ok:
            disqualify.append(f"we can only plan projects starting within {q.max_timeline_months:g} months")

    if disqualify:
        status = "not_qualified"
    elif missing:
        status = "needs_info"
    else:
        status = "qualified"

    rep_id, rep_reason = (route(policy, fields) if status == "qualified" else (None, None))
    return {"status": status, "checks": checks, "missing": missing, "reasons": disqualify,
            "rep_id": rep_id, "rep_reason": rep_reason}


def route(policy: Policy, fields: dict) -> tuple[str, str]:
    service = _val(fields, "service")
    budget = _val(fields, "budget")
    for i, rule in enumerate(policy.routing.rules, 1):
        w = rule.when
        conds = []
        if w.services is not None:
            conds.append(service in w.services)
        if w.min_budget is not None:
            conds.append(budget is not None and float(budget) >= w.min_budget)
        if conds and all(conds):
            return rule.assign, f"Rule {i}: {rule.reason or 'matched'}"
    return policy.routing.default, "Default rep (no routing rule matched)"
