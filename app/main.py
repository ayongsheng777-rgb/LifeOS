"""LifeOS 核心入口：FastAPI 网关 + OTP 鉴权中间件 + 飞书/OTP 端点 + lifespan。

- 所有 /api/* 受 Bearer 鉴权保护（白名单：/api/health、/api/auth/*）
- 飞书消息（WS 长连接）最终路由到 AgentRouter 并回转
- OTP（TOTP + 会话令牌）按《复刻指导 03》实现，纯标准库
"""
import os
import re
import json
import asyncio
import time
import hmac
import httpx
import datetime
import logging
import threading
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

from app import auth
from app.config import settings, mask_secret, AIProfile, _is_valid_key
from app.ai import client as ai_client
from app.ai import model_presets
from app.ai.usage_store import usage_store
from app.ai.ratelimit import ai_rate_limiter
from app.feishu import (bot, start_bot, stop_bot, bot_status, get_news)
from app.feishu_deviceflow import FeishuDeviceFlow
from app.agent.router import agent_router, MessagePayload
from app.skills.api_skill import (build_api_skills_into, write_skill_package,
                                  delete_skill_package, sanitize_skill_name,
                                  ApiSkillHandler)
from app.skills.db_store import PgStore, init_db, remember_fact, list_facts, delete_fact
from app.connector import connector
from app.backup import (run_backup_all, start_backup_scheduler, stop_backup_scheduler,
                      is_scheduler_running, list_backup_points, run_restore, VALID_COMPONENTS,
                      normalize_target)

# ===== 备份实时日志收集（供 Web 面板 /api/backup/log 轮询）=====
_logger = logging.getLogger("lifeos.main")

_backup_log_buf = deque(maxlen=1000)
_backup_log_lock = threading.Lock()
_backup_log_seq = {"n": 0}


class _BackupLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with _backup_log_lock:
            _backup_log_seq["n"] += 1
            _backup_log_buf.append({
                "id": _backup_log_seq["n"],
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "level": record.levelname,
                "msg": msg,
            })


_bk_logger = logging.getLogger("lifeos.backup")
_bk_handler = _BackupLogHandler()
_bk_handler.setFormatter(logging.Formatter("%(message)s"))
_bk_logger.addHandler(_bk_handler)


def restart_backup_scheduler():
    """停止旧的备份定时调度线程，用最新 settings 重新拉起（配置热更新后调用）。"""
    stop_backup_scheduler()
    time.sleep(0.3)  # 等待旧线程退出
    targets = [t for t in (settings.backup_targets or []) if t.get("enabled") and t.get("path")]
    if targets:
        start_backup_scheduler(targets, settings.backup_retention_days, settings.data_dir,
                               settings.backup_schedule_hour)
        _logger.info("备份定时调度已按新配置重启（%d 个启用目标，保留=%d天，定时=%02d:00）",
                     len(targets), settings.backup_retention_days, settings.backup_schedule_hour)


# ===== 备份目标辅助（归一化 / 密码遮罩 / 校验 / 按 path 定位）=====
def _mask_targets(targets):
    """返回带密码遮罩的目标列表（UI 用 type=password，不回传明文）。"""
    out = []
    for t in (targets or []):
        n = normalize_target(t)
        if n.get("password"):
            n = dict(n)
            n["password"] = "****"
        out.append(n)
    return out


def _resolve_target(identifier):
    """按归一化 path 在已配置目标中定位并返回归一化目标（供 points/restore 使用）。"""
    for t in (settings.backup_targets or []):
        n = normalize_target(t)
        if n["path"] == identifier:
            return n
    return None


def _validate_target(n):
    """按传输方式校验目标必填字段，返回错误字符串或 None。"""
    m = n["method"]
    if m == "local":
        if not n["path"]:
            return "本地目标需填写路径"
    elif m == "smb":
        if not n["host"] or not n["share"]:
            return "SMB 需填写主机与共享名"
        if not n["directory"]:
            return "SMB 需填写远程目录"
    elif m in ("sftp", "ftp", "webdav"):
        if not n["host"]:
            return f"{m.upper()} 需填写主机"
        if not n["directory"]:
            return f"{m.upper()} 需填写远程目录"
    return None


# ===== 鉴权白名单（03-OTP：勿扩大）=====
PUBLIC_EXACT = {"/api/health", "/api/connector/webhook"}
PUBLIC_PREFIXES = ("/api/auth/",)


# ===== 登录暴力破解防护（P0-02，单进程内存限流；Redis 起后可升级为共享限流）=====
_login_fail: dict = {}  # ip -> {"count": int, "lock_until": float}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_allowed(ip: str) -> bool:
    rec = _login_fail.get(ip)
    if not rec:
        return True
    return rec["lock_until"] <= time.time()


def _login_fail_inc(ip: str) -> None:
    rec = _login_fail.get(ip, {"count": 0, "lock_until": 0.0})
    rec["count"] += 1
    # 5 次→锁 30s；10 次→锁 300s；20+ 次→锁 1800s（指数退避）
    if rec["count"] >= 20:
        rec["lock_until"] = time.time() + 1800
    elif rec["count"] >= 10:
        rec["lock_until"] = time.time() + 300
    elif rec["count"] >= 5:
        rec["lock_until"] = time.time() + 30
    _login_fail[ip] = rec


