"""新闻语义研判（04-AI：engine/news_ai）。

走 client 统一入口；AI 不可用时返回 None（调用方降级为仅存原文）。
news_score 融合口径：AI 情绪 ×0.7 + 规则 ×0.3，再乘 impact/level/credibility 衰减，映射到 0~100。
"""
from app.ai import client
from app.ai.prompt import NEWS_SYSTEM, news_user
from app.config import settings

_LEVELS = ("个股", "板块", "宏观")
_PROFIT = ("正面", "中性", "负面", "不确定")
_HORIZONS = ("短期", "中期", "长期")
_RULE_SENT = {"正面": 1.0, "负面": -1.0, "中性": 0.0, "不确定": 0.0}


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _pick(v, allowed, default):
    if v in allowed:
        return v
    # 包含匹配兜底（如“吸筹期”→“吸筹”）
    for a in allowed:
        if isinstance(v, str) and a in v:
            return a
    return default


async def interpret_text(text: str, attachment: str = "") -> dict | None:
    if not client.available():
        return None
    raw = await client.chat_json(
        NEWS_SYSTEM, news_user(text, attachment),
        model_profile=settings.get_scenario_profile("news"),
        max_tokens=800, temperature=0.2, cache_ttl=900,
    )
    if not isinstance(raw, dict):
        return None
    return {
        "sentiment": _clamp(raw.get("sentiment", 0), -1, 1),
        "impact": int(_clamp(raw.get("impact", 1), 1, 5)),
        "horizon": _pick(raw.get("horizon", "短期"), _HORIZONS, "短期"),
        "level": _pick(raw.get("level", "个股"), _LEVELS, "个股"),
        "credibility": _clamp(raw.get("credibility", 0.5), 0, 1),
        "profit_impact": _pick(raw.get("profit_impact", "不确定"), _PROFIT, "不确定"),
        "reason": str(raw.get("reason", ""))[:500],
        "key_events": list(raw.get("key_events", []))[:10],
        "src": "ai",
    }


def news_score(data: dict | None) -> int:
    """融合打分映射到 0~100（50 为中性）。"""
    if not data:
        return 0
    sentiment = _clamp(data.get("sentiment", 0), -1, 1)
    impact = int(_clamp(data.get("impact", 1), 1, 5))
    level = data.get("level", "个股")
    credibility = _clamp(data.get("credibility", 0.5), 0, 1)

    rule_sent = _RULE_SENT.get(data.get("profit_impact"), 0.0)
    mixed = sentiment * 0.7 + rule_sent * 0.3

    level_w = {"个股": 0.8, "板块": 1.0, "宏观": 1.0}.get(level, 0.8)
    decay = (impact / 5.0) * level_w * credibility
    score = mixed * 100 * decay
    return int(max(0, min(100, 50 + score)))
