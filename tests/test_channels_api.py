import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import channels, db, engine
from app.main import app


WA = {"entry": [{"changes": [{"value": {
    "contacts": [{"wa_id": "447700900001", "profile": {"name": "Asha"}}],
    "messages": [{"from": "447700900001", "id": "wamid.1", "type": "text", "text": {"body": "Hi there"}}]}}]}]}
IG = {"object": "instagram", "entry": [{"messaging": [
    {"sender": {"id": "IGSID1"}, "recipient": {"id": "ME"}, "message": {"mid": "m1", "text": "hello"}},
    {"sender": {"id": "ME"}, "recipient": {"id": "IGSID1"}, "message": {"mid": "m2", "text": "echo", "is_echo": True}}]}]}


def test_parse_whatsapp_and_instagram():
    wa = channels.parse_whatsapp(WA)
    assert wa == [{"channel": "whatsapp", "user_id": "447700900001", "text": "Hi there", "msg_id": "wamid.1", "name": "Asha"}]
    ig = channels.parse_instagram(IG)
    assert len(ig) == 1 and ig[0]["user_id"] == "IGSID1"


def test_parse_email_strips_quotes_and_skips_autoreplies(monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", "bot@test.com")
    raw = (b"From: Asha <asha@test.com>\r\nTo: bot@test.com\r\nSubject: Dashboards\r\nMessage-ID: <a1@test>\r\n\r\n"
           b"Budget is 20k\r\n\r\nOn Mon, Bot wrote:\r\n> old text\r\n")
    e = channels.parse_email(raw)
    assert e["user_id"] == "asha@test.com" and e["text"] == "Budget is 20k" and e["meta"]["subject"] == "Dashboards"
    auto = raw.replace(b"Subject:", b"Auto-Submitted: auto-replied\r\nSubject:")
    assert channels.parse_email(auto) is None


def test_webhook_verify_and_signature(monkeypatch, env):
    monkeypatch.setenv("META_VERIFY_TOKEN", "tok")
    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    llm, cal, sent = env
    llm.queue(fields={})
    with TestClient(app) as c:
        r = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"})
        assert r.text == "42"
        assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403
        body = json.dumps(WA).encode()
        assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
        state = c.get("/api/state").json()
        assert state["leads"][0]["channel"] == "whatsapp"


def test_dashboard_requires_password(monkeypatch, env):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", auth=("demo", "pw")).status_code == 200
        assert c.get("/healthz").status_code == 200


def test_policy_editor_validates(monkeypatch, env):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with TestClient(app) as c:
        text = c.get("/api/policy").text
        assert c.put("/api/policy", content=text.replace("min_budget: 5000", "min_budget: 8000")).status_code == 200
        assert "8000" in c.get("/api/policy").text
        assert c.put("/api/policy", content="company: [broken").status_code == 400
