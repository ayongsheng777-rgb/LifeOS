"""研判编排层（04-AI 第4节）：固定三段式。

每个分析函数：① 不可用时返回 None ② 调 chat_json（显式传场景 profile）③ 输出规整钳制。
本文件给一个通用 analyze() + 一个健康场景示例（仅信息整理，非诊断）。
"""
from app.ai import client
from app.config import settings

HEALTH_SYSTEM = (
    "你是个人健康记录助手，只做信息整理与提醒，不做医学诊断，不替代医生。"
    "用户描述身体感受或用药，请提炼：主要症状/用药、 urgency(低/中/高)、"
    "建议行动（如休息/就医提醒）、注意点。只输出 JSON："
    "{symptoms, medication, urgency, action, note}。"
)


async def analyze(system: str, user: str, scenario: str = "", *, max_tokens: int = 1024,
                  temperature: float = 0.3, cache_ttl: int = 900) -> dict | None:
    if not client.available():
        return None
    mp = settings.get_scenario_profile(scenario) if scenario else None
    data = await client.chat_json(
        system, user, model_profile=mp, max_tokens=max_tokens,
        temperature=temperature, cache_ttl=cache_ttl,
    )
    if not isinstance(data, dict):
        return None
    data["src"] = "ai"
    return data


async def analyze_health(text: str) -> dict | None:
    return await analyze(HEALTH_SYSTEM, text, "health", max_tokens=600, temperature=0.2)