def _login_fail_reset(ip: str) -> None:
    _login_fail.pop(ip, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：PostgreSQL 表就绪（DB_URL 未配置则跳过）
    await init_db()
    # 启动：若飞书已启用且有凭据，拉起 WS Bot
    loop = asyncio.get_running_loop()
    if settings.feishu_enabled and settings.feishu_app_id and settings.feishu_app_secret:
        start_bot(loop)
    # 启动：数据突变备份定时调度（每日 backup_schedule_hour，多目标：本地+NAS）
    targets = [t for t in (settings.backup_targets or []) if t.get("enabled") and t.get("path")]
    if targets:
        start_backup_scheduler(targets, settings.backup_retention_days, settings.data_dir,
                               settings.backup_schedule_hour)
    yield
    # 关停
    stop_backup_scheduler()
    await stop_bot()


app = FastAPI(title="Chufeng LifeOS", version="2.0", lifespan=lifespan)


# ===== 鉴权中间件 =====
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    # CORS 预检直接放行（真正加头由 CORSMiddleware 完成，见下方 add_middleware）
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    # 非 /api 路径（前端 SPA 静态资源）一律放行，不进鉴权
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if auth.verify_token(token):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"error": "未授权，请先登录", "code": "AUTH_REQUIRED"},
        headers={"WWW-Authenticate": "Bearer"},
    )


# ===== 安全响应头（P0-03：CSP 等，降低 XSS/点击劫持面）=====
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; object-src 'none'; frame-ancestors 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# ===== CORS（放在 auth_guard 之后注册，使其为最外层，优先处理预检）=====
# 同源（SPA 由本服务托管）无需跨域；以下为本地开发（vite :5173）与隧道域名放行
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:7208",
        "http://127.0.0.1:7208",
        "https://lifeos.yshost.de5.net",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 健康检查 =====
@app.get("/api/health")
async def health():
    # #30 信息脱敏：不暴露内部基础设施地址，仅返回依赖可用性摘要
    return {
        "status": "ok",
        "ai_available": ai_client.available(),
        "feishu": bot_status(),
        "dependencies": {
            "redis": "configured" if settings.redis_url else "not_configured",
            "qdrant": "configured" if settings.qdrant_url else "not_configured",
            "embedding": "configured" if settings.embedding_model else "not_configured",
        },
        "connector": {
            "webhook": "configured" if settings.connector_webhook_token else "not_configured",
            "feishu_push": bool(settings.feishu_enabled and settings.feishu_app_id),
        },
    }


@app.get("/api/status")
async def status_view():
    """系统状态（设置页展示用）：依赖可用性 + AI/飞书/连接器状态。与 /api/health 同源。"""
    return await health()


# ===== OTP 端点（03-OTP）=====
class LoginReq(BaseModel):
    otp: str


class ResetReq(BaseModel):
    otp: str


@app.get("/api/auth/setup")
async def auth_setup(request: Request):
    if not auth.is_setup_open():
        return JSONResponse(status_code=403, content={"setup_open": False})
    # P0-01：设了 LIFEOS_SETUP_TOKEN 后，未绑定期必须带令牌，禁止公网匿名拿 secret
    setup_token = os.environ.get("LIFEOS_SETUP_TOKEN", "")
    if setup_token:
        provided = request.query_params.get("token", "") or request.headers.get("X-Setup-Token", "")
        if not hmac.compare_digest(provided, setup_token):
            return JSONResponse(status_code=403,
                                content={"error": "需要有效的初始化令牌", "code": "SETUP_TOKEN_REQUIRED"})
    return auth.setup_info()


@app.post("/api/auth/login")
async def auth_login(req: LoginReq, request: Request):
    ip = _client_ip(request)
    if not _login_allowed(ip):
        return JSONResponse(status_code=429,
                            content={"error": "尝试过于频繁，请稍后再试", "code": "LOGIN_LOCKED"},
                            headers={"Retry-After": "30"})
    result = auth.login(req.otp)
    if not result:
        _login_fail_inc(ip)
        return JSONResponse(status_code=401, content={"error": "动态码错误", "code": "OTP_INVALID"})
    _login_fail_reset(ip)
    return result


@app.get("/api/auth/check")
async def auth_check(authorization: Optional[str] = Header(None)):
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    authed = bool(token) and auth.verify_token(token)
    return {"authed": authed, "setup_open": auth.is_setup_open()}


