"""LifeOS Phase 1 数据层：PostgreSQL 异步 ORM 存储（替代 JsonStore）。

设计取舍（对齐《验收报告》P1「数据层迁 PostgreSQL」+ 用户选定「完整 ORM 改造」）：
- 待办 / 收支各一张**真实类型化表**（todos / expenses），字段为真正的列（标题/金额/类型…），
  不再把整条记录塞进 JSON 文件。
- 对外仍提供与 JsonStore **完全一致的异步方法接口**
  （list_all / list_where / add / find / update / delete / delete_where），
  返回的仍是原来的 dict 形状（{id, user_id, created_at, ...业务字段}）。
  → 4 个调用点（main.py ×2、todo_skill、expense_skill）只改 `JsonStore→PgStore` 一行，
    业务逻辑 / 前端 / 技能筛选逻辑零改动，回归风险最低。
- list_where / delete_where 的 predicate 仍是 Python lambda（对个人实例数据量足够），
  行为与旧 JsonStore 完全一致，无需把 lambda 翻译成 SQL。
- init_db() 在应用启动时建表（CREATE TABLE IF NOT EXISTS）；DB_URL 未配置则优雅跳过。
"""
import time
import uuid
from typing import Optional

from sqlalchemy import String, BigInteger, Boolean, Numeric, Text, select, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings


# ===== ORM 模型（真实类型化表）=====
class Base(DeclarativeBase):
    pass


class Todo(Base):
    __tablename__ = "todos"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(512))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    due: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    done_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(16))            # "income" | "expense"
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    category: Mapped[str] = mapped_column(String(64), default="其他")
    note: Mapped[str] = mapped_column(Text, default="")
    happened_at: Mapped[str] = mapped_column(String(32))     # "YYYY-MM-DD"


# name -> (ORM 模型, 业务列集合)
_MODELS = {"todo": Todo, "expense": Expense}
_COLS = {
    "todo": {"title", "done", "priority", "due", "done_at"},
    "expense": {"type", "amount", "category", "note", "happened_at"},
}


# ===== 引擎 / 会话（懒加载：import 时不连库）=====
_engine = None
_sessionmaker = None


def _make_engine() -> bool:
    global _engine, _sessionmaker
    url = settings.db_url
    if not url:
        return False
    # pool_pre_ping 自动剔除失效连接（PG 重启后）
    _engine = create_async_engine(url, future=True, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return True


def get_sessionmaker():
    if _sessionmaker is None:
        if not _make_engine():
            raise RuntimeError("DB_URL 未配置：无法使用 PostgreSQL 存储（待办/收支不可用）")
    return _sessionmaker


async def init_db() -> None:
    """应用启动时建表。DB_URL 未配置则跳过（不报错，便于纯本地无 PG 启动）。"""
    if not settings.db_url:
        print("[db_store] 未配置 DB_URL，跳过建表（待办/收支将不可用）")
        return
    if _engine is None:
        _make_engine()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[db_store] PostgreSQL 表已就绪（todos / expenses）")


# ===== PgStore：与 JsonStore 完全一致的方法接口 =====
class PgStore:
    """按 name 选择真实表，暴露与 JsonStore 同签名的方法。"""

    def __init__(self, name: str):
        if name not in _MODELS:
            raise ValueError(f"未知存储名: {name}（支持：{list(_MODELS)}）")
        self.name = name
        self._model = _MODELS[name]
        self._cols = _COLS[name]

    # ---- 内部：行 -> dict（保持旧 JsonStore 的返回形状）----
    def _to_dict(self, row) -> dict:
        d = {"id": row.id, "user_id": row.user_id, "created_at": row.created_at}
        for c in self._cols:
            d[c] = getattr(row, c)
        # amount 旧实现存 float；Numeric 读出为 Decimal，转回 float 保兼容
        if self.name == "expense" and "amount" in d and d["amount"] is not None:
            d["amount"] = float(d["amount"])
        return d

    def _payload_to_cols(self, payload: dict) -> dict:
        """只取模型认识的列，忽略未知键（未知键旧实现会丢弃，行为一致）。"""
        return {k: v for k, v in payload.items() if k in self._cols}

    # ---- 与 JsonStore 一致的公开方法 ----
    async def list_all(self, user_id: str) -> list:
        sm = get_sessionmaker()
        async with sm() as s:
            res = await s.execute(
                select(self._model)
                .where(self._model.user_id == user_id)
                .order_by(self._model.created_at)
            )
            rows = res.scalars().all()
        return [self._to_dict(r) for r in rows]

    async def list_where(self, user_id: str, predicate) -> list:
        # 个人实例数据量小：拉该用户全部后在 Python 过滤（与 JsonStore 行为一致）
        items = await self.list_all(user_id)
        return [x for x in items if predicate(x)]

    async def add(self, user_id: str, payload: dict) -> dict:
        sm = get_sessionmaker()
        item_id = uuid.uuid4().hex[:12]
        created = int(time.time())
        cols = self._payload_to_cols(payload)
        row = self._model(id=item_id, user_id=user_id, created_at=created, **cols)
        async with sm() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return self._to_dict(row)

    async def find(self, user_id: str, item_id: str) -> Optional[dict]:
        sm = get_sessionmaker()
        async with sm() as s:
            row = await s.get(self._model, item_id)
            if row is None or row.user_id != user_id:
                return None
            return self._to_dict(row)

    async def update(self, user_id: str, item_id: str, patch: dict) -> Optional[dict]:
        sm = get_sessionmaker()
        cols = self._payload_to_cols(patch)
        async with sm() as s:
            row = await s.get(self._model, item_id)
            if row is None or row.user_id != user_id:
                return None
            for k, v in cols.items():
                setattr(row, k, v)
            await s.commit()
            await s.refresh(row)
            return self._to_dict(row)

    async def delete(self, user_id: str, item_id: str) -> bool:
        sm = get_sessionmaker()
        async with sm() as s:
            row = await s.get(self._model, item_id)
            if row is None or row.user_id != user_id:
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def delete_where(self, user_id: str, predicate) -> int:
        items = await self.list_all(user_id)
        matched = [x for x in items if predicate(x)]
        if not matched:
            return 0
        sm = get_sessionmaker()
        ids = [m["id"] for m in matched]
        async with sm() as s:
            await s.execute(
                delete(self._model)
                .where(self._model.id.in_(ids))
                .where(self._model.user_id == user_id)
            )
            await s.commit()
        return len(matched)
