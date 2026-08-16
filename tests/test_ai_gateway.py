"""Phase 4 AI Gateway 测试：限速 / 用量埋点 / 健康自检 / 流式端点。

测试进程已隔离外部 PG/Redis（conftest 中 pop 掉 DB_URL/REDIS_URL），
验证：(1) 限速逻辑；(2) 用量在无 DB 时优雅 no-op；(3) 端点结构/降级；
(4) 流式端点 SSE 包装（真实回声路径，无需外部 AI）。
"""
import asyncio

from app.ai.ratelimit import RateLimiter
from app.ai.usage_store import usage_store, estimate_cost
from app.main import app, agent_router


# ===================== 限速器 =====================
def test_ratelimiter_blocks_after_limit():
    rl = RateLimiter(max_requests=3, window_seconds=60)
    assert rl.allow("u") is True
    assert rl.allow("u") is True
    assert rl.allow("u") is True
    assert rl.allow("u") is False  # 第 4 次被拦


def test_ratelimiter_per_user_independent():
    rl = RateLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("a") is True
    assert rl.allow("a") is False
    assert rl.allow("b") is True  # 不同用户独立计数


def test_ratelimiter_reset_after_window(monkeypatch):
    import time as _t
    orig = _t.time  # 先捕获原函数，避免 mock 内递归
    rl = RateLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("u") is True
    assert rl.allow("u") is False
    monkeypatch.setattr(_t, "time", lambda: orig() + 61)  # 时间前进超过窗口
    assert rl.allow("u") is True


# ===================== 用量埋点 =====================
def test_estimate_cost_known_model():
    assert estimate_cost("deepseek-chat", 1000, 1000) > 0


def test_estimate_cost_unknown_model_zero():
    assert estimate_cost("no-such-model-xyz", 1000, 1000) == 0


def test_usage_store_no_db_noop():
    # 测试环境无 DB_URL → record 静默返回 None，summary 返回空结构（不连库、不报错）
    r = asyncio.run(usage_store.record("me", model="x", scenario="chat", ok=True))
    assert r is None
    s = asyncio.run(usage_store.summary("me"))
    assert s["calls"] == 0 and s["per_model"] == {}


# ===================== 端点：健康自检 =====================
def test_ai_probe_structure(client, auth_headers):
    r = client.get("/api/ai/probe", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "models" in data and isinstance(data["models"], list)


# ===================== 端点：用量汇总（无 DB 优雅降级）=====================
def test_ai_usage_empty_when_no_db(client, auth_headers):
    r = client.get("/api/ai/usage", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["calls"] == 0 and data["per_model"] == {}


# ===================== 端点：对话限速 429 =====================
def test_chat_rate_limit_returns_429(client, auth_headers, monkeypatch):
    class _Deny:
        def allow(self, uid):
            return False
    monkeypatch.setattr("app.main.ai_rate_limiter", _Deny())
    r = client.post("/api/agent/chat", json={"message": "你好"}, headers=auth_headers)
    assert r.status_code == 429
    assert r.json().get("code") == "RATE_LIMITED"


# ===================== 端点：流式 SSE（真实回声路径）=====================
def test_chat_stream_sse_echo(client, auth_headers):
    r = client.post("/api/agent/chat/stream", json={"message": "你好"}, headers=auth_headers)
    assert r.status_code == 200
    body = r.text
    assert "data:" in body, "应为 SSE 格式"
    assert "[DONE]" in body, "流结束应有 [DONE] 标记"
    assert "你好" in body  # 回声兜底内容应出现在流中
