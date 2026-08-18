"""AI 用量 / 费用统计存储（Phase 4 AI Gateway）。

复用 db_store 的 SQLAlchemy 引擎与 Base；定义 ai_usage 表，
由 app 启动时 init_db() 的 Base.metadata.create_all 自动建表。

设计：
- 每次 AI 调用（无论成败）记录一条：模型、场景、输入/输出 token、估算费用、耗时、成败、错误。
- DB 未配置（无 DB_URL）时优雅 no-op，不阻断主流程。
- 费用按模型名查官方单价知识库估算（元/每百万 token，见 app.ai.model_presets），
  仅作参考；未知模型记 0。
"""
import time
import uuid
import logging
from typing import Optional

from sqlalchemy import String, BigInteger, Boolean, Numeric, Text, Integer, select, func, Index, cast
from sqlalchemy.orm import Mapped, mapped_column

from app.skills.db_store import Base, get_sessionmaker

log = logging.getLogger("lifeos.ai.usage")

# 费用单位：元（CNY）。单价取自 model_presets.OFFICIAL_PRICING_CNY
# （元/每百万 token，输入/输出分开），作为 Token 费用统计参考。
# 未知模型返回 0（不估算），与指导文档「无 usage 记 0」原则一致。
from app.ai.model_presets import estimate_cost_cny  # noqa: E402


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """按模型名查官方单价知识库估算费用（元）。未知模型返回 0。"""
    return estimate_cost_cny(model, input_tokens, output_tokens)


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

    __table_args__ = (
        Index("ix_ai_usage_user_created", "user_id", "created_at"),
    )


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

    async def daily_summary(self, user_id: str, days: int = 14) -> list:
        """按天聚合用量（近 N 天），用于趋势图。DB 不可用时返回空列表。"""
        try:
            sm = get_sessionmaker()
        except RuntimeError:
            return []
        try:
            since = int(time.time()) - days * 86400
            async with sm() as s:
                res = await s.execute(
                    select(AIUsage).where(AIUsage.user_id == user_id, AIUsage.created_at >= since)
                )
                rows = res.scalars().all()
            by_day: dict = {}
            for r in rows:
                day = time.strftime("%Y-%m-%d", time.localtime(r.created_at))
                d = by_day.setdefault(day, {"date": day, "calls": 0, "tokens": 0,
                                            "cost": 0.0, "ok": 0, "fail": 0})
                d["calls"] += 1
                d["tokens"] += int(r.input_tokens or 0) + int(r.output_tokens or 0)
                d["cost"] = round(d["cost"] + float(r.cost or 0), 6)
                if r.ok:
                    d["ok"] += 1
                else:
                    d["fail"] += 1
            return [by_day[d] for d in sorted(by_day)]
        except Exception as e:
            log.warning("ai_usage 按天汇总失败（忽略）: %s", e)
            return []

    async def summary(self, user_id: str, since_ts: int = None) -> dict:
        """汇总某用户用量（SQL 聚合下推，避免全表拉行）。DB 不可用时返回空结构。"""
        try:
            sm = get_sessionmaker()
        except RuntimeError:
            return self._empty()
        try:
            base = select(AIUsage).where(AIUsage.user_id == user_id)
            if since_ts:
                base = base.where(AIUsage.created_at >= since_ts)
            sub = base.subquery()
            async with sm() as s:
                # 总览：单条聚合
                tot = (await s.execute(
                    select(func.count(),
                           func.coalesce(func.sum(AIUsage.input_tokens), 0),
                           func.coalesce(func.sum(AIUsage.output_tokens), 0),
                           func.coalesce(func.sum(AIUsage.cost), 0.0),
                           func.coalesce(func.sum(cast(AIUsage.ok, Integer)), 0))
                    .select_from(sub)
                )).first()
                # 按模型分组：calls/tokens/cost
                rows = (await s.execute(
                    select(AIUsage.model, func.count(),
                           func.coalesce(func.sum(AIUsage.input_tokens), 0),
                           func.coalesce(func.sum(AIUsage.output_tokens), 0),
                           func.coalesce(func.sum(AIUsage.cost), 0.0))
                    .select_from(sub).group_by(AIUsage.model)
                )).all()
            calls = int(tot[0] or 0)
            ok_calls = int(tot[4] or 0)
            in_tok = int(tot[1] or 0)
            out_tok = int(tot[2] or 0)
            cost = round(float(tot[3] or 0), 6)
            per_model: dict = {}
            for m, c, it, ot, co in rows:
                per_model[m or "unknown"] = {
                    "calls": int(c),
                    "tokens": int(it) + int(ot),
                    "cost": round(float(co or 0), 6),
                }
            return {
                "calls": calls, "ok": ok_calls, "fail": calls - ok_calls,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok, "cost": cost,
                "per_model": per_model,
            }
        except Exception as e:
            log.warning("ai_usage 汇总失败（忽略）: %s", e)
            return self._empty()


# 单例
usage_store = UsageStore()
