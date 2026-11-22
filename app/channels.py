"""Real inbound/outbound channels.

- WhatsApp: WhatsApp Cloud API (Meta) — webhook in, Graph API out.
- Instagram: Instagram API with Instagram Login (Meta) — webhook in, graph.instagram.com out.
- Email: IMAP polling in (works with any mailbox, e.g. Gmail + app password).
  Out: Gmail API over HTTPS when connected (many hosts, incl. Railway, block outbound SMTP ports),
  otherwise SMTP. Replies carry In-Reply-To/References so they thread.
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import hmac
import imaplib
import os
import re
import secrets
import smtplib
import time
from urllib.parse import urlencode
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import httpx

from . import db

GRAPH = "https://graph.facebook.com/" + os.environ.get("META_API_VERSION", "v21.0")
IG_GRAPH = "https://graph.instagram.com/" + os.environ.get("META_API_VERSION", "v21.0")


class ChannelError(Exception):
    pass


class ChannelAuthError(ChannelError):
    """Token expired / revoked — needs a human to fix the connection."""


def env(k: str, d: str = "") -> str:
    return os.environ.get(k, d)


# ---- webhook security -------------------------------------------------------
def verify_signature(raw: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


# ---- parsing ---------------------------------------------------------------
def parse_whatsapp(payload: dict) -> list[dict]:
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in v.get("contacts", [])}
            for m in v.get("messages", []):  # 'statuses' (delivery receipts) are ignored
                text = m.get("text", {}).get("body") if m.get("type") == "text" else f"[{m.get('type')} message]"
                out.append({"channel": "whatsapp", "user_id": m["from"], "text": text or "",
                            "msg_id": m["id"], "name": names.get(m["from"])})
    return out


def parse_instagram(payload: dict) -> list[dict]:
    own = env("IG_USER_ID")
    out = []
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []):
            msg = ev.get("message")
            if not msg or msg.get("is_echo") or msg.get("is_deleted"):
                continue  # skip our own sent messages echoed back
            sender = ev.get("sender", {}).get("id")
            if not sender or sender == own:
                continue
            out.append({"channel": "instagram", "user_id": sender, "text": msg.get("text") or "[attachment]",
                        "msg_id": msg.get("mid"), "name": None})
    return out


# ---- sending ---------------------------------------------------------------
def _raise_meta(r: httpx.Response, channel: str) -> None:
    try:
        err = r.json().get("error", {})
    except ValueError:
        err = {}
    msg = f"{channel} send failed: HTTP {r.status_code} {err.get('message', r.text[:150])}"
    if r.status_code == 401 or err.get("code") == 190:
        db.set_status(channel, "error", "Access token expired or invalid — generate a new token")
        raise ChannelAuthError(msg)
    db.set_status(channel, "error", msg)
    raise ChannelError(msg)


def send_whatsapp(to: str, text: str) -> str:
    try:
        r = httpx.post(f"{GRAPH}/{env('WA_PHONE_NUMBER_ID')}/messages", timeout=15,
                       headers={"Authorization": f"Bearer {env('WA_ACCESS_TOKEN')}"},
                       json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})
    except httpx.HTTPError as e:
        raise ChannelError(f"whatsapp network error: {e}") from e
    if r.status_code != 200:
        _raise_meta(r, "whatsapp")
    db.set_status("whatsapp", "ok", "Last send succeeded")
    return (r.json().get("messages") or [{}])[0].get("id", "")


def send_instagram(to: str, text: str) -> str:
    try:
        r = httpx.post(f"{IG_GRAPH}/{env('IG_USER_ID', 'me')}/messages", timeout=15,
                       headers={"Authorization": f"Bearer {env('IG_ACCESS_TOKEN')}"},
                       json={"recipient": {"id": to}, "message": {"text": text}})
    except httpx.HTTPError as e:
        raise ChannelError(f"instagram network error: {e}") from e
    if r.status_code != 200:
        _raise_meta(r, "instagram")
    db.set_status("instagram", "ok", "Last send succeeded")
    return r.json().get("message_id", "")


# ---- Gmail API sending (HTTPS) ---------------------------------------------
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_gmail_access: tuple[str, float] | None = None


def gmail_connected() -> bool:
    return bool((db.get_json("gmail_tokens") or {}).get("refresh_token"))


def gmail_auth_url() -> str:
    state = "gmail." + secrets.token_urlsafe(16)
    db.set_setting("gmail_oauth_state", state)
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": env("GOOGLE_CLIENT_ID"), "redirect_uri": env("GOOGLE_REDIRECT_URI"),
        "response_type": "code", "scope": GMAIL_SCOPE, "access_type": "offline",
        "prompt": "consent", "state": state, "login_hint": env("EMAIL_ADDRESS")})


def gmail_exchange_code(code: str, state: str) -> None:
    global _gmail_access
    if not state or state != db.get_setting("gmail_oauth_state"):
        raise ChannelAuthError("OAuth state mismatch")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
        "code": code, "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": env("GOOGLE_REDIRECT_URI"), "grant_type": "authorization_code"})
    if r.status_code != 200 or not r.json().get("refresh_token"):
        raise ChannelAuthError(f"Gmail token exchange failed: {r.text[:200]}")
    tok = r.json()
    db.set_json("gmail_tokens", {"refresh_token": tok["refresh_token"]})
    _gmail_access = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
    db.set_status("email", "ok", "Gmail sending connected")


def _gmail_token() -> str:
    global _gmail_access
    if _gmail_access and _gmail_access[1] > time.time():
        return _gmail_access[0]
    refresh = (db.get_json("gmail_tokens") or {}).get("refresh_token")
    try:
        r = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "refresh_token": refresh, "client_id": env("GOOGLE_CLIENT_ID"),
            "client_secret": env("GOOGLE_CLIENT_SECRET"), "grant_type": "refresh_token"})
    except httpx.HTTPError as e:
        raise ChannelError(f"Gmail token refresh network error: {e}") from e
    if r.status_code != 200:
        if "invalid_grant" in r.text:
            db.set_json("gmail_tokens", {})
            db.set_status("email", "error", "Gmail sending expired/revoked — reconnect at /oauth/gmail/start")
            raise ChannelAuthError("Gmail connection expired or was revoked")
        raise ChannelError(f"Gmail token refresh failed: HTTP {r.status_code}")
    tok = r.json()
    _gmail_access = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
    return _gmail_access[0]


def _send_via_gmail(msg: EmailMessage) -> None:
    global _gmail_access
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    for attempt in (1, 2):
        try:
            r = httpx.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", timeout=20,
                           headers={"Authorization": f"Bearer {_gmail_token()}"}, json={"raw": raw})
        except httpx.HTTPError as e:
            raise ChannelError(f"Gmail API network error: {e}") from e
        if r.status_code == 401 and attempt == 1:
            _gmail_access = None
            continue
        if r.status_code in (401, 403):
            db.set_status("email", "error", f"Gmail API refused send (HTTP {r.status_code}) — reconnect Gmail")
            raise ChannelAuthError(f"Gmail API HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            db.set_status("email", "error", f"Gmail API send failed: HTTP {r.status_code}")
            raise ChannelError(f"Gmail API HTTP {r.status_code}: {r.text[:200]}")
        return


def send_email(to: str, subject: str, text: str, in_reply_to: str | None) -> str:
    msg = EmailMessage()
    msg["From"] = env("EMAIL_ADDRESS")
    msg["To"] = to
    msg["Subject"] = subject
    mid = make_msgid(domain=env("EMAIL_ADDRESS").split("@")[-1] or "localhost")
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(text)
    if gmail_connected():
        _send_via_gmail(msg)
        db.set_status("email", "ok", "Last send succeeded (Gmail API)")
        return mid
    try:
        with smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "465")), timeout=20) as s:
            s.login(env("EMAIL_ADDRESS"), env("EMAIL_PASSWORD"))
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        db.set_status("email", "error", "SMTP login failed — check app password")
        raise ChannelAuthError(f"email auth failed: {e}") from e
    except (smtplib.SMTPException, OSError) as e:
        db.set_status("email", "error", f"SMTP error: {e}")
        raise ChannelError(f"email send failed: {e}") from e
    db.set_status("email", "ok", "Last send succeeded")
    return mid


def send(contact: dict, text: str) -> str:
    ch, to = contact["channel"], contact["external_id"]
    if ch == "whatsapp":
        return send_whatsapp(to, text)
    if ch == "instagram":
        return send_instagram(to, text)
    if ch == "email":
        meta = contact.get("meta") or {}
        subj = meta.get("subject") or "Your enquiry"
        subj = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        return send_email(to, subj, text, meta.get("last_message_id"))
    raise ChannelError(f"unknown channel {ch}")


# ---- email polling ---------------------------------------------------------
QUOTE_START = re.compile(r"^(On .+wrote:|-----Original Message-----|From: .+|>)", re.M)


def _strip_quoted(body: str) -> str:
    m = QUOTE_START.search(body)
    return (body[: m.start()] if m else body).strip()


def _body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = re.sub(r"<[^>]+>", " ", content)
    return _strip_quoted(content)


def parse_email(raw: bytes) -> dict | None:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if not sender or sender == env("EMAIL_ADDRESS").lower():
        return None
    if msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return None  # never reply to auto-replies / mailing lists (avoids loops)
    return {"channel": "email", "user_id": sender, "text": _body_text(msg)[:4000],
            "msg_id": msg.get("Message-ID") or hashlib.sha1(raw).hexdigest(),
            "name": parseaddr(msg.get("From", ""))[0] or None,
            "meta": {"subject": str(msg.get("Subject", "")), "last_message_id": msg.get("Message-ID")}}


def email_configured() -> bool:
    return bool(env("EMAIL_ADDRESS") and env("EMAIL_PASSWORD"))


def fetch_new_emails() -> list[dict]:
    allowed = {a.strip().lower() for a in env("EMAIL_ALLOWED_SENDERS").split(",") if a.strip()}
    try:
        box = imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com"), timeout=20)
    except OSError as e:
        raise ChannelError(f"IMAP connect failed: {e}") from e
    try:
        try:
            box.login(env("EMAIL_ADDRESS"), env("EMAIL_PASSWORD"))
        except imaplib.IMAP4.error as e:
            db.set_status("email", "error", "IMAP login failed — check app password")
            raise ChannelAuthError(f"IMAP login failed: {e}") from e
        box.select("INBOX")
        _, data = box.search(None, "UNSEEN")
        out = []
        for num in data[0].split():
            _, parts = box.fetch(num, "(BODY.PEEK[])")
            raw = parts[0][1]
            box.store(num, "+FLAGS", "\\Seen")
            item = parse_email(raw)
            if item and (not allowed or item["user_id"] in allowed):
                out.append(item)
        db.set_status("email", "ok", f"Inbox polled ({len(out)} new)")
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass
