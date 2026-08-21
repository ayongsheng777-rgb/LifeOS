"""OTP（TOTP）验证模块 —— 纯标准库实现，零三方依赖。

对齐《复刻指导 03》：
- 动态码：RFC 6238 TOTP（HMAC-SHA1，30s 步长，6 位码，±1 步长容忍漂移）
- 会话令牌：HMAC-SHA256 签名无状态令牌，签发后放内存有效集（重启即失效）
- 密钥管理：env 优先 > 落盘文件 > 自动生成（首次绑定后锁死展示）
- 守卫：FastAPI Bearer 中间件 + WebSocket ?token= 校验

不使用 pyotp；兼容 Google Authenticator / 1Password / Authy。
"""
import os
import re
import time
import json
import base64
import struct
import hmac
import hashlib
import secrets
import string
from urllib.parse import quote

import segno

# ===== 常量与文件 =====
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(_PROJECT_ROOT, "data")
if not os.path.isabs(DATA_DIR):
    DATA_DIR = os.path.join(_PROJECT_ROOT, DATA_DIR)
OTP_ISSUER = os.environ.get("OTP_ISSUER", "丽素")
OTP_ACCOUNT = os.environ.get("OTP_ACCOUNT", "admin@lifeos")
SESSION_TTL = int(os.environ.get("SESSION_TTL", "43200"))  # 12h

_SECRET_FILE = os.path.join(DATA_DIR, "otp_secret")
_ENROLLED_FILE = os.path.join(DATA_DIR, "otp_enrolled")
_SESSION_FILE = os.path.join(DATA_DIR, "session_secret")

VALID_TOKENS = set()  # 内存有效令牌集（重启清空 = 设计内）

# TOTP 防重放：记已用动态码，时间窗内不可复用
_used_otp: dict = {}

_PLACEHOLDER = "****"
_B32_ALPHABET = set(string.ascii_uppercase + string.digits + "=")


# ===== TOTP 密钥生命周期 =====
def get_secret() -> str:
    """env OTP_SECRET > 落盘文件 > 自动生成20字节随机数并落盘。"""
    env_secret = os.environ.get("OTP_SECRET", "").strip()
    if env_secret:
        return env_secret
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    with open(_SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(secret)
    return secret


def is_setup_open() -> bool:
    """仅当【无 env 密钥】且【无 enrolled 标记】时开放密钥展示。"""
    return not os.environ.get("OTP_SECRET") and not os.path.exists(_ENROLLED_FILE)


def mark_enrolled() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_ENROLLED_FILE, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())))


# ===== otpauth URI（★ 必须百分号编码）=====
def _encode_label(text: str) -> str:
    return quote(text, safe="")


def otpauth_uri() -> str:
    secret = get_secret()
    return (f"otpauth://totp/{_encode_label(OTP_ISSUER)}?secret={secret}"
            f"&issuer={_encode_label(OTP_ISSUER)}&algorithm=SHA1&digits=6&period=30")


def qr_data_uri() -> str:
    """本地用 segno 渲染 otpauth_uri 为 SVG data URI；绝不用在线二维码服务。"""
    qr = segno.make(otpauth_uri(), error="m")
    return qr.svg_data_uri()


# ===== HOTP / TOTP 核心（RFC 6238）=====
def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))  # 补填充
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF  # 动态截断
    return str(code % 10 ** 6).zfill(6)


def verify_otp(code: str, window: int = 1) -> bool:
    """±window 个时间步长（±30s）容忍时钟漂移。"""
    if len(code) != 6 or not code.isdigit():
        return False
    secret = get_secret()
    now = int(time.time())
    return any(_hotp(secret, (now + w * 30) // 30) == code
               for w in range(-window, window + 1))


# ===== 会话令牌（HMAC 签名）=====
def _get_session_secret() -> str:
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env:
        return env
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(_SESSION_FILE):
        with open(_SESSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    secret = secrets.token_hex(32)  # 64 hex
    with open(_SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(secret)
    return secret


def _b64url(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")


def _hmac_sha256(key: str, body: str) -> str:
    return hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


def generate_token() -> dict:
    issued = int(time.time())
    expiry = issued + SESSION_TTL
    body = _b64url(f"{issued}.{expiry}")
    sig = _hmac_sha256(_get_session_secret(), body)
    token = f"{body}.{sig}"
    VALID_TOKENS.add(token)  # ★ 内存有效集：验签之外还要在集合内
    return {"token": token, "expires": expiry, "ttl": SESSION_TTL}


def verify_token(token: str) -> bool:
    if not token or token not in VALID_TOKENS:
        return False
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return False
    expected = _hmac_sha256(_get_session_secret(), body)
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        raw = base64.urlsafe_b64decode(body + "=" * ((4 - len(body) % 4) % 4)).decode()
        _, expiry = raw.split(".")
        if int(expiry) < int(time.time()):
            VALID_TOKENS.discard(token)  # 过期顺手剔除
            return False
    except Exception:
        return False
    return True


def revoke_token(token: str) -> None:
    VALID_TOKENS.discard(token)


# ===== OTP 重置（换绑）=====
def reset_otp() -> dict | None:
    if os.environ.get("OTP_SECRET"):
        return None  # 固定密钥模式不可在线重置
    # 删 otp_secret + otp_enrolled → 清空 VALID_TOKENS → 生成新密钥落盘
    for p in (_SECRET_FILE, _ENROLLED_FILE):
        if os.path.exists(p):
            os.remove(p)
    VALID_TOKENS.clear()
    secret = get_secret()
    return {
        "secret": secret,
        "otpauth_uri": otpauth_uri(),
        "qr_data_uri": qr_data_uri(),
        "account": OTP_ACCOUNT,
        "issuer": OTP_ISSUER,
    }


# ===== 对外接口（供 main.py 端点调用）=====
def setup_info() -> dict:
    if not is_setup_open():
        return {"setup_open": False}
    return {
        "setup_open": True,
        "secret": get_secret(),
        "otpauth_uri": otpauth_uri(),
        "qr_data_uri": qr_data_uri(),
        "account": OTP_ACCOUNT,
        "issuer": OTP_ISSUER,
    }


def login(otp: str) -> dict | None:
    otp = otp or ""
    now = time.time()
    # 清理过期记录（时间窗外的码可复用）
    for _c in [c for c, t in _used_otp.items() if t <= now]:
        _used_otp.pop(_c, None)
    if otp in _used_otp:
        return None  # 时间窗内同码重放拒绝
    if not verify_otp(otp):
        return None
    _used_otp[otp] = now + 60  # 60 秒内同码不可二次登录
    mark_enrolled()
    return generate_token()


def check() -> dict:
    return {"authed": False, "setup_open": is_setup_open()}


def logout(token: str) -> bool:
    revoke_token(token)
    return True


def otp_reset(otp: str) -> dict | None:
    """必须同时持有会话令牌 + 当前动态码（安全门）。"""
    if not verify_otp(otp or ""):
        return None
    return reset_otp()
