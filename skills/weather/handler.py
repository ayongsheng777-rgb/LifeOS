"""天气查询完整技能包：实时天气 / 多日预报（Open-Meteo，免费无需 Key）。

- 数据来自 Open-Meteo 公开接口（geocoding-api + api.open-meteo.com），无需任何 API Key。
- 直连（trust_env=False），不继承系统/沙箱代理。
- 流程：从用户消息提取城市名 → 地理编码 → 取实时 + 未来 3 天预报 → 中文格式化。
"""
import httpx
from urllib.parse import quote

_GEO = "https://geocoding-api.open-meteo.com/v1/search"
_FC = "https://api.open-meteo.com/v1/forecast"
# 直连 Open-Meteo，不继承任何 HTTP_PROXY/HTTPS_PROXY 环境变量
_HTTP = dict(trust_env=False, timeout=httpx.Timeout(15))

# WMO 天气代码 → 中文描述
_WMO = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨（弱）", 53: "毛毛雨（中）", 55: "毛毛雨（强）",
    56: "冻毛毛雨（弱）", 57: "冻毛毛雨（强）",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨（弱）", 67: "冻雨（强）",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨（弱）", 81: "阵雨（中）", 82: "阵雨（强）",
    85: "阵雪（弱）", 86: "阵雪（强）",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}

# 从消息中提取城市时，去掉这些词（触发词 + 疑问词 + 标点）
_STOPWORDS = [
    "天气", "气温", "温度", "多少度", "下雨", "降雨", "晴天", "多云",
    "预报", "预测", "查询", "查一下", "查查", "看看", "今天", "明天",
    "后天", "现在", "当前", "实时", "怎么样", "怎样", "如何", "帮我",
    "我想知道", "附近", "会", "吗", "呢", "啊", "的", "了", "请",
    "？", "?", "。", ".", "！", "!", " ",
]


def _extract_city(message: str) -> str:
    s = message
    for w in _STOPWORDS:
        s = s.replace(w, " ")
    return " ".join(s.split()).strip()


async def _geocode(name: str):
    url = f"{_GEO}?name={quote(name)}&count=1&language=zh&format=json"
    async with httpx.AsyncClient(**_HTTP) as hc:
        r = await hc.get(url)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    data = r.json()
    res = data.get("results")
    if not res:
        return None, f"找不到「{name}」这个地点"
    g = res[0]
    return (g["latitude"], g["longitude"], g.get("name"), g.get("country"), g.get("admin1")), None


async def _forecast(lat, lon):
    url = (
        f"{_FC}?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=auto&forecast_days=3"
    )
    async with httpx.AsyncClient(**_HTTP) as hc:
        r = await hc.get(url)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.json(), None


def _fmt(geo, fc) -> str:
    name, country, admin1 = geo[2], geo[3], geo[4]
    loc = name
    if admin1 and admin1 != name:
        loc += f"（{admin1}）"
    if country and country not in ("中国", "China"):
        loc += f"，{country}"
    cur = fc.get("current", {})
    lines = [f"🌤️ {loc} 实时天气"]
    wc = _WMO.get(cur.get("weather_code"), "未知")
    lines.append(f"· {wc}，{cur.get('temperature_2m')}°C（体感 {cur.get('apparent_temperature')}°C）")
    lines.append(f"· 湿度 {cur.get('relative_humidity_2m')}%，风速 {cur.get('wind_speed_10m')} km/h")
    daily = fc.get("daily", {})
    times = daily.get("time", [])
    codes = daily.get("weather_code", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    pprob = daily.get("precipitation_probability_max", [])
    if times:
        lines.append("未来几天：")
        labels = ["今天", "明天", "后天"]
        for i in range(min(len(times), 3)):
            label = labels[i] if i < len(labels) else times[i][5:]
            lines.append(f"· {label}：{_WMO.get(codes[i], '?')} {tmin[i]}~{tmax[i]}°C，降水概率 {pprob[i]}%")
    return "\n".join(lines)


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        city = _extract_city(message)
        if not city:
            return ("请告诉我城市，例如：\n"
                    "· 天气 北京\n"
                    "· 上海今天天气\n"
                    "· 广州明天会下雨吗")
        geo, err = await _geocode(city)
        if err:
            return f"地理编码失败：{err}"
        fc, err2 = await _forecast(geo[0], geo[1])
        if err2:
            return f"获取天气失败：{err2}"
        return _fmt(geo, fc)
