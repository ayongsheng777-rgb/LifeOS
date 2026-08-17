"""高德地图完整技能包：地点搜索 / 地理编码 / 路线规划。

- Key 取自 settings.api_skills 中 name=="高德地图" 的条目（UI 改 Key 立即生效）。
- 意图识别：
  * 含「到 / → / 至 / | / 去」→ 路线规划（先地理编码两端，再驾车路径规划）
  * 含「坐标 / 经纬度」→ 地理编码（地名→经纬度）
  * 默认 → POI 关键词搜索
- 直连高德（trust_env=False），不走系统/沙箱代理。
"""
import httpx
from urllib.parse import quote

_BASE = "https://restapi.amap.com"
# 直连高德，不继承任何 HTTP_PROXY/HTTPS_PROXY 环境变量
_HTTP = dict(trust_env=False, timeout=httpx.Timeout(15))


def _get_key() -> str:
    from app.config import settings
    for s in settings.api_skills:
        if s.get("name") == "高德地图":
            return s.get("api_key", "")
    return ""


async def _get_json(url: str):
    async with httpx.AsyncClient(**_HTTP) as hc:
        r = await hc.get(url)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    try:
        return r.json(), None
    except Exception:
        return None, r.text[:160]


def _extract_query(message: str, keywords: list) -> str:
    msg = message
    for kw in (keywords or []):
        if kw:
            msg = msg.replace(kw, "")
    return msg.strip()


def _split_route(q: str):
    for sep in ("到", "→", "至", "->", "|", "去"):
        if sep in q:
            a, b = q.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a and b:
                return a, b
    return None


async def _geocode(name: str, key: str):
    url = f"{_BASE}/v3/geocode/geo?address={quote(name)}&key={key}"
    data, err = await _get_json(url)
    if err:
        return None, err
    if data.get("status") != "1" or not data.get("geocodes"):
        return None, data.get("info", "地理编码失败")
    return data["geocodes"][0]["location"], None


async def _route(origin_name: str, dest_name: str, key: str) -> str:
    oloc, e1 = await _geocode(origin_name, key)
    if e1:
        return f"起点「{origin_name}」解析失败：{e1}"
    dloc, e2 = await _geocode(dest_name, key)
    if e2:
        return f"终点「{dest_name}」解析失败：{e2}"
    url = (f"{_BASE}/v3/direction/driving?origin={oloc}&destination={dloc}"
           f"&key={key}&extensions=base")
    data, err = await _get_json(url)
    if err:
        return f"路线规划请求失败：{err}"
    if data.get("status") != "1":
        return f"路线规划失败：{data.get('info')}"
    paths = data.get("route", {}).get("paths", [{}])
    if not paths:
        return "未规划出路线。"
    p = paths[0]
    km = int(p.get("distance", 0)) / 1000.0
    mins = int(p.get("duration", 0)) / 60.0
    steps = [s.get("instruction", "") for s in p.get("steps", []) if s.get("instruction")]
    lines = [f"🚗 {origin_name} → {dest_name}",
             f"全程约 {km:.1f} 公里，预计 {mins:.0f} 分钟"]
    if steps:
        lines.append("路线指引：")
        for i, s in enumerate(steps[:10], 1):
            lines.append(f"{i}. {s}")
        if len(steps) > 10:
            lines.append(f"…（共 {len(steps)} 步）")
    return "\n".join(lines)


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        key = _get_key()
        if not key:
            return "（高德地图未配置 API Key：请在「设置→技能管理→API 技能」中填写高德 Key）"
        q = _extract_query(message, self.metadata.get("trigger_keywords", []))
        if not q:
            return ("请告诉我地点，例如：\n"
                    "· 高德 故宫（搜索景点）\n"
                    "· 高德 北京天安门 坐标（查经纬度）\n"
                    "· 高德 北京到上海（路线规划）")
        # 1) 路线规划
        route = _split_route(q)
        if route:
            return await _route(route[0], route[1], key)
        # 2) 地理编码（坐标 / 经纬度）
        if any(k in q for k in ("坐标", "经纬度")):
            name = q.replace("坐标", "").replace("经纬度", "").strip()
            loc, err = await _geocode(name, key)
            if err:
                return f"地理编码失败：{err}"
            return f"📍 {name}\n坐标：{loc}"
        # 3) 默认 POI 关键词搜索
        url = f"{_BASE}/v3/place/text?keywords={quote(q)}&key={key}&extensions=base"
        data, err = await _get_json(url)
        if err:
            return f"搜索失败：{err}"
        if data.get("status") != "1":
            return f"搜索失败：{data.get('info')}"
        pois = data.get("pois", [])[:5]
        if not pois:
            return f"未找到与「{q}」相关的地点。"
        lines = [f"🔍 {q} 相关地点："]
        for p in pois:
            lines.append(f"· {p.get('name')} — {p.get('address', '')}（{p.get('location', '')}）")
        return "\n".join(lines)
