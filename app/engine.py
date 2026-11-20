"""Conversation engine: one inbound message in → understand → decide (rules) → act → reply.

Lead lifecycle: new → needs_info → (not_qualified | slots_offered) → awaiting_confirmation → booked
Flags that stop all automation: takeover (human in control) and needs_review (uncertain/failed action).
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from datetime import datetime, timezone

from . import booking, channels, db, policy as policy_mod, rules, slots
from .calendar_google import CalendarAuthError, CalendarError
from .llm import (FORBIDDEN_IN_FOLLOWUP, LLMError, extraction_prompts, ground, missing_labels,
                  reply_prompts)


class Deps:
    llm = None
    calendar = None
    send = staticmethod(channels.send)

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)


deps = Deps()
_lead_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


# ---- entry points ----------------------------------------------------------
def handle_inbound(channel: str, user_id: str, text: str, msg_id: str | None,
                   name: str | None = None, meta: dict | None = None) -> dict:
    contact = db.get_or_create_contact(channel, user_id, name)  # identity = (channel, id); never merged
    if meta:
        db.update_contact_meta(contact["id"], meta)
    if db.add_message(contact["id"], channel, "in", "customer", text, msg_id) is None:
        return {"duplicate": True}  # provider retried the webhook — already handled
    lead = db.get_or_create_lead(contact["id"])
    db.log(lead["id"], "inbound", f"[{channel}] {text[:160]}")
    process(lead["id"])
    return {"lead_id": lead["id"]}


def process(lead_id: int) -> None:
    with _lead_locks[lead_id]:  # one message at a time per lead
        _process(lead_id)


# ---- core --------------------------------------------------------------------
def _process(lead_id: int) -> None:
    lead = db.get_lead(lead_id)
    if lead["takeover"]:
        db.log(lead_id, "paused", "Human takeover is on — no automatic action taken")
        return
    if lead["needs_review"]:
        db.log(lead_id, "paused", f"Waiting for human review ({lead['review_reason']}) — no automatic action taken")
        return

    pol = policy_mod.load()
    contact = db.get_contact(lead["contact_id"])
    history = db.messages_for(contact["id"])
    offered = lead["offered_slots"] or []
    pending = lead["pending_slot"]

    # 1) Understand (LLM) ------------------------------------------------------
    try:
        system, user = extraction_prompts(pol, history, [f"{i}. {slots.fmt(s, pol)}" for i, s in enumerate(offered, 1)],
                                          slots.fmt(pending, pol) if pending else None)
        ext, usage = deps.llm.extract_json(system, user)
    except LLMError as e:
        db.log(lead_id, "error", f"LLM unavailable, no action taken: {e}. Use 'Retry' once fixed.")
        return
    _log_usage(lead_id, "extract", usage)

    customer_texts = [m["text"] for m in history if m["direction"] == "in"]
    found, dropped = ground(ext.get("fields") or {}, customer_texts, pol)
    if dropped:
        db.log(lead_id, "grounding", "Ignored unsupported values: " + "; ".join(dropped))
    fields = {**(lead["fields"] or {}), **found}
    if contact["channel"] == "email" and "email" not in fields:
        fields["email"] = {"value": contact["external_id"], "quote": "(sender address)"}
    if "name" not in fields and contact.get("display_name"):
        fields["name"] = {"value": contact["display_name"], "quote": f"(profile name on {contact['channel']})"}
    lead = db.update_lead(lead_id, fields=fields)

    status = lead["status"]
    choice = ext.get("slot_choice")
    confirmation = ext.get("confirmation")

    # 2) Decide + act ------------------------------------------------------------
    if status == "booked":
        m = lead["meeting"] or {}
        db.log(lead_id, "idempotent", "Message after booking — existing meeting kept, no new event created")
        _reply(lead, contact, f"You're all set — your {pol.meeting.title.lower()} with {m.get('rep_name')} is "
                              f"already confirmed for {m.get('when')}. No need to confirm again.")
        return

    if status in ("slots_offered", "awaiting_confirmation") and isinstance(choice, int) and 1 <= choice <= len(offered):
        slot = offered[choice - 1]
        if status == "slots_offered" or slot != pending:
            db.update_lead(lead_id, pending_slot=slot, status="awaiting_confirmation")
            db.log(lead_id, "slot_selected", f"Customer chose option {choice}: {slots.fmt(slot, pol)}")
            _reply(lead, contact, f"Great — shall I book {slots.fmt(slot, pol)} with "
                                  f"{pol.rep(lead['assigned_rep']).name}? Reply YES to confirm.")
            return

    if status == "awaiting_confirmation":
        if confirmation == "yes":
            _book(db.get_lead(lead_id), contact, pol)
            return
        if confirmation == "no":
            db.log(lead_id, "slot_rejected", "Customer declined the proposed time")
            _offer_slots(lead, contact, pol, prefix="No problem.")
            return
        _reply(lead, contact, f"Just to check — shall I book {slots.fmt(pending, pol)}? Reply YES to confirm, "
                              f"or tell me another option number.")
        return

    if status == "slots_offered":
        if confirmation == "no":
            db.update_lead(lead_id, needs_review=1, review_reason="customer wants other times")
            db.log(lead_id, "handoff", "None of the offered times suit — handed to rep")
            _reply(lead, contact, f"Understood — {pol.rep(lead['assigned_rep']).name} will contact you to find a time that works.")
            return
        listing = "\n".join(f"{i}. {slots.fmt(s, pol)}" for i, s in enumerate(offered, 1))
        _reply(lead, contact, f"Please reply with the number of the time that suits you:\n{listing}")
        return

    # Collecting / (re)qualifying
    decision = rules.evaluate(pol, fields)
    db.update_lead(lead_id, decision=decision, qualification=decision["status"])
    db.log(lead_id, "qualification", f"{decision['status']}"
           + (f" — missing: {', '.join(decision['missing'])}" if decision["missing"] else "")
           + (f" — reasons: {'; '.join(decision['reasons'])}" if decision["reasons"] else ""))

    if decision["status"] == "needs_info":
        db.update_lead(lead_id, status="needs_info")
        facts = {"ask_for": missing_labels(pol, decision["missing"]),
                 "already_known": {k: v["value"] for k, v in fields.items()}}
        fallback = "Thanks for getting in touch! To help, could you share " + ", ".join(facts["ask_for"]) + "?"
        _reply(lead, contact, _phrase(lead_id, pol, contact, "Ask the customer for the missing details in ask_for, "
                                      "briefly and naturally. Do not ask for anything already_known.", facts, fallback))
    elif decision["status"] == "not_qualified":
        db.update_lead(lead_id, status="not_qualified")
        facts = {"reasons": decision["reasons"]}
        fallback = ("Thank you for your interest. Unfortunately we can't take this on because "
                    + "; ".join(decision["reasons"]) + ". If anything changes, just reply here.")
        _reply(lead, contact, _phrase(lead_id, pol, contact, "Politely explain we can't help, using ONLY the reasons. "
                                      "Say they can reply if their details change.", facts, fallback))
    else:
        rep = pol.rep(decision["rep_id"])
        db.update_lead(lead_id, assigned_rep=rep.id)
        db.log(lead_id, "routing", f"Assigned to {rep.name} — {decision['rep_reason']}")
        _offer_slots(db.get_lead(lead_id), contact, pol, prefix="Thanks — that's everything I need.")


def _offer_slots(lead: dict, contact: dict, pol, prefix: str = "") -> None:
    rep = pol.rep(lead["assigned_rep"])
    try:
        start, end = slots.search_window(pol, deps.now())
        busy = deps.calendar.freebusy(start, end)
    except CalendarAuthError as e:
        return _needs_review(lead, contact, f"calendar connection problem: {e}",
                             f"{rep.name} will be in touch shortly to arrange a call.")
    except CalendarError as e:
        return _needs_review(lead, contact, f"calendar read failed: {e}",
                             f"{rep.name} will be in touch shortly to arrange a call.")
    offered = slots.generate(pol, busy, deps.now())
    if not offered:
        return _needs_review(lead, contact, "no free slots in the booking window",
                             f"{rep.name} will be in touch shortly to arrange a call.")
    db.update_lead(lead["id"], offered_slots=offered, pending_slot=None, status="slots_offered")
    db.log(lead["id"], "slots_offered", f"Offered {len(offered)} slots from live calendar availability")
    listing = "\n".join(f"{i}. {slots.fmt(s, pol)}" for i, s in enumerate(offered, 1))
    _reply(lead, contact, f"{prefix} I'd like to set up a {pol.meeting.duration_minutes}-minute "
                          f"{pol.meeting.title.lower()} with {rep.name}. Which time suits you?\n{listing}\n"
                          f"Reply with the number.".strip())


def _book(lead: dict, contact: dict, pol) -> None:
    rep = pol.rep(lead["assigned_rep"])
    slot = lead["pending_slot"]
    f = lead["fields"] or {}
    summary = f"{pol.meeting.title}: {f.get('name', {}).get('value', 'Lead')} ↔ {rep.name}"
    description = (f"Booked by sales assistant (lead #{lead['id']}, via {contact['channel']}).\n"
                   f"Assigned rep: {rep.name} <{rep.email}>\n"
                   + "\n".join(f"{k}: {v['value']}" for k, v in f.items()))
    db.log(lead["id"], "booking", f"Customer confirmed {slots.fmt(slot, pol)} — rechecking availability")

    def still_allowed():
        cur = db.get_lead(lead["id"])
        return not cur["takeover"] and not cur["needs_review"]

    res = booking.book(deps.calendar, lead, slot, summary, description, pol.timezone, still_allowed)
    r = res["result"]
    if r in ("confirmed", "already"):
        when = slots.fmt(slot, pol)
        db.update_lead(lead["id"], status="booked", meeting={
            "when": when, "start": slot["start"], "end": slot["end"], "rep_name": rep.name,
            "event_id": res.get("event_id"), "link": res.get("link")})
        db.log(lead["id"], "booked" if r == "confirmed" else "idempotent",
               f"Calendar event {res.get('event_id')} {'created' if r == 'confirmed' else 'already existed — not duplicated'}")
        _reply(lead, contact, f"Booked! Your {pol.meeting.title.lower()} with {rep.name} is confirmed for {when}. "
                              f"See you then.")
    elif r == "conflict":
        db.log(lead["id"], "conflict", f"{slots.fmt(slot, pol)} was taken before booking — no event created; offering new times")
        _offer_slots(lead, contact, pol, prefix="Sorry — that time was just taken.")
    elif r == "in_progress":
        db.log(lead["id"], "idempotent", "Duplicate confirmation while booking in progress — ignored")
    elif r == "stopped":
        db.log(lead["id"], "paused", "Booking stopped because a human took over")
    elif r == "uncertain":
        _needs_review(lead, contact, f"booking outcome uncertain ({res.get('detail')}) — NOT retried; use Reconcile",
                      f"Thanks! I'm finalising your booking — {rep.name} will confirm shortly.")
    else:  # auth_error / error — definitely not created
        _needs_review(lead, contact, f"booking failed: {res.get('detail')}",
                      f"Sorry, I couldn't complete the booking just now. {rep.name} will contact you to confirm a time.")


# ---- helpers ---------------------------------------------------------------
def _needs_review(lead: dict, contact: dict, reason: str, customer_msg: str) -> None:
    db.update_lead(lead["id"], needs_review=1, review_reason=reason)
    db.log(lead["id"], "needs_review", reason)
    _reply(lead, contact, customer_msg)


def _phrase(lead_id: int, pol, contact: dict, action: str, facts: dict, fallback: str) -> str:
    try:
        system, user = reply_prompts(pol, contact["channel"], action, facts, db.messages_for(contact["id"]))
        text, usage = deps.llm.write(system, user)
        _log_usage(lead_id, "reply", usage)
    except LLMError as e:
        db.log(lead_id, "error", f"LLM phrasing failed, used template: {e}")
        return fallback
    if not text or len(text) > 600 or FORBIDDEN_IN_FOLLOWUP.search(text):
        db.log(lead_id, "guardrail", "LLM reply rejected by output check — used safe template")
        return fallback
    return text


def _reply(lead: dict, contact: dict, text: str, author: str = "bot") -> bool:
    if author == "bot":
        cur = db.get_lead(lead["id"])
        if cur["takeover"]:
            db.log(lead["id"], "paused", "Reply not sent — human takeover switched on")
            return False
    try:
        ext_id = deps.send(contact, text)
    except channels.ChannelError as e:
        db.add_message(contact["id"], contact["channel"], "out", author, text, None, status="failed")
        db.log(lead["id"], "send_failed", str(e))
        return False
    db.add_message(contact["id"], contact["channel"], "out", author, text, ext_id or None)
    db.log(lead["id"], "sent", f"{author} → {contact['channel']}: {text[:120]}")
    return True


def human_reply(lead_id: int, text: str) -> bool:
    lead = db.get_lead(lead_id)
    return _reply(lead, db.get_contact(lead["contact_id"]), text, author="human")


def _log_usage(lead_id: int, step: str, usage: dict) -> None:
    if not usage:
        return
    pin = float(os.environ.get("LLM_PRICE_IN_PER_M", "0.59"))
    pout = float(os.environ.get("LLM_PRICE_OUT_PER_M", "0.79"))
    cost = usage.get("prompt_tokens", 0) / 1e6 * pin + usage.get("completion_tokens", 0) / 1e6 * pout
    db.log(lead_id, "llm", f"{step}: {usage.get('prompt_tokens', 0)} in / {usage.get('completion_tokens', 0)} out tokens ≈ ${cost:.5f}")
