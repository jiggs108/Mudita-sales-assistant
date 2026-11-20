"""FastAPI app: webhooks, Google OAuth, dashboard API, and the dashboard page."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from . import booking, channels, db, engine, policy as policy_mod
from .calendar_google import CalendarError, GoogleCalendar
from .llm import GroqLLM

STATIC = Path(__file__).with_name("static")
PUBLIC_PATHS = ("/webhooks/", "/healthz", "/oauth/google/callback")


def _init_statuses() -> None:
    checks = {
        "whatsapp": all(os.environ.get(k) for k in ("WA_PHONE_NUMBER_ID", "WA_ACCESS_TOKEN", "META_VERIFY_TOKEN")),
        "instagram": all(os.environ.get(k) for k in ("IG_ACCESS_TOKEN", "META_VERIFY_TOKEN")),
        "email": channels.email_configured(),
        "calendar": engine.deps.calendar.configured(),
        "llm": bool(os.environ.get("GROQ_API_KEY")),
    }
    for name, ok in checks.items():
        if not ok:
            db.set_status(name, "not_configured", "Environment variables missing")
        elif db.get_status(name)["state"] in ("unknown", "not_configured"):
            db.set_status(name, "ok", "Configured")
    if checks["calendar"] and not engine.deps.calendar.connected():
        db.set_status("calendar", "error", "Not connected — click 'Connect Google Calendar'")


async def email_poller() -> None:
    interval = int(os.environ.get("EMAIL_POLL_SECONDS", "30"))
    while True:
        await poll_email_once()
        await asyncio.sleep(interval)


async def poll_email_once() -> int:
    try:
        items = await asyncio.to_thread(channels.fetch_new_emails)
    except channels.ChannelError as e:
        db.log(None, "email_error", str(e))
        return 0
    for it in items:
        await asyncio.to_thread(engine.handle_inbound, "email", it["user_id"], it["text"], it["msg_id"],
                                it["name"], it["meta"])
    return len(items)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db._conn is None:
        db.init()
    policy_mod.get_text()  # seed the default policy on first run
    engine.deps.llm = engine.deps.llm or GroqLLM()
    engine.deps.calendar = engine.deps.calendar or GoogleCalendar()
    _init_statuses()
    task = asyncio.create_task(email_poller()) if channels.email_configured() else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Sales Assistant", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Dashboard + API are behind HTTP Basic auth (DASHBOARD_USER / DASHBOARD_PASSWORD). Webhooks are public."""
    pwd = os.environ.get("DASHBOARD_PASSWORD")
    if pwd and not request.url.path.startswith(PUBLIC_PATHS):
        user = os.environ.get("DASHBOARD_USER", "demo")
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                u, p = base64.b64decode(header[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pwd)
            except Exception:
                ok = False
        if not ok:
            return PlainTextResponse("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="dashboard"'})
    return await call_next(request)


# ---- Webhooks (Meta) ---------------------------------------------------------
def _verify_get(request: Request) -> PlainTextResponse:
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == os.environ.get("META_VERIFY_TOKEN"):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


async def _receive(request: Request, bg: BackgroundTasks, channel: str, secret_env: str, parser) -> dict:
    raw = await request.body()
    secret = os.environ.get(secret_env)
    if secret and not channels.verify_signature(raw, request.headers.get("x-hub-signature-256"), secret):
        db.log(None, "webhook_rejected", f"{channel}: bad signature")
        raise HTTPException(403, "bad signature")
    try:
        items = parser(json.loads(raw or b"{}"))
    except (ValueError, KeyError) as e:
        db.log(None, "webhook_error", f"{channel}: could not parse payload: {e}")
        return {"ok": True}
    db.set_status(channel, "ok", f"Webhook received ({len(items)} message(s))")
    for it in items:  # respond 200 fast; process in background
        bg.add_task(engine.handle_inbound, channel, it["user_id"], it["text"], it["msg_id"], it["name"])
    return {"ok": True}


@app.get("/webhooks/whatsapp")
def wa_verify(request: Request):
    return _verify_get(request)


@app.post("/webhooks/whatsapp")
async def wa_receive(request: Request, bg: BackgroundTasks):
    return await _receive(request, bg, "whatsapp", "META_APP_SECRET", channels.parse_whatsapp)


@app.get("/webhooks/instagram")
def ig_verify(request: Request):
    return _verify_get(request)


@app.post("/webhooks/instagram")
async def ig_receive(request: Request, bg: BackgroundTasks):
    return await _receive(request, bg, "instagram", "IG_APP_SECRET", channels.parse_instagram)


# ---- Google OAuth ------------------------------------------------------------
@app.get("/oauth/google/start")
def google_start():
    cal = engine.deps.calendar
    if not cal.configured():
        raise HTTPException(400, "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI first")
    return RedirectResponse(cal.auth_url())


@app.get("/oauth/google/callback")
def google_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        db.set_status("calendar", "error", f"Google consent failed: {error}")
        return RedirectResponse("/")
    try:
        engine.deps.calendar.exchange_code(code, state)
    except CalendarError as e:
        db.set_status("calendar", "error", str(e))
    return RedirectResponse("/")


# ---- Dashboard API ---------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/api/state")
def state():
    leads = db.q("""SELECT l.*, c.channel, c.external_id, c.display_name,
                    (SELECT text FROM messages m WHERE m.contact_id=c.id ORDER BY id DESC LIMIT 1) AS last_text,
                    (SELECT created_at FROM messages m WHERE m.contact_id=c.id ORDER BY id DESC LIMIT 1) AS last_at
                    FROM leads l JOIN contacts c ON c.id=l.contact_id ORDER BY l.updated_at DESC""")
    statuses = {n: db.get_status(n) for n in ("whatsapp", "instagram", "email", "calendar", "llm")}
    return {"statuses": statuses, "leads": leads, "calendar_connected": engine.deps.calendar.connected()}


@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: int):
    lead = db.get_lead(lead_id) or _404()
    contact = db.get_contact(lead["contact_id"])
    pol = policy_mod.load()
    rep = next((r.model_dump() for r in pol.reps if r.id == lead["assigned_rep"]), None)
    return {
        "lead": lead, "contact": contact, "rep": rep,
        "messages": db.messages_for(contact["id"], 200),
        "actions": db.q("SELECT * FROM actions WHERE lead_id=? ORDER BY id DESC LIMIT 200", (lead_id,)),
        "bookings": db.q("SELECT * FROM bookings WHERE lead_id=? ORDER BY id", (lead_id,)),
    }


def _404():
    raise HTTPException(404, "not found")


class Toggle(BaseModel):
    on: bool


class Text(BaseModel):
    text: str


@app.post("/api/leads/{lead_id}/takeover")
def takeover(lead_id: int, body: Toggle):
    db.get_lead(lead_id) or _404()
    db.update_lead(lead_id, takeover=1 if body.on else 0)
    db.log(lead_id, "takeover", "Human took over — automation stopped" if body.on else "Automation handed back to assistant")
    return {"ok": True}


@app.post("/api/leads/{lead_id}/reply")
def manual_reply(lead_id: int, body: Text):
    db.get_lead(lead_id) or _404()
    if not body.text.strip():
        raise HTTPException(400, "empty message")
    return {"ok": engine.human_reply(lead_id, body.text.strip())}


@app.post("/api/leads/{lead_id}/resume")
def resume(lead_id: int):
    db.get_lead(lead_id) or _404()
    db.update_lead(lead_id, needs_review=0, review_reason=None)
    db.log(lead_id, "resumed", "Review cleared by human — automation will act on the next message")
    return {"ok": True}


@app.post("/api/leads/{lead_id}/retry")
def retry(lead_id: int):
    db.get_lead(lead_id) or _404()
    db.log(lead_id, "retry", "Human asked the assistant to re-process the conversation")
    engine.process(lead_id)
    return {"ok": True}


@app.post("/api/leads/{lead_id}/reconcile")
def reconcile(lead_id: int):
    lead = db.get_lead(lead_id) or _404()
    try:
        results = booking.reconcile(engine.deps.calendar, lead_id)
    except CalendarError as e:
        raise HTTPException(502, f"calendar check failed: {e}")
    for b in results:
        db.log(lead_id, "reconcile", f"Booking {b['event_id']}: {b['status']}")
        if b["status"] == "confirmed":
            pol = policy_mod.load()
            from .slots import fmt
            rep = pol.rep(lead["assigned_rep"])
            db.update_lead(lead_id, status="booked", needs_review=0, review_reason=None, meeting={
                "when": fmt({"start": b["slot_start"]}, pol), "start": b["slot_start"], "end": b["slot_end"],
                "rep_name": rep.name, "event_id": b["event_id"], "link": b.get("link")})
    return {"ok": True, "results": results}


@app.get("/api/policy", response_class=PlainTextResponse)
def get_policy():
    return policy_mod.get_text()


@app.put("/api/policy")
async def put_policy(request: Request):
    text = (await request.body()).decode()
    try:
        policy_mod.save(text)
    except Exception as e:
        raise HTTPException(400, f"Invalid policy: {e}")
    db.log(None, "policy", "Business policy updated")
    return {"ok": True}


@app.post("/api/email/poll")
async def email_poll():
    if not channels.email_configured():
        raise HTTPException(400, "email not configured")
    return {"new": await poll_email_once()}


@app.get("/api/log")
def global_log():
    return db.q("SELECT * FROM actions WHERE lead_id IS NULL ORDER BY id DESC LIMIT 50")
