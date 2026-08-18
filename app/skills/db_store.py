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

from sqlalchemy import String, BigInteger, Integer, Boolean, Numeric, Text, select, delete
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


class PersonalFact(Base):
    """个人长期事实库（永久记忆 A）：结构化事实，如『我今年用 XX 药好用』。

    与短期/工作记忆不同，这里跨会话长期保留，并在每次对话时作为上下文注入，
    让 AI 真正『记住』用户的偏好、健康、账号等稳定信息。
    """
    __tablename__ = "personal_facts"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(64), default="通用")
    key: Mapped[str] = mapped_column(String(256))            # 简短标签，如「用药-2026」
    value: Mapped[str] = mapped_column(Text)                # 事实内容
    source: Mapped[str] = mapped_column(String(32), default="chat")  # chat | manual


class Reminder(Base):
    """主动提醒引擎：到期/回访事件。调度器扫描后走飞书推送。"""
    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[int] = mapped_column(BigInteger)               # 到期 epoch
    advance_days: Mapped[int] = mapped_column(Integer, default=1)  # 提前几天提醒
    repeat_interval_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 回访间隔
    repeat_remaining: Mapped[int] = mapped_column(Integer, default=0)  # 剩余回访次数
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/sent/done/cancelled
    last_sent_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat | manual


# name -> (ORM 模型, 业务列集合)
_MODELS = {"todo": Todo, "expense": Expense, "fact": PersonalFact}
_COLS = {
    "todo": {"title", "done", "priority", "due", "done_at"},
    "expense": {"type", "amount", "category", "note", "happened_at"},
    "fact": {"category", "key", "value", "source", "updated_at"},
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


# ===== 个人长期事实库（永久记忆 A）便捷接口 =====
async def remember_fact(user_id: str, key: str, value: str,
                        category: str = "通用", source: str = "chat") -> dict:
    """写入/更新一条个人事实（按 user_id+key 幂等 upsert）。"""
    if not settings.db_url:
        raise RuntimeError("DB_URL 未配置：无法写入个人事实库")
    sm = get_sessionmaker()
    now = int(time.time())
    async with sm() as s:
        res = await s.execute(
            select(PersonalFact).where(
                PersonalFact.user_id == user_id, PersonalFact.key == key)
        )
        row = res.scalars().first()
        if row:
            row.value = value
            row.category = category
            row.source = source
            row.updated_at = now
            await s.commit()
            await s.refresh(row)
            return _fact_to_dict(row)
        row = PersonalFact(
            id=uuid.uuid4().hex[:12], user_id=user_id, created_at=now,
            updated_at=now, category=category, key=key, value=value, source=source,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return _fact_to_dict(row)


async def list_facts(user_id: str, limit: int = 50) -> list:
    store = PgStore("fact")
    items = await store.list_all(user_id)
    items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return items[:limit]


async def delete_fact(user_id: str, fact_id: str) -> bool:
    store = PgStore("fact")
    return await store.delete(user_id, fact_id)


def _fact_to_dict(row) -> dict:
    return {
        "id": row.id, "user_id": row.user_id, "category": row.category,
        "key": row.key, "value": row.value, "source": row.source,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


# ===== 提醒引擎存储 =====
def _reminder_to_dict(row) -> dict:
    return {
        "id": row.id, "user_id": row.user_id, "created_at": row.created_at,
        "title": row.title, "detail": row.detail, "due_at": row.due_at,
        "advance_days": row.advance_days, "repeat_interval_days": row.repeat_interval_days,
        "repeat_remaining": row.repeat_remaining, "status": row.status,
        "last_sent_at": row.last_sent_at, "source": row.source,
    }


async def add_reminder(user_id: str, *, title: str, detail: str = "", due_at: int,
                       advance_days: int = 1, repeat_interval_days=None,
                       repeat_times: int = 0, source: str = "chat") -> dict:
    if not settings.db_url:
        raise RuntimeError("DB_URL 未配置：无法写入提醒")
    sm = get_sessionmaker()
    now = int(time.time())
    row = Reminder(
        id=uuid.uuid4().hex[:12], user_id=user_id, created_at=now,
        title=title, detail=detail, due_at=int(due_at), advance_days=int(advance_days),
        repeat_interval_days=repeat_interval_days, repeat_remaining=int(repeat_times or 0),
        status="pending", source=source,
    )
    async with sm() as s:
        s.add(row)
        await s.commit()
        await s.refresh(row)
    return _reminder_to_dict(row)


async def list_reminders(user_id: str, status: str = None) -> list:
    sm = get_sessionmaker()
    async with sm() as s:
        q = select(Reminder).where(Reminder.user_id == user_id)
        if status:
            q = q.where(Reminder.status == status)
        q = q.order_by(Reminder.due_at)
        rows = (await s.execute(q)).scalars().all()
    return [_reminder_to_dict(r) for r in rows]


async def due_reminders(advance_window: bool = True) -> list:
    """跨用户返回所有 pending 且已到提醒窗口的提醒（调度器用）。"""
    sm = get_sessionmaker()
    now = int(time.time())
    async with sm() as s:
        rows = (await s.execute(
            select(Reminder).where(Reminder.status == "pending")
        )).scalars().all()
    out = []
    for r in rows:
        window = (r.due_at - r.advance_days * 86400) if advance_window else r.due_at
        if now >= window:
            out.append(_reminder_to_dict(r))
    return out


async def mark_reminder_sent(rid: str) -> bool:
    sm = get_sessionmaker()
    async with sm() as s:
        r = await s.get(Reminder, rid)
        if not r:
            return False
        r.status = "sent"
        r.last_sent_at = int(time.time())
        await s.commit()
        return True


async def mark_reminder_done(rid: str) -> bool:
    """手动标记提醒为已完成（停止回访/取消未到期提醒），保留记录。"""
    sm = get_sessionmaker()
    async with sm() as s:
        r = await s.get(Reminder, rid)
        if not r:
            return False
        r.status = "done"
        r.last_sent_at = int(time.time())
        await s.commit()
        return True


async def reschedule_reminder(rid: str) -> bool:
    sm = get_sessionmaker()
    async with sm() as s:
        r = await s.get(Reminder, rid)
        if not r:
            return False
        step = (r.repeat_interval_days or 1) * 86400
        r.due_at = r.due_at + step
        r.repeat_remaining = max(0, r.repeat_remaining - 1)
        r.last_sent_at = int(time.time())
        if r.repeat_remaining <= 0:
            r.status = "done"
        await s.commit()
        return True


async def delete_reminder(user_id: str, rid: str) -> bool:
    sm = get_sessionmaker()
    async with sm() as s:
        r = await s.get(Reminder, rid)
        if not r or r.user_id != user_id:
            return False
        await s.delete(r)
        await s.commit()
        return True