@app.post("/api/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(None)):
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    auth.logout(token)
    return {"ok": True}


@app.post("/api/auth/otp-reset")
async def auth_otp_reset(req: ResetReq):
    # 已通过 auth_guard 校验 Bearer；此处还需当前动态码（安全门）
    result = auth.otp_reset(req.otp)
    if result is None:
        # 可能是固定密钥模式拒绝，或动态码错误
        if os.environ.get("OTP_SECRET"):
            return JSONResponse(status_code=403, content={"error": "固定密钥模式下不可在线重置", "code": "RESET_DENIED"})
        return JSONResponse(status_code=401, content={"error": "动态码错误", "code": "OTP_INVALID"})
    return result


# ===== 飞书端点（02-飞书，均受 Bearer 保护）=====
@app.post("/api/feishu/qrcode")
async def feishu_qrcode():
    flow = FeishuDeviceFlow()
    result = await flow.start()
    if result.get("status") != "ok":
        return JSONResponse(status_code=400, content={"error": result.get("reason", "发起授权失败")})
    return {
        "scan_url": result["scan_url"],
        "poll_token": result["poll_token"],
        "expires_in": result.get("expires_in", 300),
    }


@app.get("/api/feishu/qrcode/status")
async def feishu_qrcode_status(token: str = ""):
    flow = FeishuDeviceFlow()
    res = await flow.poll(token)
    if res.get("status") == "success":
        settings.upsert_setting({
            "feishu_app_id": res["app_id"],
            "feishu_app_secret": res["app_secret"],
            "feishu_enabled": True,
        })
        settings.apply_overrides(feishu_enabled=True)
        loop = asyncio.get_running_loop()
        start_bot(loop)
        return {"status": "success", "feishu_enabled": True}
    return res  # pending / expired / denied / error


@app.get("/api/feishu/status")
async def feishu_status():
    return bot_status()


@app.post("/api/feishu/bot-start")
async def feishu_bot_start():
    if not (settings.feishu_app_id and settings.feishu_app_secret):
        return JSONResponse(status_code=400, content={"error": "飞书凭据未配置"})
    loop = asyncio.get_running_loop()
    start_bot(loop)
    return {"ok": True, "online": bot.is_online()}


@app.post("/api/feishu/disconnect")
async def feishu_disconnect():
    settings.upsert_setting({"feishu_enabled": False})
    settings.apply_overrides(feishu_enabled=False)
    await stop_bot()
    return {"ok": True, "feishu_enabled": False}


@app.get("/api/feishu/news")
async def feishu_news():
    return {"count": len(get_news()), "items": get_news()[-20:]}


# ===== 记忆管理端点（受 Bearer 保护）=====
# 三层记忆：工作(进程内) / 短期(Redis) / 长期(Qdrant)。
# 长期记忆若未配置/不可用，优雅返回 configured:false，不报错。
@app.get("/api/memory/short")
async def memory_short():
    uid = DEFAULT_USER
    return {
        "working": agent_router.memory.get_working(uid),
        "short": agent_router.memory.get_short(uid),
    }


@app.delete("/api/memory/short")
async def memory_short_clear():
    uid = DEFAULT_USER
    agent_router.memory.clear_short(uid)
    agent_router.memory.clear_working(uid)
    return {"ok": True}


@app.get("/api/memory/long")
async def memory_long(limit: int = 50):
    items = agent_router.memory.list_long(DEFAULT_USER, limit=limit)
    if items is None:
        return {"configured": False, "items": []}
    return {"configured": True, "items": items}


@app.delete("/api/memory/long")
async def memory_long_delete(id: str = Query("", description="要删除的长期经验点 ID")):
    if not id:
        return JSONResponse(status_code=400, content={"error": "需提供 id 参数（要删除的长期经验点 ID）"})
    ok = agent_router.memory.delete_long(id)
    if not ok:
        return JSONResponse(status_code=503, content={"error": "长期记忆未配置/不可用"})
    return {"ok": True}


# ===== 个人长期事实库（永久记忆 A）端点 =====
class FactReq(BaseModel):
    key: str
    value: str
    category: str = "通用"
    source: str = "manual"


@app.post("/api/memory/fact")
async def memory_fact_add(req: FactReq):
    try:
        fact = await remember_fact(DEFAULT_USER, req.key, req.value,
                                   category=req.category, source=req.source)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": f"保存失败：{e}"})
    return {"ok": True, "fact": fact}


@app.get("/api/memory/facts")
async def memory_facts(limit: int = Query(50, ge=1, le=200)):
    try:
        items = await list_facts(DEFAULT_USER, limit=limit)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": f"读取失败：{e}"})
    return {"facts": items}


@app.delete("/api/memory/fact/{fact_id}")
async def memory_fact_del(fact_id: str):
    try:
        ok = await delete_fact(DEFAULT_USER, fact_id)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": f"删除失败：{e}"})
    return {"ok": ok}


# ===== 调试/本地入口：直接对话 Agent（受 Bearer 保护）=====
class ChatReq(BaseModel):
    message: str


class ConnectorPushReq(BaseModel):
    channel: str            # "feishu" | "http"
    target: str = ""        # feishu: "admin" 或 open_id；http: 完整 URL
    message: str


@app.post("/api/agent/chat")
async def agent_chat(req: ChatReq):
    # Phase 4：场景限速（进程内滑动窗口，按用户）
    if not ai_rate_limiter.allow(DEFAULT_USER):
        return JSONResponse(status_code=429,
                            content={"error": "请求过于频繁，请稍后再试", "code": "RATE_LIMITED"},
                            headers={"Retry-After": "30"})
    # P0-04：外部 REST 不再接受客户端自填 user_id，统一用单用户身份 "me"
    reply = await agent_router.process_message(
        MessagePayload(user_id="me", message=req.message, source="debug",
                       time=str(int(time.time()))))
    return {"reply": reply}


@app.get("/api/agent/history")
async def agent_history():
    """返回当前用户的对话历史（短期记忆），供前端重新进入时自动恢复。

    短期记忆存储格式为 {"role": "user"|"assistant", "content": str, "time": float}，
    此处统一转换为前端友好的 {role, text}（assistant → ai）。
    """
    items = agent_router.memory.get_short(DEFAULT_USER)
    messages = []
    for it in items:
        role = "user" if it.get("role") == "user" else "ai"
        text = it.get("content") or it.get("text") or ""
        if text:
            messages.append({"role": role, "text": text})
    return {"messages": messages}


# ===== 数据突变备份（多目标：本地磁盘 + 多个 NAS 挂载点）=====
@app.post("/api/backup/run")
async def backup_run():
    """手动触发一次全目标备份（所有启用目标）。数据导出在后台线程执行，不阻塞请求。"""
    targets = [t for t in (settings.backup_targets or []) if t.get("enabled") and t.get("path")]
    if not targets:
        return JSONResponse(status_code=400,
                            content={"error": "未配置任何启用的备份目标"})
    summary = await asyncio.to_thread(
        run_backup_all, targets, settings.backup_retention_days, settings.data_dir,
    )
    return summary


@app.get("/api/backup/status")
async def backup_status():
    """返回上次备份状态（各目标 manifest 概要）、当前配置。"""
    targets = settings.backup_targets or []
    cache_dir = os.path.join(settings.data_dir, "backup_status")
    out = []
    for t in targets:
        n = normalize_target(t)
        entry = dict(n)
        if entry.get("password"):
            entry["password"] = "****"
        entry["last_backup"] = None
        entry["results"] = None
        st = os.path.join(cache_dir, re.sub(r'[^A-Za-z0-9._-]', '_', n["path"]) + ".json")
        if os.path.exists(st):
            try:
                with open(st, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                entry["last_backup"] = loaded.get("last_backup")
                entry["results"] = loaded.get("results")
            except Exception:
                pass
        out.append(entry)
    return {
        "targets": out,
        "backup_targets": _mask_targets(targets),
        "retention_days": settings.backup_retention_days,
        "schedule_hour": settings.backup_schedule_hour,
        "scheduler_running": is_scheduler_running(),
    }


# ===== 备份配置读写（Web 面板在线编辑，runtime.json 落库，热重启调度）=====
@app.get("/api/backup/config")
async def backup_config_view():
    """返回当前备份配置（供面板编辑表单初始化）。密码以 **** 遮罩。"""
    return {
        "backup_targets": _mask_targets(settings.backup_targets),
        "backup_retention_days": settings.backup_retention_days,
        "backup_schedule_hour": settings.backup_schedule_hour,
    }


class BackupConfigReq(BaseModel):
    backup_targets: Optional[list] = None
    backup_retention_days: Optional[int] = None
    backup_schedule_hour: Optional[int] = None


@app.post("/api/backup/config")
async def backup_config_update(req: BackupConfigReq):
    """在线更新备份配置（多目标列表，含 SMB/SFTP/FTP/WebDAV）：落库并热重启定时调度。"""
    patch = {}
    if req.backup_targets is not None:
        if not isinstance(req.backup_targets, list):
            return JSONResponse(status_code=400, content={"error": "备份目标必须是数组"})
        # 已存目标按归一化 path 建索引，供密码占位符时找回原密码
        existing = {normalize_target(t)["path"]: t for t in (settings.backup_targets or [])}
        tgs = []
        for i, t in enumerate(req.backup_targets):
            if not isinstance(t, dict):
                return JSONResponse(status_code=400, content={"error": f"第 {i + 1} 个目标格式错误"})
            # 密码占位符 ****：保留已存密码，不覆盖
            pw = t.get("password")
            if isinstance(pw, str) and pw.startswith("****"):
                cand = normalize_target({k: v for k, v in t.items() if k != "password"})
                prev = existing.get(cand["path"])
                if prev:
                    t = dict(t)
                    t["password"] = prev.get("password", "")
            try:
                n = normalize_target(t)
            except Exception as e:
                return JSONResponse(status_code=400, content={"error": f"第 {i + 1} 个目标解析失败：{e}"})
            err = _validate_target(n)
            if err:
                return JSONResponse(status_code=400, content={"error": f"第 {i + 1} 个目标：{err}"})
            if not n["path"]:
                return JSONResponse(status_code=400, content={"error": f"第 {i + 1} 个目标路径为空"})
            tgs.append(n)
        if not tgs:
            return JSONResponse(status_code=400, content={"error": "至少需要一个备份目标"})
        patch["backup_targets"] = tgs
    if req.backup_retention_days is not None:
        try:
            d = int(req.backup_retention_days)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": "保留天数必须是数字"})
        if d < 1 or d > 365:
            return JSONResponse(status_code=400, content={"error": "保留天数需在 1-365 之间"})
        patch["backup_retention_days"] = d
    if req.backup_schedule_hour is not None:
        try:
            h = int(req.backup_schedule_hour)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": "定时小时必须是数字"})
        if h < 0 or h > 23:
            return JSONResponse(status_code=400, content={"error": "定时小时需在 0-23 之间"})
        patch["backup_schedule_hour"] = h
    if not patch:
        return JSONResponse(status_code=400, content={"error": "无可更新字段"})
    settings.upsert_setting(patch)  # 内部会再次归一化（幂等）
    restart_backup_scheduler()
    return {"ok": True, "backup_targets": _mask_targets(patch["backup_targets"]),
            "backup_retention_days": getattr(settings, "backup_retention_days", None),
            "backup_schedule_hour": getattr(settings, "backup_schedule_hour", None)}


@app.get("/api/backup/points")
async def backup_points(target: str = Query("", description="备份目标 path（归一化后的稳定主键）")):
    """列出某目标下所有可用备份时间点（含各组件可用性）。远程目标经传输层读取。"""
    if not target:
        return JSONResponse(status_code=400, content={"error": "需提供 target 参数"})
    n = _resolve_target(target)
    if not n:
        return JSONResponse(status_code=400, content={"error": "该目标未配置"})
    points = await asyncio.to_thread(list_backup_points, n, settings.data_dir)
    return {"target": target, "points": points}


class BackupRestoreReq(BaseModel):
    target: str
    timestamp: str
    components: list   # 子集：["postgres","redis","qdrant","data"]


@app.post("/api/backup/restore")
async def backup_restore(req: BackupRestoreReq):
    """还原指定目标/时间点的部分或全部组件（危险操作，前端二次确认后调用）。"""
    n = _resolve_target(req.target)
    if not n:
        return JSONResponse(status_code=400, content={"error": "该目标未配置"})
    comps = [c for c in (req.components or []) if c in VALID_COMPONENTS]
    if not comps:
        return JSONResponse(status_code=400, content={"error": "未选择有效还原组件"})
    if not req.timestamp:
        return JSONResponse(status_code=400, content={"error": "未指定备份时间点"})
    try:
        summary = await asyncio.to_thread(
            run_restore, n, req.timestamp, comps, settings.data_dir)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"还原失败：{e}"})
    return summary


