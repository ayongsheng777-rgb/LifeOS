"""LifeOS 核心入口：FastAPI 网关 + OTP 鉴权中间件 + 飞书/OTP 端点 + lifespan。

- 所有 /api/* 受 Bearer 鉴权保护（白名单：/api/health、/api/auth/*）
- 飞书消息（WS 长连接）最终路由到 AgentRouter 并回转
- OTP（TOTP + 会话令牌）按《复刻指导 03》实现，纯标准库
"""
import os
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from app import auth
from app.config import settings, mask_secret
from app.ai import client as ai_client
from app.feishu import (bot, start_bot, stop_bot, bot_status, get_news)
from app.feishu_deviceflow import FeishuDeviceFlow
from app.agent.router import agent_router, MessagePayload
from app.skills.store import JsonStore

# ===== 鉴权白名单（03-OTP：勿扩大）=====
PUBLIC_EXACT = {"/api/health"}
PUBLIC_PREFIXES = ("/api/auth/",)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    return {
        "status": "ok",
        "ai_available": ai_client.available(),
        "feishu": bot_status(),
        "memory": {
            "short_term_redis": settings.redis_url,
            "long_term_qdrant": settings.qdrant_url,
            "embedding_model": settings.embedding_model,
        },
    }


# ===== OTP 端点（03-OTP）=====
class LoginReq(BaseModel):
    otp: str


class ResetReq(BaseModel):
    otp: str


@app.get("/api/auth/setup")
async def auth_setup():
    if not auth.is_setup_open():
        return JSONResponse(status_code=403, content={"setup_open": False})
    info = auth.setup_info()
    # 脱敏：secret 仅展示一次（setup_open 时允许展示）；otpauth_uri 含 secret，前端用后即焚
    return info


@app.post("/api/auth/login")
async def auth_login(req: LoginReq):
    result = auth.login(req.otp)
    if not result:
        return JSONResponse(status_code=401, content={"error": "动态码错误", "code": "OTP_INVALID"})
    return result


@app.get("/api/auth/check")
async def auth_check():
    return {"authed": False, "setup_open": auth.is_setup_open()}


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


# ===== 调试/本地入口：直接对话 Agent（受 Bearer 保护）=====
class ChatReq(BaseModel):
    user_id: str = "debug"
    message: str


@app.post("/api/agent/chat")
async def agent_chat(req: ChatReq):
    reply = await agent_router.process_message(
        MessagePayload(user_id=req.user_id, message=req.message, source="debug",
                       time=str(int(time.time()))))
    return {"reply": reply}


# ===== 待办 / 收支 REST（前后端共用 JsonStore，按单用户实例隔离）=====
_todo_store = JsonStore("todo")
_expense_store = JsonStore("expense")
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
