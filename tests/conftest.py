"""pytest 公共夹具：用独立临时 DATA_DIR 隔离真实数据，避免污染 ./data。

所有测试不依赖外部 Redis / Qdrant（对应模块失败会自动降级），纯 TestClient 跑通。
"""
import os
import tempfile

# 必须在 import app 之前设置，否则会读到真实 ./data
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lifeos_test_")
os.environ.pop("OTP_SECRET", None)        # 确保未绑定态可测 setup
os.environ.pop("LIFEOS_SETUP_TOKEN", None)

import pytest
from fastapi.testclient import TestClient
from app.main import app
import app.auth as auth_mod
import app.main as main_mod


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """登录拿一个有效 Bearer 令牌，供需要鉴权的接口测试。"""
    import time
    code = auth_mod._hotp(auth_mod.get_secret(), int(time.time()) // 30)
    r = TestClient(app).post("/api/auth/login", json={"otp": code})
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def fresh_state():
    """每个测试前重置绑定态/令牌/登录限流，保证互相独立。"""
    for fn in ("otp_secret", "otp_enrolled"):
        p = os.path.join(os.environ["DATA_DIR"], fn)
        if os.path.exists(p):
            os.remove(p)
    auth_mod.VALID_TOKENS.clear()
    main_mod._login_fail.clear()
    yield
