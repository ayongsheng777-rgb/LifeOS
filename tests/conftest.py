"""pytest 公共夹具：用独立临时 DATA_DIR 隔离真实数据，避免污染 ./data。

所有测试不依赖外部 Redis / Qdrant（对应模块失败会自动降级），纯 TestClient 跑通。
"""
import os
import tempfile

# 必须在 import app 之前设置，否则会读到真实 ./data
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lifeos_test_")
os.environ.pop("OTP_SECRET", None)        # 确保未绑定态可测 setup
os.environ.pop("LIFEOS_SETUP_TOKEN", None)
# 测试进程禁止连外部 PG / Redis / Qdrant（保持纯本地、确定性、不污染真实数据）。
# 注意：必须设为空字符串而非 pop——config 的 load_dotenv() 在 import 时会从 .env 重新注入，
# 而 dotenv 默认不覆盖已存在的变量，故先用空串占位即可屏蔽 .env 里的值。
os.environ["DB_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["QDRANT_URL"] = ""
os.environ["CONNECTOR_WEBHOOK_TOKEN"] = ""

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
