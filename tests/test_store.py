"""Phase 1：PgStore 对真实 PostgreSQL 的往返测试。

仅当环境变量 DB_URL 已配置时运行；否则自动跳过（本地无 PG 也能跑其它测试）。
用一个事件循环跑完整流程，避免异步引擎跨 loop 复用报错。
"""
import asyncio
import os

import pytest

from app.skills.db_store import PgStore, init_db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DB_URL"),
    reason="DB_URL 未配置，跳过 PgStore 集成测试（需真实 PostgreSQL）",
)


def test_store_roundtrip():
    async def main():
        await init_db()

        # ===== 待办 CRUD + 谓词过滤 =====
        todo = PgStore("todo")
        created = await todo.add(
            "me", {"title": "写报告", "done": False, "priority": "高", "due": "2026-08-20"}
        )
        assert created["id"] and len(created["id"]) == 12
        assert created["user_id"] == "me"
        assert created["title"] == "写报告"
        assert created["done"] is False
        assert created["priority"] == "高"

        items = await todo.list_all("me")
        assert any(i["id"] == created["id"] for i in items)

        upd = await todo.update("me", created["id"], {"done": True, "done_at": 1})
        assert upd["done"] is True

        open_items = await todo.list_where("me", lambda x: not x.get("done"))
        assert not any(i["id"] == created["id"] for i in open_items)

        ok = await todo.delete("me", created["id"])
        assert ok is True
        assert await todo.find("me", created["id"]) is None

        # delete_where：按标题关键字批量删
        await todo.add("me", {"title": "买菜", "done": False})
        removed = await todo.delete_where("me", lambda x: "买菜" in (x.get("title") or ""))
        assert removed >= 1

        # ===== 收支 CRUD + 金额类型 =====
        exp = PgStore("expense")
        e = await exp.add(
            "me",
            {"type": "expense", "amount": 12.5, "category": "餐饮", "note": "", "happened_at": "2026-08-16"},
        )
        assert e["amount"] == 12.5
        assert isinstance(e["amount"], float)  # 与旧 JsonStore(float) 兼容

        eitems = await exp.list_all("me")
        assert any(i["id"] == e["id"] for i in eitems)

        ok = await exp.delete("me", e["id"])
        assert ok is True

    asyncio.run(main())
