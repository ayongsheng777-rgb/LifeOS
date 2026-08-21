"""通用 AI 参数理解层（slots）——让技能用自然语言理解替代机械字符串裁剪。

设计目标（学习进化型）：
- 把「用户到底想查什么 / 地点是哪个 / 起终点是什么」交给 AI 读懂，而非抠字符串；
- 注入多轮上下文（短期记忆）+ 个人长期记忆（事实块），让理解越聊越懂：
  上一句说过「荆州」，这一句「明天呢」也能接上；记过「我住荆州」，直接默认查荆州；
- AI 不可用 / 提取失败 → 一律返回 None，由调用方回退原有字符串逻辑，绝不卡死主流程。
"""
import logging

from app.ai import client

log = logging.getLogger("lifeos.ai.slots")

# ===== 天气：提取「要查天气的地点」 =====
WEATHER_SLOT_SYSTEM = (
    "你是丽素（LifeOS）的天气地点提取器。从用户的话里提取『要查询天气的地点』。\n"
    "只输出 JSON：{\"has_location\": true|false, \"city\": \"地点名\"}\n"
    "规则：\n"
    "1. 忽略寒暄（你好、谢谢等）、疑问词和语气词，只找真正的地名。\n"
    "2. 城市名去掉多余前缀/后缀：『湖北荆州』→『荆州』，『荆州市』→『荆州』，"
    "『北京天安门』→『北京』。\n"
    "3. 用户没提地点 → has_location=false，city 留空。\n"
    "4. 若本轮没提地点、但上下文/长期记忆里能确定地点，用那个地点并 has_location=true。\n"
    "5. 拿不准地点是否存在 → 仍 has_location=true，把最可能的城市名填上（由后续接口校验）。"
)

# ===== 地图：提取意图 + 起终点 =====
AMAP_SLOT_SYSTEM = (
    "你是丽素（LifeOS）的地图意图提取器。从用户的话里提取地图查询的意图和参数。\n"
    "只输出 JSON：{\"intent\": \"search\"|\"geocode\"|\"route\", \"q\": \"关键词/地点\", "
    "\"from\": \"起点\", \"to\": \"终点\"}\n"
    "规则：\n"
    "1. 含『到/去/→/至/从…到…』且两端都是地点 → intent=route，填 from/to。\n"
    "2. 含『坐标/经纬度』→ intent=geocode，q=地点名。\n"
    "3. 其它 → intent=search，q=关键词。\n"
    "4. 提取不出就留空对应字段。"
)


def _render_context(context: list) -> str:
    """把多轮上下文（含长期记忆 system 条目）渲染成可读文本块。"""
    if not context:
        return ""
    lines = []
    for c in context[-6:]:
        role = c.get("role") if isinstance(c, dict) else ""
        content = c.get("content", "") if isinstance(c, dict) else str(c)
        if not content:
            continue
        if role == "system":
            lines.append(f"[长期记忆] {content}")
        elif role == "user":
            lines.append(f"用户: {content}")
        else:
            lines.append(f"助手: {content}")
    return "\n".join(lines)


async def extract_weather_location(message: str, context: list) -> dict | None:
    """AI 提取天气地点；失败返回 None（调用方回退字符串裁剪）。"""
    if not client.available():
        return None
    ctx = _render_context(context)
    user = f"{ctx}\n用户：{message}" if ctx else message
    try:
        res = await client.chat_json(WEATHER_SLOT_SYSTEM, user, cache_ttl=60, scenario="slot")
    except Exception as e:
        log.warning("天气地点提取失败（回退裁剪）: %s", e)
        return None
    return res if isinstance(res, dict) else None


async def extract_amap_intent(message: str, context: list) -> dict | None:
    """AI 提取地图意图与起终点；失败返回 None（调用方回退原逻辑）。"""
    if not client.available():
        return None
    ctx = _render_context(context)
    user = f"{ctx}\n用户：{message}" if ctx else message
    try:
        res = await client.chat_json(AMAP_SLOT_SYSTEM, user, cache_ttl=60, scenario="slot")
    except Exception as e:
        log.warning("地图意图提取失败（回退原逻辑）: %s", e)
        return None
    return res if isinstance(res, dict) else None