@app.get("/api/backup/log")
async def backup_log_view(since: int = 0):
    """轮询备份实时日志；since 为上一条日志 id，返回其后的新日志。"""
    with _backup_log_lock:
        items = [x for x in _backup_log_buf if x["id"] > since]
        last_id = _backup_log_buf[-1]["id"] if _backup_log_buf else 0
    return {"logs": items, "last_id": last_id}


# ===== AI Gateway：模型健康自检 + 用量统计 + 流式对话 =====
@app.get("/api/ai/probe")
async def ai_probe():
    """逐模型连通性自检（真实打模型，返回每个模型的 ok/latency）。"""
    profs = []
    active = settings.active_ai_profile()
    if active:
        profs.append(active)
    for m in settings.ai_models:
        if m.get("api_key"):
            profs.append(AIProfile.from_dict(m))
    models = []
    for p in profs:
        res = await ai_client.probe(model_profile=p)
        models.append({
            "id": p.id, "model": p.model, "ok": res.get("ok"),
            "latency_ms": res.get("latency_ms"), "reason": res.get("reason"),
        })
    return {"ai_enabled": settings.ai_enabled, "models": models}


@app.get("/api/ai/usage")
async def ai_usage():
    """当前用户 AI 用量汇总（来自 ai_usage 表；未配置 DB 时返回空结构）。"""
    summary = await usage_store.summary(DEFAULT_USER)
    return summary


