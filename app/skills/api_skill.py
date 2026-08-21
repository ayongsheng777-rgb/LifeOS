"""API 技能：纯配置驱动的可调用 HTTP 技能（如高德地图、天气查询等）。

无需写代码，在设置页填「请求地址模板 + Key + 触发词」即可新增。
- api_url 支持 {query} 占位符（用户消息去掉触发词后的剩余文本）。
- method 支持 GET / POST（默认 GET）。
- api_key 可选，注入为 Authorization: Bearer <key>。
- 容错：超时/异常/非 200 均给友好提示，不崩主流程。

同时提供「完整技能包」写入与热加载能力（write_skill_package / 由 router 调用 reload）。
"""
import os
import sys
import json
import httpx
from urllib.parse import quote

# 代理（与 AI 客户端一致：本地网关走 127.0.0.1:1080）
_PROXY = os.environ.get("LIFEOS_PROXY", "") or None

# 技能包目录：与 loader.SkillRegistry 解析一致（SKILLS_DIR 默认 "skills"，相对 cwd）
SKILLS_DIR = os.environ.get("SKILLS_DIR", "skills")
SKILLS_ROOT = os.path.abspath(SKILLS_DIR)


def mask_secret(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return "****" + s[-4:]


class ApiSkillHandler:
    """配置驱动的 HTTP 调用技能。

    约定：metadata 含 name/description/trigger_keywords/api_url/api_key/method。
    """

    def __init__(self, metadata: dict):
        self.metadata = metadata
        self.api_url = metadata.get("api_url", "")
        self.api_key = metadata.get("api_key", "")
        self.method = (metadata.get("method") or "GET").upper()
        # 半自动档：配了 process_prompt 时，取到数据后再交给 LLM 按此提示词加工成结论
        self.process_prompt = metadata.get("process_prompt", "")

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        query = self._extract_query(message)

        # ① 取原始数据：无 api_url 时用用户消息本身作为输入（纯 LLM 加工型，如合同审查）
        if self.api_url:
            raw, err = await self._fetch(query)
            if err:
                return err
        else:
            raw = query or message

        # ② 半自动加工：配了 process_prompt 就走 LLM 加工，否则原样返回（纯查数型）
        if self.process_prompt:
            return await self._process(raw, message)
        return raw

    async def _fetch(self, query: str) -> tuple:
        """返回 (raw_text, err)；err 非空表示失败。"""
        url = self.api_url
        if "{query}" in url:
            url = url.replace("{query}", quote(query))
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(proxy=_PROXY, timeout=httpx.Timeout(15)) as hc:
                if self.method == "POST":
                    resp = await hc.post(url, headers=headers, json={"query": query})
                else:
                    resp = await hc.get(url, headers=headers)
        except Exception as e:
            return "", f"【{self.metadata.get('name')}】调用异常：{type(e).__name__}: {e}"
        if resp.status_code != 200:
            return "", f"【{self.metadata.get('name')}】调用失败（HTTP {resp.status_code}）：{resp.text[:200]}"
        # 优先按 JSON 规整返回，失败则原样返回文本
        try:
            data = resp.json()
            if isinstance(data, (dict, list)):
                return json.dumps(data, ensure_ascii=False, indent=2)[:4000], ""
        except Exception:
            pass
        return resp.text[:4000], ""

    async def _process(self, raw: str, message: str) -> str:
        """半自动加工：把原始数据 + 用户问题交给 LLM，按 process_prompt 输出结论。"""
        from app.ai import client
        if not client.available():
            return raw
        user = f"用户问题：{message}\n\n原始数据：\n{raw}"
        try:
            out = await client.chat(self.process_prompt, user, temperature=0.3, max_tokens=1500)
        except Exception:
            return raw
        return out or raw

    def _extract_query(self, message: str) -> str:
        """去掉触发词后的剩余文本作为查询。"""
        msg = message
        for kw in (self.metadata.get("trigger_keywords") or []):
            if kw:
                msg = msg.replace(kw, "")
        return msg.strip()


def build_api_skills_into(registry) -> None:
    """把 settings.api_skills 中已启用的条目注册进 SkillRegistry。

    代码包（skills/ 目录）同名技能优先：若 sid 已存在于 registry，
    说明已有完整的 Python 技能包接管，纯配置型 ApiSkillHandler 不覆盖它
    （例如「高德地图」已由 skills/amap 完整包实现，仅借用其 api_key）。
    """
    from app.config import settings
    for s in settings.api_skills:
        if not s.get("enabled", True):
            continue
        sid = s.get("id") or s.get("name")
        if not sid:
            continue
        if sid in registry.skills:
            # 已被 skills/ 代码包接管（如高德地图），跳过避免覆盖
            continue
        metadata = {
            "name": sid,
            "description": s.get("description", ""),
            "trigger_keywords": s.get("trigger_keywords", []),
            "api_url": s.get("api_url", ""),
            "api_key": s.get("api_key", ""),
            "method": s.get("method", "GET"),
            "process_prompt": s.get("process_prompt", ""),
            "_type": "api",
        }
        registry.skills[sid] = ApiSkillHandler(metadata)


def sanitize_skill_name(name: str) -> str:
    """校验技能包名：仅允许字母数字下划线，防止路径穿越。"""
    name = (name or "").strip()
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("技能包名仅允许字母、数字、下划线")
    return name


# ===== 技能包 handler 静态安全校验（O-4：阻断明显 RCE / 破坏型代码）=====
# 单机单用户系统下技能即本进程内执行的 Python；此清单用于在写入前拦掉
# 最典型的远程代码执行与破坏性文件操作，降低「拿到令牌即 RCE」的风险面。
# 注意：这是最小可信基线，不是沙箱；仍只应添加自己信任的代码。
_DENY_PATTERNS = (
    "os.system", "os.popen", "subprocess", "eval(", "exec(",
    "__import__(", "shutil.rmtree", "os.remove", "os.rmdir", "os.unlink",
    "os.kill", "os.exec", "popen(", "import subprocess",
)


def validate_handler_code(code: str) -> tuple[bool, str]:
    """返回 (是否允许, 拒绝原因)。命中危险模式即拒绝。"""
    if not code:
        return False, "handler_code 为空"
    lowered = code.lower()
    for pat in _DENY_PATTERNS:
        if pat.lower() in lowered:
            return False, f"handler_code 包含被禁止的危险调用：{pat!r}（技能包执行在本进程，禁止 RCE/破坏性操作）"
    return True, ""


def write_skill_package(name: str, description: str, trigger_keywords: list,
                        handler_code: str) -> str:
    """写入一个完整技能包（skill.yaml + handler.py），返回目录路径。

    安全：name 已 sanitize；文件内容由调用方负责（本地单用户系统）。
    """
    name = sanitize_skill_name(name)
    folder = os.path.join(SKILLS_ROOT, name)
    os.makedirs(folder, exist_ok=True)
    yaml_path = os.path.join(folder, "skill.yaml")
    handler_path = os.path.join(folder, "handler.py")
    # skill.yaml 不保留空触发词
    kws = [k for k in (trigger_keywords or []) if k]
    meta = {
        "name": name,
        "description": description or name,
    }
    if kws:
        meta["trigger_keywords"] = kws
    with open(yaml_path, "w", encoding="utf-8") as f:
        import yaml
        yaml.safe_dump(meta, f, allow_unicode=True, sort_keys=False)
    with open(handler_path, "w", encoding="utf-8") as f:
        f.write(handler_code)
    return folder


def delete_skill_package(name: str) -> None:
    """删除一个完整技能包目录（含 skill.yaml + handler.py）。"""
    name = sanitize_skill_name(name)
    folder = os.path.join(SKILLS_ROOT, name)
    if os.path.isdir(folder):
        import shutil
        # 仅删除已知技能文件，避免误删整目录外内容
        for fn in ("skill.yaml", "handler.py"):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp):
                os.remove(fp)
        # 若目录已空（仅剩 __pycache__），一并清理
        try:
            rem = os.listdir(folder)
        except OSError:
            rem = []
        if all(x == "__pycache__" or x.startswith(".") for x in rem):
            shutil.rmtree(folder, ignore_errors=True)
