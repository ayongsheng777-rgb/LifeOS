"""抖音公开数据技能：实时热榜 / 关键词搜索。

等价于 SkillHub 技能 guaikei-douyin-track-hot-topics 的调用能力，但直接用 httpx
调 guaikei.com 聚合接口，无需 Node 运行时。

- Token 读取顺序：skillhub 配置(.skill_config.json 的 GUAIKEI_API_TOKEN) → 环境变量 GUAIKEI_API_TOKEN。
- 意图：含「热搜/热榜/热门/热点/什么火」→ 热榜；否则视为关键词搜索（去掉触发词后为关键词）。
- 直连 guaikei（trust_env=False，国内接口不走代理）。

依赖懒导入（execute 内）避免与 app.skillhub 形成初始化期的循环导入。
"""
import os
import time
import httpx
from urllib.parse import urlencode

_BASE = "https://www.guaikei.com"
_SLUG = "guaikei-douyin-track-hot-topics"
# 直连国内接口，不继承任何 HTTP_PROXY/HTTPS_PROXY
_HTTP = dict(trust_env=False, timeout=httpx.Timeout(20))

# 热榜意图关键词
_HOT_KW = ("热搜", "热榜", "热门", "热点", "什么火", "什么热门", "榜单", "今天什么")


def _get_token() -> str:
    # 1) 优先 skillhub 配置（飞书「安装 @ns/slug，key 是 xxx」写入的就是这里）
    try:
        from app.skillhub import get_skill_config
        cfg = get_skill_config(_SLUG) or {}
        t = cfg.get("GUAIKEI_API_TOKEN", "")
        if t:
            return t
    except Exception:
        pass
    # 2) 回退环境变量
    return os.environ.get("GUAIKEI_API_TOKEN", "")


async def _get_json(path: str, params: dict):
    url = f"{_BASE}{path}?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(**_HTTP) as hc:
            r = await hc.get(url)
    except Exception as e:
        return None, f"请求异常：{type(e).__name__}: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    try:
        return r.json(), None
    except Exception:
        return None, r.text[:160]


async def _post_json(path: str, params: dict, data: dict):
    url = f"{_BASE}{path}?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(**_HTTP) as hc:
            r = await hc.post(url, json=data)
    except Exception as e:
        return None, f"请求异常：{type(e).__name__}: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    try:
        return r.json(), None
    except Exception:
        return None, r.text[:160]


def _fmt_num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


async def _hot(token: str) -> str:
    data, err = await _get_json("/api/douyin/hot-search", {"_": int(time.time() * 1000), "token": token})
    if err:
        return f"抖音热榜获取失败：{err}"
    if not isinstance(data, dict) or data.get("errcode") != 0:
        code = (data or {}).get("errcode") if isinstance(data, dict) else "?"
        msg = (data or {}).get("errmsg", "") if isinstance(data, dict) else ""
        if code == 401:
            return "抖音 Token 无效或未配置。请在飞书发：\n「安装 @user_7a394f02/guaikei-douyin-track-hot-topics，key 是 <你的Token>」"
        return f"抖音热榜接口返回错误（errcode={code}）：{msg}"
    items = data.get("data") or []
    if not items:
        return "抖音热榜暂无数据，稍后再试。"
    lines = ["🔥 抖音实时热榜 Top%d：" % min(len(items), 20)]
    for it in items[:20]:
        word = it.get("word") or "?"
        pos = it.get("position", "")
        hot = _fmt_num(it.get("hot_value"))
        view = _fmt_num(it.get("view_count"))
        lines.append(f"{pos}. {word}　热度{hot}　搜索{view}")
    return "\n".join(lines)


async def _search(token: str, keyword: str, limit: int = 10) -> str:
    base_params = {"_": int(time.time() * 1000), "token": token}
    # ① 创建任务
    body = {
        "keyword": keyword,
        "sort_type": 0,
        "publish_time": 0,
        "filter_duration": 0,
        "content_type": 0,
        "limit": limit,
    }
    _, err = await _post_json("/api/douyin/general-search/keyword", dict(base_params), body)
    if err:
        return f"抖音搜索失败（建任务）：{err}"
    # ② 查询结果（接口内部可能仍在处理，简单重试几次）
    data = None
    for _ in range(10):
        data, err = await _get_json("/api/douyin/general-search/info", {
            **base_params,
            "keyword": keyword,
            "sort_type": 0,
            "publish_time": 0,
            "filter_duration": 0,
            "content_type": 0,
            "limit": limit,
        })
        if err:
            return f"抖音搜索失败（查结果）：{err}"
        if isinstance(data, dict) and data.get("errcode") == 0 and data.get("data"):
            break
        await _sleep(2)
    if not isinstance(data, dict) or data.get("errcode") != 0:
        code = (data or {}).get("errcode") if isinstance(data, dict) else "?"
        if code == 401:
            return "抖音 Token 无效或未配置。请在飞书发：\n「安装 @user_7a394f02/guaikei-douyin-track-hot-topics，key 是 <你的Token>」"
        msg = (data or {}).get("errmsg", "") if isinstance(data, dict) else ""
        return f"抖音搜索接口返回错误（errcode={code}）：{msg}"
    items = data.get("data") or []
    if not items:
        return f"没有搜到「{keyword}」相关的抖音内容。"
    lines = [f"🔍 「{keyword}」抖音结果 Top{min(len(items), limit)}："]
    for it in items[:limit]:
        desc = (it.get("desc") or "").strip().replace("\n", " ")[:40]
        nick = it.get("author_nickname") or "?"
        digg = _fmt_num(it.get("digg_count"))
        url = it.get("share_url") or it.get("url") or ""
        line = f"· {desc}｜作者 {nick}｜👍{digg}"
        if url:
            line += f"｜{url}"
        lines.append(line)
    return "\n".join(lines)


async def _sleep(sec: float):
    import asyncio
    await asyncio.sleep(sec)


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        token = _get_token()
        if not token:
            return ("（抖音技能未配置 Token）\n请在飞书发：\n"
                    "「安装 @user_7a394f02/guaikei-douyin-track-hot-topics，key 是 <你的Token>」\n"
                    "Token 获取见抖音技能 readme（www.guaikei.com）。")

        msg = message
        # 去掉触发词，留出真正的查询内容
        q = msg
        for kw in (self.metadata.get("trigger_keywords") or []):
            if kw:
                q = q.replace(kw, "").strip()

        # 意图：热榜 vs 搜索
        if any(k in msg for k in _HOT_KW):
            return await _hot(token)

        # 无明确热榜意图 → 尝试关键词搜索
        q = q.strip(" ，。！？、的了吗呢呀")
        if not q or len(q) < 2:
            return await _hot(token)  # 只说了「抖音」，默认给热榜
        return await _search(token, q)
