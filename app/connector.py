"""Phase 6 Connector：通用 Webhook 入站 + 出站推送。

设计要点：
- 入站：`POST /api/connector/webhook`（共享令牌验签）→ 按 `type` 路由：
  todo（建待办）/ memory（存短期记忆）/ chat（走 Agent 对话）/ raw（原样存记忆）。
- 出站：`ConnectorService.push(channel, target, message)`，支持 feishu（推给管理员/指定 open_id）
  与 http（通用 POST）。全部失败降级，不阻断主流程。
- 不引入额外 DB 表；待办复用 PgStore，记忆复用 MemoryManager，对话复用 AgentRouter。
"""
import json
import logging
import time
from typing import Any, Optional

import httpx

from app.config import settings
from app.agent.router import agent_router, MessagePayload
from app.skills.db_store import PgStore

log = logging.getLogger("lifeos.connector")

DEFAULT_USER = "me"


class ConnectorService:
    def __init__(self):
        self.inbound_count = 0
        self.last_inbound: Optional[dict] = None

    # ===================== 入站路由 =====================
    async def handle_inbound(self, payload: dict) -> dict:
        self.inbound_count += 1
        self.last_inbound = {"at": int(time.time()), "type": payload.get("type")}
        typ = (payload.get("type") or "raw").lower()
        handlers = {
            "todo": self._in_todo,
            "memory": self._in_memory,
            "chat": self._in_chat,
        }
        fn = handlers.get(typ, self._in_raw)
        return await fn(payload)

    async def _in_todo(self, p: dict) -> dict:
        title = (p.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title required"}
        store = PgStore("todo")
        item = await store.add(DEFAULT_USER, {
            "title": title, "done": False,
            "priority": p.get("priority"), "due": p.get("due"),
        })
        return {"ok": True, "type": "todo", "item": item}

    async def _in_memory(self, p: dict) -> dict:
        content = p.get("content") or p.get("text") or ""
        if not content:
            return {"ok": False, "error": "content required"}
        agent_router.memory.add_short(DEFAULT_USER, "[webhook] " + content, "")
        return {"ok": True, "type": "memory", "stored": "short"}

    async def _in_chat(self, p: dict) -> dict:
        message = p.get("message") or ""
        if not message:
            return {"ok": False, "error": "message required"}
        reply = await agent_router.process_message(
            MessagePayload(message=message, user_id=DEFAULT_USER, source="webhook")
        )
        return {"ok": True, "type": "chat", "reply": reply}

    async def _in_raw(self, p: dict) -> dict:
        snippet = p.get("content") or json.dumps(p, ensure_ascii=False)
        agent_router.memory.add_short(DEFAULT_USER, "[webhook-raw] " + str(snippet)[:500], "")
        return {"ok": True, "type": "raw", "stored": "short"}

    # ===================== 出站推送 =====================
    async def push(self, channel: str, target: str, message: str) -> dict:
        channel = (channel or "").lower()
        if channel == "feishu":
            return await self._push_feishu(target, message)
        if channel == "http":
            return await self._push_http(target, message)
        return {"ok": False, "error": "unknown channel: " + str(channel)}

    async def _push_feishu(self, target: str, message: str) -> dict:
        from app.feishu import bot  # 延迟导入，避免任何潜在循环
        if not settings.feishu_enabled or not settings.feishu_app_id:
            return {"ok": False, "error": "feishu not configured"}
        if target in (None, "", "admin"):
            targets = list(settings.feishu_admin_users or [])
        else:
            targets = [target]
        if not targets:
            return {"ok": False, "error": "no feishu admin users configured"}
        sent = await bot.push_to_users(targets, message)
        return {"ok": sent > 0, "sent": sent, "total": len(targets)}

    async def _push_http(self, target: str, message: str) -> dict:
        if not target or not target.startswith("http"):
            return {"ok": False, "error": "invalid http target"}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(target,
                                json={"message": message, "text": message},
                                headers={"Content-Type": "application/json"})
                return {"ok": True, "status": r.status_code}
        except Exception as e:
            log.warning("connector http push failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ===================== 状态 =====================
    def status(self) -> dict:
        return {
            "webhook_enabled": bool(settings.connector_webhook_token),
            "inbound_count": self.inbound_count,
            "last_inbound": self.last_inbound,
            "feishu_push": bool(settings.feishu_enabled and settings.feishu_app_id),
        }


connector = ConnectorService()
