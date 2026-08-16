"""LifeOS 核心入口：FastAPI 网关 + OTP 鉴权中间件 + 飞书/OTP 端点 + lifespan。

- 所有 /api/* 受 Bearer 鉴权保护（白名单：/api/health、/api/auth/*）
- 飞书消息（WS 长连接）最终路由到 AgentRouter 并回转
- OTP（TOTP + 会话令牌）按《复刻指导 03》实现，纯标准库
"""
import os
import json
import asyncio
import time
import hmac
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

from app import auth
from app.config import settings, mask_secret, AIProfile
from app.ai import client as ai_client
from app.ai.usage_store import usage_store
from app.ai.ratelimit import ai_rate_limiter
from app.feishu import (bot, start_bot, stop_bot, bot_status, get_news)
from app.feishu_deviceflow import FeishuDeviceFlow
from app.agent.router import agent_router, MessagePayload
from app.skills.db_store import PgStore, init_db

# ===== 鉴权白名单（03-OTP：勿扩大）=====
PUBLIC_EXACT = {"/api/health"}
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
    yield
    # 关停
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
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
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
    }


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


# ===== 调试/本地入口：直接对话 Agent（受 Bearer 保护）=====
class ChatReq(BaseModel):
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


# ===== 配置查看（脱敏，供前端设置页）=====
@app.get("/api/config")
async def config_view():
    return {
        "ai_enabled": settings.ai_enabled,
        "ai_active": settings.ai_active,
        "ai_model": settings.ai_model,
        "ai_base_url": settings.ai_base_url,
        "ai_api_key_masked": mask_secret(settings.ai_api_key),
        "ai_models_count": len(settings.ai_models),
        "scenario_models": settings.scenario_models,
        "feishu_enabled": settings.feishu_enabled,
        "feishu_app_id_masked": mask_secret(settings.feishu_app_id),
        "otp_setup_open": auth.is_setup_open(),
    }


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
