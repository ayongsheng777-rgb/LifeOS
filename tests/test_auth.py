"""鉴权相关测试：setup 保护、登录、限流、令牌校验、登出、CSRF 头。"""
import os
import time

from app import auth as auth_mod


def _current_otp():
    secret = auth_mod.get_secret()
    return auth_mod._hotp(secret, int(time.time()) // 30)


def test_setup_open_when_fresh(client):
    r = client.get("/api/auth/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["setup_open"] is True
    assert body["secret"]  # 未绑定期必须能拿到 secret（供首次绑定）


def test_setup_protection_with_token(client):
    os.environ["LIFEOS_SETUP_TOKEN"] = "setup-xyz"
    try:
        # 不带令牌 → 403 要求令牌
        r = client.get("/api/auth/setup")
        assert r.status_code == 403
        assert r.json().get("code") == "SETUP_TOKEN_REQUIRED"
        # 带正确令牌 → 200
        r2 = client.get("/api/auth/setup?token=setup-xyz")
        assert r2.status_code == 200
        assert r2.json()["setup_open"] is True
        # 带错误令牌 → 403
        r3 = client.get("/api/auth/setup?token=wrong")
        assert r3.status_code == 403
    finally:
        os.environ.pop("LIFEOS_SETUP_TOKEN", None)


def test_login_success_and_check(client):
    r = client.post("/api/auth/login", json={"otp": _current_otp()})
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["expires"] > int(time.time())
    # 有效令牌 → auth/check 返 true
    chk = client.get("/api/auth/check", headers={"Authorization": f"Bearer {token}"})
    assert chk.status_code == 200
    assert chk.json()["authed"] is True


def test_login_invalid_otp(client):
    r = client.post("/api/auth/login", json={"otp": "000000"})
    assert r.status_code == 401
    assert r.json().get("code") == "OTP_INVALID"


def test_login_rate_limit(client):
    # 连续错误触发限流：前 5 次 401，第 6 次 429
    statuses = []
    for _ in range(6):
        rr = client.post("/api/auth/login", json={"otp": "000000"})
        statuses.append(rr.status_code)
    assert 401 in statuses
    assert statuses[-1] == 429
    assert statuses.count(429) >= 1


def test_auth_check_invalid_and_forged(client):
    # 无令牌 → false
    r1 = client.get("/api/auth/check")
    assert r1.json()["authed"] is False
    # 伪造令牌 → false
    r2 = client.get("/api/auth/check", headers={"Authorization": "Bearer garbage"})
    assert r2.json()["authed"] is False


def test_logout_revokes(client):
    token = client.post("/api/auth/login", json={"otp": _current_otp()}).json()["token"]
    lo = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert lo.status_code == 200
    chk = client.get("/api/auth/check", headers={"Authorization": f"Bearer {token}"})
    assert chk.json()["authed"] is False  # 已吊销


def test_unauthorized_api_returns_401(client):
    r = client.get("/api/todos")
    assert r.status_code == 401


def test_security_headers_present(client):
    r = client.get("/api/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_health_does_not_leak_urls(client):
    r = client.get("/api/health")
    body = r.json()
    assert "dependencies" in body
    assert "redis_url" not in body
    assert "qdrant_url" not in body
    assert "embedding_model" not in body
