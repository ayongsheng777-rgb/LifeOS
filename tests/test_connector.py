"""Phase 6 Connector 测试：Webhook 鉴权 + 入站路由 + 出站推送降级（全离线，无 PG/飞书/外网依赖）。"""
import pytest
from fastapi.testclient import TestClient

import app.connector as connector_mod
from app.main import app
from app.config import settings

client = TestClient(app)
WEBHOOK_HEADER = {"X-LifeOS-Webhook-Token": "secret-token"}


@pytest.fixture
def with_token():
    settings.connector_webhook_token = "secret-token"
    yield
    settings.connector_webhook_token = ""


def test_webhook_disabled_without_token():
    r = client.post("/api/connector/webhook", json={"type": "todo", "title": "x"})
    assert r.status_code == 503
    assert r.json()["code"] == "WEBHOOK_DISABLED"


def test_webhook_wrong_token(with_token):
    r = client.post("/api/connector/webhook", json={"type": "todo", "title": "x"},
                     headers={"X-LifeOS-Webhook-Token": "bad"})
    assert r.status_code == 401
    assert r.json()["code"] == "WEBHOOK_TOKEN_INVALID"


def test_webhook_todo_routes(with_token, monkeypatch):
    class FakeStore:
        async def add(self, user, data):
            return {"id": "t1", **data}
    monkeypatch.setattr(connector_mod, "PgStore", lambda *a, **k: FakeStore())
    r = client.post("/api/connector/webhook",
                    json={"type": "todo", "title": "买菜", "priority": "high"},
                    headers=WEBHOOK_HEADER)
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["ok"] is True
    assert res["item"]["title"] == "买菜"
    assert res["item"]["done"] is False


def test_webhook_todo_requires_title(with_token, monkeypatch):
    class FakeStore:
        async def add(self, user, data):
            return {"id": "t1", **data}
    monkeypatch.setattr(connector_mod, "PgStore", lambda *a, **k: FakeStore())
    r = client.post("/api/connector/webhook", json={"type": "todo"}, headers=WEBHOOK_HEADER)
    assert r.status_code == 200
    assert r.json()["result"]["ok"] is False


def test_webhook_chat_routes(with_token):
    r = client.post("/api/connector/webhook", json={"type": "chat", "message": "你好"},
                     headers=WEBHOOK_HEADER)
    assert r.status_code == 200
    assert "reply" in r.json()["result"]


def test_webhook_memory_routes(with_token):
    r = client.post("/api/connector/webhook", json={"type": "memory", "content": "记住密码123"},
                     headers=WEBHOOK_HEADER)
    assert r.status_code == 200
    assert r.json()["result"]["ok"] is True


def test_status_endpoint_requires_bearer(monkeypatch):
    monkeypatch.setattr("app.auth.verify_token", lambda t: True)
    r = client.get("/api/connector/status")
    assert r.status_code == 200
    body = r.json()
    assert "webhook_enabled" in body
    assert "inbound_count" in body
    assert "feishu_push" in body


def test_push_feishu_not_configured(monkeypatch):
    monkeypatch.setattr("app.auth.verify_token", lambda t: True)
    settings.feishu_enabled = False
    settings.feishu_app_id = ""
    r = client.post("/api/connector/push",
                    json={"channel": "feishu", "target": "admin", "message": "hi"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_push_unknown_channel_unit():
    import asyncio
    out = asyncio.run(connector_mod.connector.push("sms", "x", "y"))
    assert out["ok"] is False
    assert "unknown channel" in out["error"]


def test_push_http_unit(monkeypatch):
    import asyncio

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(connector_mod.httpx, "AsyncClient", _FakeClient)
    out = asyncio.run(
        connector_mod.connector.push("http", "https://example.com/hook", "hello"))
    assert out["ok"] is True
    assert out["status"] == 200