@app.get("/api/ai/usage/daily")
async def ai_usage_daily(days: int = Query(14, ge=1, le=90)):
    """近 N 天按天用量趋势（Dashboard 趋势图用）。"""
    return {"days": days, "daily": await usage_store.daily_summary(DEFAULT_USER, days=days)}


# ===== 完美模型配置模块：预设库 / 获取模型列表 / 测速 / Token费用参考 / 模型增删改 =====
class ModelFetchReq(BaseModel):
    base_url: str
    api_key: str = ""
    proxy: Optional[str] = None


class ModelSpeedReq(BaseModel):
    id: Optional[str] = None          # 复用已配置模型（带有效 key）
    base_url: Optional[str] = None   # 或内联传入
    model: Optional[str] = None
    api_key: Optional[str] = None
    proxy: Optional[str] = None
    rounds: int = 3


class ModelUpsertReq(BaseModel):
    id: str
    name: Optional[str] = None
    base_url: str
    model: str
    api_key: str = ""
    proxy: Optional[str] = None
    tags: list = []


class ModelActiveReq(BaseModel):
    id: str


def _profile_from_req(req: ModelSpeedReq) -> Optional[AIProfile]:
    """从请求构造模型档案：优先按 id 取已配置模型，否则用内联 base_url/model/api_key。"""
    if req.id:
        for m in settings.ai_models:
            if m.get("id") == req.id and _is_valid_key(m.get("api_key", "")):
                return AIProfile.from_dict(m)
    if req.base_url and req.model and req.api_key:
        return AIProfile(id=req.id or "adhoc", name=req.id or "adhoc",
                         base_url=req.base_url, model=req.model,
                         api_key=req.api_key, proxy=req.proxy or "")
    return None


@app.get("/api/models/presets")
async def models_presets():
    """预设厂商 + 模型目录（UI 一键添加用），含官方单价参考。"""
    return model_presets.presets_for_ui()


@app.get("/api/models")
async def models_list():
    """当前已配置模型清单（脱敏 key + 是否默认 + 是否可用）。"""
    out = []
    for m in settings.ai_models:
        out.append({
            "id": m.get("id"),
            "name": m.get("name", m.get("id")),
            "base_url": m.get("base_url", ""),
            "model": m.get("model", ""),
            "has_key": _is_valid_key(m.get("api_key", "")),
            "api_key_masked": mask_secret(m.get("api_key", "")),
            "proxy": m.get("proxy", ""),
            "tags": m.get("tags", []) or [],
            "is_active": m.get("id") == settings.ai_active,
        })
    return {"active": settings.ai_active, "models": out}


@app.post("/api/models")
async def models_upsert(req: ModelUpsertReq):
    """新增或更新一条模型配置（按 id 去重），持久化到运行时配置。"""
    mid = req.id.strip()
    if not mid:
        return JSONResponse(status_code=400, content={"error": "id 不能为空"})
    # 自动从 base_url 推断 provider，并带出预设官方单价（供 UI 参考）
    provider = model_presets.provider_of(req.base_url)
    preset = model_presets.find_preset_model(provider, req.model)
    entry = {
        "id": mid,
        "name": req.name or req.id,
        "base_url": req.base_url.rstrip("/"),
        "model": req.model,
        "api_key": req.api_key,
        "proxy": req.proxy or "",
        "tags": req.tags or [],
    }
    models = settings.upsert_ai_model(entry)
    # 若当前无默认，自动设为默认
    if not _is_valid_key(settings.ai_active) or settings.ai_active not in {m.get("id") for m in models}:
        if models:
            settings.set_active_ai_model(models[0].get("id"))
    return {"ok": True, "provider": provider,
            "preset_pricing": preset, "models": len(models)}


@app.delete("/api/models/{mid}")
async def models_delete(mid: str):
    """删除一条模型配置（按 id），持久化。"""
    models = settings.remove_ai_model(mid)
    return {"ok": True, "removed": mid, "active": settings.ai_active, "models": len(models)}


@app.post("/api/models/active")
async def models_set_active(req: ModelActiveReq):
    """设置当前默认生效模型，持久化。"""
    if not any(m.get("id") == req.id for m in settings.ai_models):
        return JSONResponse(status_code=404, content={"error": "模型不存在"})
    settings.set_active_ai_model(req.id)
    return {"ok": True, "active": settings.ai_active}


@app.post("/api/models/fetch")
async def models_fetch(req: ModelFetchReq):
    """获取模型列表：用 base_url + api_key 调 OpenAI 兼容的 /models 端点，返回真实可用模型。

    便于用户「填好地址和 key → 一键拉取模型清单 → 选一个添加」。失败返回结构化 reason。
    """
    base_url = (req.base_url or "").rstrip("/")
    if not base_url:
        return JSONResponse(status_code=400, content={"error": "base_url 不能为空"})
    proxy = req.proxy or settings.ai_proxy or None
    headers = {}
    if req.api_key:
        headers["Authorization"] = f"Bearer {req.api_key}"
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(20)) as hc:
            resp = await hc.get(f"{base_url}/models", headers=headers)
        if resp.status_code != 200:
            return JSONResponse(status_code=502,
                                content={"error": "获取失败",
                                         "reason": _fetch_error(resp.status_code, resp.text[:200])})
        data = resp.json()
        items = data.get("data", []) if isinstance(data, dict) else []
        models = []
        for it in items:
            if isinstance(it, dict):
                models.append({"id": it.get("id"), "object": it.get("object", ""),
                               "owned_by": it.get("owned_by", "")})
        return {"ok": True, "count": len(models), "models": models,
                "provider": model_presets.provider_of(base_url)}
    except Exception as e:
        return JSONResponse(status_code=502,
                            content={"error": "网络异常", "reason": f"{type(e).__name__}: {e}"})


