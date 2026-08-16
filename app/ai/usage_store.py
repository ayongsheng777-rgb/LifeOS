"""AI 用量 / 费用统计存储（Phase 4 AI Gateway）。

复用 db_store 的 SQLAlchemy 引擎与 Base；定义 ai_usage 表，
由 app 启动时 init_db() 的 Base.metadata.create_all 自动建表。

设计：
- 每次 AI 调用（无论成败）记录一条：模型、场景、输入/输出 token、估算费用、耗时、成败、错误。
- DB 未配置（无 DB_URL）时优雅 no-op，不阻断主流程。
- 费用按模型名查近似定价表估算（USD/1K tokens），仅作参考。
"""
import time
import uuid
import logging
from typing import Optional

from sqlalchemy import String, BigInteger, Boolean, Numeric, Text, Integer, select
from sqlalchemy.orm import Mapped, mapped_column

from app.skills.db_store import Base, get_sessionmaker

log = logging.getLogger("lifeos.ai.usage")

# 近似定价（USD / 1K tokens）：输入价, 输出价。仅作参考，非实时。
_PRICING = {
    "deepseek-chat": (0.00027, 0.0011),
    "deepseek-reasoner": (0.00055, 0.0022),
    "deepseek-v3": (0.00027, 0.0011),
    "deepseek-v4": (0.00027, 0.0011),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "glm-4": (0.00027, 0.0011),
    "qwen-max": (0.0004, 0.0012),
    "qwen-plus": (0.0002, 0.0006),
    "moonshot": (0.0006, 0.0018),
    "kimi-k3": (0.0006, 0.0018),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """按模型名估算费用（USD）。未知模型返回 0。"""
    price = _PRICING.get((model or "").lower())
    if not price:
        return 0.0
    in_price, out_price = price
    return round(in_price * (input_tokens / 1000.0) + out_price * (output_tokens / 1000.0), 6)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # type: ignore[assignment]
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(String(64), default="")
    scenario: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Numeric(10, 6), default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class UsageStore:
    async def record(self, user_id: str, *, model: str, scenario: str = None,
                     input_tokens: int = 0, output_tokens: int = 0,
                     latency_ms: int = 0, ok: bool = True, error: str = None) -> Optional[str]:
        """记录一条 AI 调用用量；DB 不可用/出错时静默返回 None（不阻断主流程）。"""
        try:
            sm = get_sessionmaker()
        except RuntimeError:
            return None
        row = AIUsage(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            created_at=int(time.time()),
            model=model or "",
            scenario=scenario,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cost=estimate_cost(model, int(input_tokens or 0), int(output_tokens or 0)),
            latency_ms=int(latency_ms or 0),
            ok=bool(ok),
            error=error,
        )
        try:
            async with sm() as s:
                s.add(row)
                await s.commit()
            return row.id
        except Exception as e:
            log.warning("ai_usage 记录失败（忽略）: %s", e)
            return None

    @staticmethod
    def _empty() -> dict:
        return {"calls": 0, "ok": 0, "fail": 0, "input_tokens": 0,
                "output_tokens": 0, "total_tokens": 0, "cost": 0.0, "per_model": {}}

    async def summary(self, user_id: str, since_ts: int = None) -> dict:
        """汇总某用户用量；DB 不可用时返回空结构。"""
        try:
            sm = get_sessionmaker()
        except RuntimeError:
            return self._empty()
        try:
            async with sm() as s:
                res = await s.execute(select(AIUsage).where(AIUsage.user_id == user_id))
                rows = res.scalars().all()
            if since_ts:
                rows = [r for r in rows if r.created_at >= since_ts]
            calls = len(rows)
            ok_calls = sum(1 for r in rows if r.ok)
            in_tok = sum(r.input_tokens for r in rows)
            out_tok = sum(r.output_tokens for r in rows)
            cost = sum(float(r.cost) for r in rows)
            per_model: dict = {}
            for r in rows:
                m = r.model or "unknown"
                d = per_model.setdefault(m, {"calls": 0, "tokens": 0, "cost": 0.0})
                d["calls"] += 1
                d["tokens"] += r.input_tokens + r.output_tokens
                d["cost"] = round(d["cost"] + float(r.cost), 6)
            return {
                "calls": calls, "ok": ok_calls, "fail": calls - ok_calls,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok, "cost": round(cost, 6),
                "per_model": per_model,
            }
        except Exception as e:
            log.warning("ai_usage 汇总失败（忽略）: %s", e)
            return self._empty()


# 单例
usage_store = UsageStore()
