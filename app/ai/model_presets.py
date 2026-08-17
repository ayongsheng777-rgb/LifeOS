"""LifeOS 模型预设目录（「完美模型配置模块」单一数据源）。

职责：
1. 预设厂商配置：deepseek / nVIDIA NIM / 阿里百炼(dashscope·qwen) / OpenAI /
   Moonshot(kimi) / Zhipu(glm) 等。UI 可直接pick，自动带回 base_url + 模型名。
2. 官方单价知识库：以「元/百万 token」为单位的输入/输出价（对齐
   Token统计面板指导文档 V1.2 §3.5 OFFICIAL_PRICING）。作为「Token 费用参考」与
   usage_store 费用折算的权威参照。

⚠️ 单价均为「公开价参考值」，厂商调价后会变化；UI 与计费逻辑都允许覆盖。
价格单位统一：元 / 每百万 (1e6) tokens；输入/输出分开定价（输出通常更贵）。
"""
from typing import Optional

# 厂商预设：UI 预设库 + 自动推断 provider 的依据
# pricing 单位 = 元 / 每百万 token（input_per_million, output_per_million）
PRESET_PROVIDERS: list[dict] = [
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "doc": "deepseek.com",
        "models": [
            {"id": "deepseek-chat", "name": "DeepSeek-V3 (chat)",
             "in_per_million": 1.0, "out_per_million": 2.0,
             "note": "官方价；缓存命中输入 ¥0.1/M"},
            {"id": "deepseek-reasoner", "name": "DeepSeek-R1 (reasoner)",
             "in_per_million": 4.0, "out_per_million": 16.0,
             "note": "官方价；缓存命中输入 ¥1/M"},
        ],
    },
    {
        "key": "nvidia",
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "doc": "build.nvidia.com",
        "models": [
            {"id": "meta/llama-3.1-8b-instruct", "name": "Llama 3.1 8B Instruct",
             "in_per_million": 4.3, "out_per_million": 5.8,
             "note": "NIM 列表价(USD→¥7.2)；参考"},
            {"id": "nvidia/llama-3.1-nemotron-70b-instruct",
             "name": "Nemotron 70B Instruct",
             "in_per_million": 9.4, "out_per_million": 10.8,
             "note": "NIM 列表价(USD→¥7.2)；参考"},
            {"id": "nvidia/llama-3.3-nemotron-super-49b-v1",
             "name": "Nemotron Super 49B",
             "in_per_million": 14.4, "out_per_million": 21.6,
             "note": "NIM 列表价(USD→¥7.2)；参考"},
            {"id": "microsoft/phi-3.5-mini-instruct", "name": "Phi-3.5 Mini",
             "in_per_million": 0.72, "out_per_million": 0.72,
             "note": "NIM 列表价(USD→¥7.2)；参考"},
        ],
    },
    {
        "key": "dashscope",
        "name": "阿里百炼 (通义千问)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "doc": "help.aliyun.com",
        "models": [
            {"id": "qwen-turbo", "name": "Qwen-Turbo",
             "in_per_million": 0.3, "out_per_million": 0.6, "note": "官方价"},
            {"id": "qwen-plus", "name": "Qwen-Plus",
             "in_per_million": 4.0, "out_per_million": 12.0,
             "note": "官方价；缓存命中输入 ¥0.4/M"},
            {"id": "qwen-max", "name": "Qwen-Max",
             "in_per_million": 20.0, "out_per_million": 60.0,
             "note": "官方价；缓存命中输入 ¥2/M"},
            {"id": "qwen-long", "name": "Qwen-Long",
             "in_per_million": 0.5, "out_per_million": 2.0, "note": "官方价"},
            {"id": "qwen2.5-72b-instruct", "name": "Qwen2.5-72B-Instruct",
             "in_per_million": 4.0, "out_per_million": 12.0,
             "note": "百炼开放模型官方价"},
            {"id": "qwen3-235b-a22b", "name": "Qwen3-235B-A22B",
             "in_per_million": 2.0, "out_per_million": 8.0,
             "note": "百炼官方价；参考"},
        ],
    },
    {
        "key": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "doc": "platform.openai.com",
        "models": [
            {"id": "gpt-4o-mini", "name": "GPT-4o mini",
             "in_per_million": 1.08, "out_per_million": 4.32,
             "note": "官方价(USD→¥7.2)；参考"},
            {"id": "gpt-4o", "name": "GPT-4o",
             "in_per_million": 18.0, "out_per_million": 72.0,
             "note": "官方价(USD→¥7.2)；参考"},
            {"id": "gpt-4.1", "name": "GPT-4.1",
             "in_per_million": 14.4, "out_per_million": 57.6,
             "note": "官方价(USD→¥7.2)；参考"},
            {"id": "o3-mini", "name": "o3-mini",
             "in_per_million": 3.6, "out_per_million": 14.4,
             "note": "官方价(USD→¥7.2)；参考"},
        ],
    },
    {
        "key": "moonshot",
        "name": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "doc": "moonshot.cn",
        "models": [
            {"id": "moonshot-v1-8k", "name": "Kimi V1 8K",
             "in_per_million": 1.2, "out_per_million": 1.2, "note": "官方价"},
            {"id": "moonshot-v1-32k", "name": "Kimi V1 32K",
             "in_per_million": 2.4, "out_per_million": 2.4, "note": "官方价"},
            {"id": "moonshot-v1-128k", "name": "Kimi V1 128K",
             "in_per_million": 6.0, "out_per_million": 6.0, "note": "官方价"},
        ],
    },
    {
        "key": "zhipu",
        "name": "智谱 AI (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "doc": "open.bigmodel.cn",
        "models": [
            {"id": "glm-4-flash", "name": "GLM-4-Flash",
             "in_per_million": 0.1, "out_per_million": 0.1, "note": "官方价(近乎免费)"},
            {"id": "glm-4-air", "name": "GLM-4-Air",
             "in_per_million": 1.0, "out_per_million": 1.0, "note": "官方价"},
            {"id": "glm-4-plus", "name": "GLM-4-Plus",
             "in_per_million": 50.0, "out_per_million": 50.0,
             "note": "官方价(¥0.05/千tokens)；参考"},
        ],
    },
]