@app.post("/api/models/speedtest")
async def models_speedtest(req: ModelSpeedReq):
    """测速：对指定模型跑 N 轮，返回 TTFT/延迟/吞吐(tps)。"""
    mp = _profile_from_req(req)
    if mp is None:
        return JSONResponse(status_code=400,
                            content={"error": "需提供有效 id 或 base_url+model+api_key"})
    return await ai_client.speed_test(model_profile=mp, rounds=req.rounds)


@app.get("/api/models/pricing")
async def models_pricing():
    """Token 费用参考：官方单价知识库（元/百万） + 当前用量折算费用。"""
    pricing = []
    for key, (inp, out) in model_presets.OFFICIAL_PRICING_CNY.items():
        if not (isinstance(key, tuple) and len(key) == 2):
            continue  # 跳过 (model,) 单独查价键，只取 (provider, model) 精确条目
        prov, mdl = key
        pricing.append({"provider": prov, "model": mdl,
                        "in_per_million": inp, "out_per_million": out})
    # 当前用量（来自 ai_usage 表；无 DB 时返回空结构）
    usage = await usage_store.summary(DEFAULT_USER)
    return {"unit": "元/每百万 token", "pricing": pricing, "usage": usage}


def _fetch_error(code: int, body: str) -> str:
    if code == 401:
        return "Key 无效（401）：无法拉取模型列表"
    if code == 403:
        return "无权限（403）"
    if code == 404:
        return "该端点不支持 /models（404）：部分中转站未实现列表接口"
    if code == 429:
        return "频率受限（429）"
    return f"HTTP {code}: {body[:160]}"


@app.get("/api/skills/stats")
async def skills_stats():
    """技能列表 + 命中计数 + 最近意图/技能（Dashboard 技能热度用）。"""
    skills = agent_router.skill_registry.get_available_skills()
    hits = agent_router.memory.skill_hits
    working = agent_router.memory.get_working(DEFAULT_USER)
    return {
        "skills": [
            {"name": s["name"], "desc": s.get("desc"), "hits": hits.get(s["name"], 0)}
            for s in skills
        ],
        "last_intent": working.get("last_intent"),
        "last_skill": working.get("last_skill"),
    }


# ===== 技能管理（设置页：API 技能 + 完整技能包）=====
def _rebuild_skills() -> None:
    """统一热重载：重新扫描技能包目录 + 注入配置驱动的 API 技能。"""
    agent_router.skill_registry.reload_all_skills()
    build_api_skills_into(agent_router.skill_registry)


class ApiSkillReq(BaseModel):
    id: Optional[str] = None
    name: str
    description: str = ""
    trigger_keywords: list = []
    api_url: str = ""
    api_key: str = ""
    method: str = "GET"
    enabled: bool = True


class SkillPackageReq(BaseModel):
    name: str
    description: str = ""
    trigger_keywords: list = []
    handler_code: str


class SkillToggleReq(BaseModel):
    enabled: bool = True


@app.get("/api/skills")
async def skills_mgmt_list():
    """管理用技能清单：区分 API / 包 类型，含启用状态与脱敏 Key。

    若某个 API 技能（api_skills 配置）的同名已由 skills/ 代码包接管
    （如「高德地图」由 skills/amap 实现，仅借用其 api_key），则标记为
    managed_by_package=True，前端据此提示「由完整技能包接管，此处仅管理 Key」。
    """
    registry = agent_router.skill_registry
    api_ids = {s.get("id") for s in settings.api_skills}
    items = []
    for s in registry.get_available_skills():
        name = s["name"]
        handler = registry.skills.get(name)
        is_pure_api = isinstance(handler, ApiSkillHandler)
        entry = {
            "name": name, "desc": s.get("desc"),
            "trigger_keywords": s.get("trigger_keywords", []),
            "type": "api" if is_pure_api else "package", "enabled": True,
        }
        if is_pure_api:
            src = next((x for x in settings.api_skills if x.get("id") == name), {})
            entry["api_url"] = src.get("api_url", "")
            entry["api_key_masked"] = mask_secret(src.get("api_key", ""))
            entry["method"] = src.get("method", "GET")
        elif name in api_ids:
            # 代码包接管，但 api_skills 中留有同名条目（用于存 Key）
            entry["managed_by_package"] = True
            src = next((x for x in settings.api_skills if x.get("id") == name), {})
            entry["api_key_masked"] = mask_secret(src.get("api_key", ""))
        items.append(entry)
    # 补上已停用的纯 API 技能（未在路由表中）
    live_names = {it["name"] for it in items}
    for s in settings.api_skills:
        if not s.get("enabled", True) and s.get("id") not in live_names:
            items.append({
                "name": s.get("name"), "desc": s.get("description", ""),
                "trigger_keywords": s.get("trigger_keywords", []),
                "type": "api", "enabled": False,
                "api_url": s.get("api_url", ""),
                "api_key_masked": mask_secret(s.get("api_key", "")),
                "method": s.get("method", "GET"),
            })
    return {"skills": items, "skills_dir": os.path.abspath(os.environ.get("SKILLS_DIR", "skills"))}


