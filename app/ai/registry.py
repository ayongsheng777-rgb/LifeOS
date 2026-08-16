"""模型能力注册表 + 场景/板块推荐（04-AI 第5节）。

- 能力标签：reasoning/long/zh/fast/cheap/vision/general
- infer_tags 按 model id 子串匹配（顺序敏感：deepseek-reasoner 须排在 deepseek 前）
- recommend：场景0.6 + 板块0.4 加权打分，返回排序+缺失能力+人话理由
"""
from dataclasses import dataclass, field

# 顺序敏感：长匹配在前
_TAG_RULES = [
    ("deepseek-reasoner", {"reasoning", "zh"}),
    ("deepseek-v4-pro", {"reasoning", "zh"}),
    ("deepseek", {"zh", "fast"}),
    ("kimi-k3", {"reasoning", "long", "zh"}),
    ("qwen", {"zh"}),
    ("glm", {"zh"}),
    ("minimax", {"zh"}),
    ("hy3", {"zh"}),
    ("gpt", {"general", "long"}),
    ("claude", {"general", "long"}),
    ("gemini", {"general", "vision", "long"}),
    ("grok", {"general"}),
]

# 场景 → 所需能力
SCENARIOS = {
    "stock": {"zh", "reasoning"},
    "institution": {"zh", "reasoning"},
    "lhb": {"zh"},
    "sector": {"zh", "reasoning"},
    "news": {"zh", "fast"},
}

# 板块 → 所需能力（可选增强）
SECTOR_HINTS = {
    "科技": {"reasoning"},
    "医药": {"reasoning"},
    "金融": {"zh"},
}


def infer_tags(model_id: str) -> set:
    mid = (model_id or "").lower()
    for needle, tags in _TAG_RULES:
        if needle in mid:
            return set(tags)
    return {"general"}


@dataclass
class RecResult:
    model_id: str
    name: str
    score: float
    missing: list = field(default_factory=list)
    reason: str = ""


def _score(tags: set, need: set) -> tuple:
    if not need:
        return 1.0, []
    hit = need & tags
    miss = sorted(need - tags)
    return len(hit) / len(need), miss


def recommend(profiles: list, scenario_id: str = "", sector_id: str = "") -> list:
    """profiles: [AIProfile|dict]。返回按分数降序的 RecResult 列表。"""
    need_sc = SCENARIOS.get(scenario_id, set())
    need_se = SECTOR_HINTS.get(sector_id, set())
    results = []
    for p in profiles:
        pid = p.id if hasattr(p, "id") else p.get("id", "default")
        pname = p.name if hasattr(p, "name") else p.get("name", pid)
        ptags = set(getattr(p, "tags", []) or []) if hasattr(p, "tags") else set(p.get("tags", []) or [])
        # 若 profile 未带 tags，按 id 推断
        if not ptags:
            ptags = infer_tags(pid)
        s1, m1 = _score(ptags, need_sc)
        s2, m2 = _score(ptags, need_se)
        score = s1 * 0.6 + s2 * 0.4
        missing = sorted(set(m1) | set(m2))
        reason = []
        if need_sc:
            reason.append(f"场景[{scenario_id}]需{','.join(sorted(need_sc))}")
        if need_se:
            reason.append(f"板块[{sector_id}]需{','.join(sorted(need_se))}")
        reason.append(f"命中{len(ptags & (need_sc | need_se))}项")
        results.append(RecResult(model_id=pid, name=pname, score=round(score, 2),
                                 missing=missing, reason="；".join(reason)))
    results.sort(key=lambda r: r.score, reverse=True)
    return results