def _build_pricing_kb() -> dict:
    """扁平化知识库：key=(provider, model) -> (in_per_million, out_per_million)。"""
    kb: dict = {}
    for p in PRESET_PROVIDERS:
        for m in p["models"]:
            kb[(p["key"], m["id"])] = (m["in_per_million"], m["out_per_million"])
    # 兜底：model 名唯一时可省略 provider（3.5 同款语义）
    for p in PRESET_PROVIDERS:
        for m in p["models"]:
            if (m["id"],) not in kb:
                kb[(m["id"],)] = (m["in_per_million"], m["out_per_million"])
    return kb


# 官方单价知识库（与 usage_store 共用）
OFFICIAL_PRICING_CNY: dict = _build_pricing_kb()


def provider_of(base_url: str) -> str:
    """从 base_url 推断渠道标识（与配置模块、计费同步一致）。

    命中顺序：知名厂商路径关键字 → 否则取主机名第一段。
    """
    u = (base_url or "").lower()
    rules = (
        ("deepseek", "api.deepseek.com"),
        ("dashscope", "dashscope"),
        ("nvidia", "nvidia.com"),
        ("moonshot", "moonshot.cn"),
        ("zhipu", "bigmodel.cn"),
        ("openai", "api.openai.com"),
    )
    for key, token in rules:
        if token in u:
            return key
    # 兜底：取主机名第一段
    try:
        from urllib.parse import urlparse
        netloc = urlparse(u).netloc or u
        host = netloc.split("@")[-1].split(":")[0]
        return host.split(".")[0] or "custom"
    except Exception:
        return "custom"


def estimate_cost_cny(model: str, input_tokens: int, output_tokens: int) -> float:
    """按模型名查官方单价知识库估算费用（元）。未知模型返回 0。"""
    key = model or ""
    price = OFFICIAL_PRICING_CNY.get((key,)) or OFFICIAL_PRICING_CNY.get(("", key))
    if not price:
        # 再尝试带 provider 的精确键（取第一个匹配）
        # 注意：知识库含 2 元组 (provider, model) 与 1 元组 (model,) 两种键，
        # 解包前必须判断长度，否则 1 元组键会抛 ValueError。
        for k, v in OFFICIAL_PRICING_CNY.items():
            if isinstance(k, tuple) and len(k) == 2:
                prov, mdl = k
                if isinstance(prov, str) and mdl == key:
                    price = v
                    break
    if not price:
        return 0.0
    in_price, out_price = price
    return round(in_price * (input_tokens / 1_000_000.0) +
                 out_price * (output_tokens / 1_000_000.0), 6)


def presets_for_ui() -> dict:
    """给前端预设库的精简结构（含厂商 + 模型 + 单价 + provider 推断线索）。"""
    return {
        "providers": [
            {
                "key": p["key"],
                "name": p["name"],
                "base_url": p["base_url"],
                "doc": p.get("doc", ""),
                "models": [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "in_per_million": m["in_per_million"],
                        "out_per_million": m["out_per_million"],
                        "note": m.get("note", ""),
                    }
                    for m in p["models"]
                ],
            }
            for p in PRESET_PROVIDERS
        ]
    }


def find_preset_model(provider_key: str, model_id: str) -> Optional[dict]:
    """按厂商 + 模型 id 找预设项（用于新增时自动带单价）。"""
    for p in PRESET_PROVIDERS:
        if p["key"] == provider_key:
            for m in p["models"]:
                if m["id"] == model_id:
                    return m
    return None