@app.get("/api/skills/api")
async def api_skills_list():
    """仅列出配置驱动的 API 技能（含脱敏 Key）。"""
    out = []
    for s in settings.api_skills:
        out.append({
            "id": s.get("id"), "name": s.get("name"),
            "description": s.get("description", ""),
            "trigger_keywords": s.get("trigger_keywords", []),
            "api_url": s.get("api_url", ""),
            "api_key_masked": mask_secret(s.get("api_key", "")),
            "method": s.get("method", "GET"),
            "enabled": s.get("enabled", True),
        })
    return {"api_skills": out}


@app.post("/api/skills/api")
async def api_skill_upsert(req: ApiSkillReq):
    """新增/更新一条 API 技能，持久化并热重载路由。"""
    name = (req.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name 不能为空"})
    if req.method not in ("GET", "POST"):
        return JSONResponse(status_code=400, content={"error": "method 仅支持 GET / POST"})
    sid = req.id.strip() if req.id else name
    try:
        settings.upsert_api_skill({
            "id": sid, "name": name, "description": req.description,
            "trigger_keywords": req.trigger_keywords, "api_url": req.api_url,
            "api_key": req.api_key, "method": req.method, "enabled": req.enabled,
        })
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    _rebuild_skills()
    return {"ok": True, "id": sid, "count": len(settings.api_skills)}


@app.delete("/api/skills/api/{sid}")
async def api_skill_delete(sid: str):
    """删除一条 API 技能并热重载。"""
    settings.remove_api_skill(sid)
    _rebuild_skills()
    return {"ok": True, "removed": sid, "count": len(settings.api_skills)}


@app.post("/api/skills/api/{sid}/toggle")
async def api_skill_toggle(sid: str, req: SkillToggleReq):
    """启用/停用一条 API 技能并热重载。"""
    settings.set_api_skill_enabled(sid, req.enabled)
    _rebuild_skills()
    return {"ok": True, "id": sid, "enabled": req.enabled}


@app.post("/api/skills/package")
async def skill_package_create(req: SkillPackageReq):
    """写入一个完整技能包（skill.yaml + handler.py）并热加载。

    安全：仅限本地单用户系统使用，handler_code 会被直接执行；
    请仅添加自己信任的代码，避免任意外部来源粘贴。
    """
    name = (req.name or "").strip()
    try:
        sanitize_skill_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    code = (req.handler_code or "").strip()
    if not code or "class SkillHandler" not in code:
        return JSONResponse(status_code=400,
                            content={"error": "handler_code 必须包含 class SkillHandler 定义"})
    try:
        folder = write_skill_package(name, req.description, req.trigger_keywords, code)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"写入失败：{e}"})
    _rebuild_skills()
    return {"ok": True, "name": name, "path": folder,
            "warning": "handler.py 中的代码将在本进程直接执行，请仅添加可信代码"}


@app.delete("/api/skills/package/{name}")
async def skill_package_delete(name: str):
    """删除一个完整技能包并热重载。"""
    try:
        sanitize_skill_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    delete_skill_package(name)
    _rebuild_skills()
    return {"ok": True, "removed": name}


@app.post("/api/skills/reload")
async def skills_reload():
    """手动热重载全部技能（包 + API）。"""
    _rebuild_skills()
    return {"ok": True, "count": len(agent_router.skill_registry.skills)}


@app.get("/api/connector/status")
async def connector_status():
    """连接器状态（前端 Dashboard 展示）：webhook 是否启用、入站计数、飞书推送可用性。"""
    return connector.status()


@app.post("/api/connector/push")
async def connector_push(req: ConnectorPushReq):
    """出站推送：把一条消息推到飞书（管理员）或通用 HTTP 端点。需 Bearer 鉴权。"""
    return await connector.push(req.channel, req.target, req.message)


@app.post("/api/connector/webhook")
async def connector_webhook(request: Request):
    """通用入站 Webhook（机器调用）：共享令牌验签（header X-LifeOS-Webhook-Token 或 ?token=）。
    未配置 CONNECTOR_WEBHOOK_TOKEN 时端点不启用（503）。按 body.type 路由 todo/memory/chat/raw。
    """
    expected = settings.connector_webhook_token
    if not expected:
        return JSONResponse(status_code=503,
                            content={"error": "连接器 Webhook 未启用（请在服务端配置 CONNECTOR_WEBHOOK_TOKEN）",
                                      "code": "WEBHOOK_DISABLED"})
    provided = request.headers.get("X-LifeOS-Webhook-Token", "")
    if not provided:
        provided = request.query_params.get("token", "")
    if not hmac.compare_digest(provided, expected):
        return JSONResponse(status_code=401,
                            content={"error": "Webhook 令牌无效", "code": "WEBHOOK_TOKEN_INVALID"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body 需为 JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "body 需为 JSON 对象"})
    result = await connector.handle_inbound(payload)
    return {"ok": True, "result": result}


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(req: ChatReq):
    """SSE 流式对话：逐块返回文本（打字机效果）。skill/multi_step 走非流式整段返回。"""
    if not ai_rate_limiter.allow(DEFAULT_USER):
        return JSONResponse(status_code=429,
                            content={"error": "请求过于频繁，请稍后再试", "code": "RATE_LIMITED"},
                            headers={"Retry-After": "30"})
    return StreamingResponse(
        _sse_chat(req.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _sse_chat(message: str):
    """把 router.stream_message 的纯文本片段包成 SSE 事件。"""
    payload = MessagePayload(user_id="me", message=message, source="debug",
                             time=str(int(time.time())))
    try:
        async for piece in agent_router.stream_message(payload):
            yield f"data: {json.dumps({'delta': piece}, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ===== 调试/本地入口：直接对话 Agent（受 Bearer 保护）=====


# ===== 待办 / 收支 REST（前后端共用 PgStore，按单用户实例隔离）=====
_todo_store = PgStore("todo")
_expense_store = PgStore("expense")
DEFAULT_USER = "me"


class TodoCreate(BaseModel):
    title: str
    priority: Optional[str] = None
    due: Optional[str] = None


class ExpenseCreate(BaseModel):
    type: str            # "income" | "expense"
    amount: float
    category: str = "其他"
    note: str = ""
    happened_at: Optional[str] = None


@app.get("/api/todos")
async def list_todos():
    return {"items": await _todo_store.list_all(DEFAULT_USER)}


@app.post("/api/todos")
async def create_todo(req: TodoCreate):
    item = await _todo_store.add(DEFAULT_USER, {
        "title": req.title, "done": False,
        "priority": req.priority, "due": req.due,
    })
    return item


@app.post("/api/todos/{tid}/done")
async def done_todo(tid: str):
    it = await _todo_store.update(DEFAULT_USER, tid,
                                  {"done": True, "done_at": int(time.time())})
    if not it:
        return JSONResponse(status_code=404, content={"error": "待办不存在"})
    return it


@app.delete("/api/todos/{tid}")
async def delete_todo(tid: str):
    ok = await _todo_store.delete(DEFAULT_USER, tid)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "待办不存在"})
    return {"ok": True}


