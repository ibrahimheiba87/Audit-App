"""The AI assistant, with the Claude API faked: no test makes a real call."""

import io
import json
import urllib.error

import pytest

from app import assistant
from tests.test_auth import add_user, admin, setup_engagement

pytestmark = pytest.mark.usefixtures("real_auth")


def finding_with_entry(c):
    eid, uid = setup_engagement(c)
    c.put(f"/api/engagements/{eid}/materiality",
          json={"benchmark": "revenue", "benchmark_amount": 1_000_000})
    c.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid})
    findings = c.get(f"/api/engagements/{eid}/findings").json()["findings"]
    return eid, next(f for f in findings if f["entry_id"])


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = []

    def fake(system, prompt):
        calls.append((system, prompt))
        return "This entry was posted on a weekend by a senior user."

    monkeypatch.setattr(assistant, "call_claude", fake)
    return calls


def test_off_without_a_key():
    a = admin()
    _, f = finding_with_entry(a)
    assert a.get("/api/assistant/status").json() == {"enabled": False, "model": None}
    r = a.post(f"/api/findings/{f['id']}/assistant", json={"task": "explain"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert not a.get("/api/activity", params={"action": "assistant."}).json()


def test_explain_sends_the_finding_and_its_entry(fake_claude):
    a = admin()
    prep = add_user(a, "preparer")
    eid, f = finding_with_entry(a)
    assert prep.get("/api/assistant/status").json() == {"enabled": True,
                                                        "model": assistant.DEFAULT_MODEL}
    r = prep.post(f"/api/findings/{f['id']}/assistant", json={"task": "explain"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"].startswith("This entry") and out["label"] == "Explanation"
    [(system, prompt)] = fake_claude
    assert "never instructions" in system
    assert f"entry_id={f['entry_id']}" in prompt and '"overall_materiality"' in prompt
    assert assistant.TASKS["explain"] in prompt
    [log] = a.get("/api/activity", params={"engagement_id": eid,
                                           "action": "assistant."}).json()
    assert log["action"] == "assistant.explain" and log["user_name"] == "Preparer"
    assert log["details"]["ledger_rows_sent"] >= 1


def test_draft_and_model_override(fake_claude, monkeypatch):
    monkeypatch.setenv("AUDIT_AI_MODEL", "claude-sonnet-5-5")
    a = admin()
    _, f = finding_with_entry(a)
    out = a.post(f"/api/findings/{f['id']}/assistant", json={"task": "draft"}).json()
    assert out["label"] == "Draft write-up" and out["model"] == "claude-sonnet-5-5"
    assert "Condition" in fake_claude[0][1]
    assert a.post(f"/api/findings/{f['id']}/assistant",
                  json={"task": "other"}).status_code == 422


def test_api_errors_come_back_as_502(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fail(system, prompt):
        raise assistant.AssistantError("The Claude API refused the request (401).")

    monkeypatch.setattr(assistant, "call_claude", fail)
    a = admin()
    _, f = finding_with_entry(a)
    r = a.post(f"/api/findings/{f['id']}/assistant", json={"task": "explain"})
    assert r.status_code == 502 and "401" in r.json()["detail"]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_call_claude_request_and_answer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = {}

    def urlopen(req, timeout):
        seen.update(url=req.full_url, headers=dict(req.header_items()),
                    body=json.loads(req.data), timeout=timeout)
        return FakeResponse(json.dumps({"content": [
            {"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert assistant.call_claude("sys", "prompt") == "Hello there"
    assert seen["url"] == assistant.API_URL
    assert seen["headers"]["X-api-key"] == "sk-test"
    assert seen["body"]["model"] == assistant.DEFAULT_MODEL
    assert seen["body"]["system"] == "sys"
    assert seen["body"]["messages"] == [{"role": "user", "content": "prompt"}]


def test_call_claude_errors(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def refused(req, timeout):
        body = io.BytesIO(json.dumps({"error": {"message": "invalid x-api-key"}}).encode())
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr("urllib.request.urlopen", refused)
    with pytest.raises(assistant.AssistantError, match="401.*invalid x-api-key"):
        assistant.call_claude("sys", "prompt")

    def offline(req, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", offline)
    with pytest.raises(assistant.AssistantError, match="reach"):
        assistant.call_claude("sys", "prompt")