@app.get("/api/expense")
async def list_expense(month: Optional[str] = None):
    items = await _expense_store.list_all(DEFAULT_USER)
    if month:
        items = [i for i in items if (i.get("happened_at") or "").startswith(month)]
    return {"items": items}


@app.post("/api/expense")
async def create_expense(req: ExpenseCreate):
    if req.type not in ("income", "expense"):
        return JSONResponse(status_code=400,
                            content={"error": "type 必须是 income 或 expense"})
    item = await _expense_store.add(DEFAULT_USER, {
        "type": req.type,
        "amount": round(req.amount, 2),
        "category": req.category,
        "note": req.note,
        "happened_at": req.happened_at or time.strftime("%Y-%m-%d"),
    })
    return item


@app.get("/api/expense/summary")
async def expense_summary(month: Optional[str] = None):
    if not month:
        month = time.strftime("%Y-%m")
    items = await _expense_store.list_all(DEFAULT_USER)
    cur = [i for i in items if (i.get("happened_at") or "").startswith(month)]
    income = sum(i.get("amount", 0) for i in cur if i.get("type") == "income")
    expense = sum(i.get("amount", 0) for i in cur if i.get("type") == "expense")
    return {"month": month, "income": round(income, 2), "expense": round(expense, 2),
            "balance": round(income - expense, 2), "count": len(cur)}


@app.delete("/api/expense/{eid}")
async def delete_expense(eid: str):
    ok = await _expense_store.delete(DEFAULT_USER, eid)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "记录不存在"})
    return {"ok": True}


# ===== 配置查看 / 更新（供前端设置页）=====
@app.get("/api/config")
async def config_view():
    # 当前生效模型名称（多模型库优先），消除旧单模型字段 ai_model 的冲突展示
    active_name = settings.ai_active
    for m in settings.ai_models:
        if m.get("id") == settings.ai_active:
            active_name = m.get("name", m.get("id"))
            break
    return {
        "ai_enabled": settings.ai_enabled,
        "ai_active": settings.ai_active,
        "ai_active_name": active_name,
        "ai_models_count": len(settings.ai_models),
        "embedding_model": settings.embedding_model,
        "scenario_models": settings.scenario_models,
        "feishu_enabled": settings.feishu_enabled,
        "feishu_app_id_masked": mask_secret(settings.feishu_app_id),
        "otp_setup_open": auth.is_setup_open(),
    }


class ConfigUpdateReq(BaseModel):
    ai_enabled: Optional[bool] = None
    embedding_model: Optional[str] = None
    feishu_enabled: Optional[bool] = None


@app.post("/api/config")
async def config_update(req: ConfigUpdateReq):
    """更新系统设置（AI 总开关 / 长期记忆模型 / 飞书启用）。受 Bearer 保护。"""
    patch = {}
    if req.ai_enabled is not None:
        patch["ai_enabled"] = bool(req.ai_enabled)
    if req.embedding_model is not None:
        patch["embedding_model"] = req.embedding_model.strip()
    if req.feishu_enabled is not None:
        patch["feishu_enabled"] = bool(req.feishu_enabled)
    if not patch:
        return JSONResponse(status_code=400, content={"error": "无可更新字段"})
    # upsert_setting 自带 **** 占位符保护，并落库 settings_runtime.json
    settings.upsert_setting(patch)
    return {"ok": True, **{k: getattr(settings, k) for k in patch}}


# ===== 前端 SPA 托管（FRONTEND_DIST 指向构建产物 dist）=====
# 与 API 同源部署时无需跨域；非 API 路径已被 auth_guard 放行。
# catch-all 放在所有 /api 路由之后，保证 /api/* 优先匹配。
FRONTEND_DIST = os.environ.get("FRONTEND_DIST", "")
if FRONTEND_DIST and os.path.isdir(FRONTEND_DIST):
    _INDEX = os.path.join(FRONTEND_DIST, "index.html")

    @app.get("/assets/{path:path}")
    async def spa_assets(path: str):
        fp = os.path.join(FRONTEND_DIST, "assets", path)
        if os.path.isfile(fp):
            return FileResponse(fp)
        return JSONResponse(status_code=404, content={"error": "not found"})

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # API 路由已优先匹配，这里仅处理前端路由（保险）
        if full_path.startswith("api"):
            return JSONResponse(status_code=404, content={"error": "not found"})
        fp = os.path.join(FRONTEND_DIST, full_path)
        if full_path and os.path.isfile(fp):
            return FileResponse(fp)
        if os.path.isfile(_INDEX):
            return FileResponse(_INDEX)
        return JSONResponse(status_code=404, content={"error": "前端未构建（FRONTEND_DIST 未配置或缺失）"})
else:
    @app.get("/")
    async def spa_missing():
        return {
            "service": "LifeOS",
            "status": "ok",
            "note": "前端未构建/未挂载（设置 FRONTEND_DIST 指向构建产物 dist 以启用 Web 控制台）",
        }
