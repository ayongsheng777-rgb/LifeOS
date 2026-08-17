"""LifeOS 中央配置：AI 多模型 + 飞书运行态 + 代理/网络。

设计要点（对齐 04-AI 指导 / 02-飞书指导）：
- 单例 `settings`，import 时从环境变量加载。
- 飞书凭据支持「扫码授权流」运行时写入并热生效（upsert_setting / apply_overrides）。
- AI 多模型库 + 场景分配：active_ai_profile() / get_scenario_profile() / available()。
- 密钥脱敏：mask_secret() 仅显示末4位。
"""
import os
import json
from dataclasses import dataclass, field
from typing import Optional

# 启用 .env 加载（若存在）：让 `cp .env.example .env` 真正生效，
# 便于在宿主/容器外注入 OTP_SECRET、AI_API_KEY、EMBEDDING_MODEL 等。
# 失败（未装 python-dotenv 或无 .env）时静默跳过，不影响现有行为。
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_PLACEHOLDER_KEYS = {"your", "xxx", "sk-xxx", "changeme", "placeholder", "todo", ""}


def mask_secret(s: str) -> str:
    """密钥脱敏：仅显示末4位，其余 ****。空值返回 ''。"""
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return "****" + s[-4:]


def _is_valid_key(key: str) -> bool:
    if not key:
        return False
    return key.strip().lower() not in _PLACEHOLDER_KEYS


@dataclass
class AIProfile:
    id: str = "default"
    name: str = "default"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    proxy: str = ""
    user_agent: str = ""
    tags: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "AIProfile":
        return cls(
            id=d.get("id", "default"),
            name=d.get("name", d.get("id", "default")),
            base_url=d.get("base_url", "https://api.deepseek.com/v1"),
            model=d.get("model", ""),
            api_key=d.get("api_key", ""),
            proxy=d.get("proxy", ""),
            user_agent=d.get("user_agent", ""),
            tags=d.get("tags", []) or [],
        )

    def has_key(self) -> bool:
        return _is_valid_key(self.api_key)


@dataclass
class Settings:
    # ---- 基础设施 ----
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"
    data_dir: str = "./data"
    db_url: str = ""   # Phase 1：PostgreSQL 连接串（postgresql+asyncpg://user:pw@host:port/db）；留空则待办/收支不可用

    # ---- AI（OpenAI 兼容）----
    ai_enabled: bool = False
    ai_base_url: str = "https://api.deepseek.com/v1"
    ai_api_key: str = ""
    ai_model: str = "deepseek-chat"
    ai_proxy: str = ""
    ai_user_agent: str = ""
    ai_models: list = field(default_factory=list)        # [dict]
    ai_active: str = "default"
    scenario_models: dict = field(default_factory=dict)  # {scenario: model_id}
    embedding_model: str = ""   # 长期记忆 embedding 模型（OpenAI 兼容 /embeddings）；留空则不启用长期记忆

    # ---- 飞书运行态（可运行时覆盖）----
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_enabled: bool = False
    feishu_trusted_bots: list = field(default_factory=list)
    feishu_admin_users: list = field(default_factory=list)

    # ---- OTP ----
    otp_issuer: str = "LifeOS"
    otp_account: str = "admin@lifeos"
    otp_secret: str = ""
    session_secret: str = ""
    session_ttl: int = 43200

    # ---- Phase 6 Connector ----
    connector_webhook_token: str = ""   # 入站 Webhook 共享密钥（留空则 /api/connector/webhook 不启用）

    # ---- 技能管理（设置页可配置）----
    # API 技能：纯配置驱动的 HTTP 调用技能（如高德地图），无需写代码即可新增。
    api_skills: list = field(default_factory=list)  # [dict] 字段约定见 upsert_api_skill

    def load_env(self) -> None:
        self.redis_url = os.environ.get("REDIS_URL", self.redis_url)
        self.qdrant_url = os.environ.get("QDRANT_URL", self.qdrant_url)
        self.data_dir = os.environ.get("DATA_DIR", self.data_dir)
        self.db_url = os.environ.get("DB_URL", self.db_url)

        self.ai_enabled = os.environ.get("AI_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        self.ai_base_url = os.environ.get("AI_BASE_URL", self.ai_base_url)
        self.ai_api_key = os.environ.get("AI_API_KEY", self.ai_api_key)
        self.ai_model = os.environ.get("AI_MODEL", self.ai_model)
        self.ai_proxy = os.environ.get("AI_PROXY", self.ai_proxy)
        self.ai_user_agent = os.environ.get("AI_USER_AGENT", self.ai_user_agent)
        self.ai_active = os.environ.get("AI_ACTIVE", self.ai_active)
        try:
            self.ai_models = json.loads(os.environ.get("AI_MODELS", "[]") or "[]")
        except json.JSONDecodeError:
            self.ai_models = []
        try:
            self.scenario_models = json.loads(os.environ.get("SCENARIO_MODELS", "{}") or "{}")
        except json.JSONDecodeError:
            self.scenario_models = {}
        self.embedding_model = os.environ.get("EMBEDDING_MODEL", self.embedding_model)

        self.feishu_app_id = os.environ.get("FEISHU_APP_ID", self.feishu_app_id)
        self.feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", self.feishu_app_secret)
        self.feishu_enabled = os.environ.get("FEISHU_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        try:
            self.feishu_trusted_bots = json.loads(os.environ.get("FEISHU_TRUSTED_BOTS", "[]") or "[]")
        except json.JSONDecodeError:
            self.feishu_trusted_bots = []
        try:
            self.feishu_admin_users = json.loads(os.environ.get("FEISHU_ADMIN_USERS", "[]") or "[]")
        except json.JSONDecodeError:
            self.feishu_admin_users = []

        self.otp_issuer = os.environ.get("OTP_ISSUER", self.otp_issuer)
        self.otp_account = os.environ.get("OTP_ACCOUNT", self.otp_account)
        self.otp_secret = os.environ.get("OTP_SECRET", self.otp_secret)
        self.session_secret = os.environ.get("SESSION_SECRET", self.session_secret)
        self.connector_webhook_token = os.environ.get("CONNECTOR_WEBHOOK_TOKEN", self.connector_webhook_token)
        try:
            self.session_ttl = int(os.environ.get("SESSION_TTL", str(self.session_ttl)))
        except ValueError:
            self.session_ttl = 43200

        # 运行时落库配置（扫码授权流写入）覆盖环境变量
        self._load_runtime()

    # ===== 运行时持久化（热更 + 重启保留）=====
    def _runtime_path(self) -> str:
        os.makedirs(self.data_dir, exist_ok=True)
        return os.path.join(self.data_dir, "settings_runtime.json")

    def _load_runtime(self) -> None:
        try:
            with open(self._runtime_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for k in ("feishu_app_id", "feishu_app_secret", "feishu_enabled",
                  "feishu_trusted_bots", "feishu_admin_users", "ai_enabled",
                  "ai_models", "ai_active", "scenario_models", "api_skills"):
            if k in data:
                setattr(self, k, data[k])

    def _save_runtime(self) -> None:
        keep = ("feishu_app_id", "feishu_app_secret", "feishu_enabled",
                "feishu_trusted_bots", "feishu_admin_users", "ai_enabled",
                "ai_models", "ai_active", "scenario_models", "api_skills")
        snap = {k: getattr(self, k) for k in keep}
        try:
            with open(self._runtime_path(), "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[config] 运行时配置落库失败: {e}")

    def upsert_setting(self, data: dict) -> None:
        """写入运行时设置（如飞书扫码授权结果），并落库。"""
        for k, v in data.items():
            # 密钥占位符保护：收到 **** 时保留原值
            if isinstance(v, str) and v.startswith("****") and hasattr(self, k):
                continue
            setattr(self, k, v)
        self._save_runtime()

    def apply_overrides(self, **kw) -> None:
        """免重启热更新（扫码授权成功后调用）。"""
        for k, v in kw.items():
            if isinstance(v, str) and v.startswith("****") and hasattr(self, k):
                continue
            setattr(self, k, v)
        self._save_runtime()

    # ===== 模型配置持久化（完美模型配置模块）=====
    def save_ai_profiles(self, models: list, active: str = None,
                         scenario_models: dict = None) -> None:
        """整批写回模型配置（重启保留）。"""
        self.ai_models = models or []
        if active is not None:
            self.ai_active = active
        if scenario_models is not None:
            self.scenario_models = scenario_models or {}
        self._save_runtime()

    def upsert_ai_model(self, model: dict) -> list:
        """新增或更新一条模型配置（按 id 去重），持久化并返回完整列表。"""
        mid = model.get("id")
        models = [m for m in self.ai_models if m.get("id") != mid]
        models.append(model)
        self.ai_models = models
        self._save_runtime()
        return self.ai_models

    def remove_ai_model(self, mid: str) -> list:
        """删除一条模型配置（按 id）；若删的是默认则退回第一条。持久化并返回列表。"""
        self.ai_models = [m for m in self.ai_models if m.get("id") != mid]
        if self.ai_active == mid:
            self.ai_active = self.ai_models[0].get("id") if self.ai_models else "default"
        self._save_runtime()
        return self.ai_models

    def set_active_ai_model(self, mid: str) -> None:
        """设置当前默认生效模型并持久化。"""
        self.ai_active = mid
        self._save_runtime()

    # ===== API 技能（设置页新增的可调用 HTTP 技能）=====
    # 字段约定（dict）：
    #   id: str（唯一，自动用 name 规范化）
    #   name: str（技能名，用于路由与展示）
    #   description: str
    #   trigger_keywords: list[str]
    #   api_url: str（支持 {query} 占位符）
    #   api_key: str（可选，作为 Bearer 注入）
    #   method: "GET" | "POST"（默认 GET）
    #   enabled: bool（默认 True）
    def upsert_api_skill(self, entry: dict) -> list:
        """新增或更新一条 API 技能（按 id 去重），持久化并返回完整列表。"""
        name = (entry.get("name") or "").strip()
        if not name:
            raise ValueError("name 不能为空")
        sid = entry.get("id") or name
        item = {
            "id": sid,
            "name": name,
            "description": entry.get("description", ""),
            "trigger_keywords": entry.get("trigger_keywords") or [],
            "api_url": entry.get("api_url", ""),
            "api_key": entry.get("api_key", ""),
            "method": (entry.get("method") or "GET").upper(),
            "enabled": bool(entry.get("enabled", True)),
        }
        skills = [s for s in self.api_skills if s.get("id") != sid]
        skills.append(item)
        self.api_skills = skills
        self._save_runtime()
        return self.api_skills

    def remove_api_skill(self, sid: str) -> list:
        """删除一条 API 技能（按 id），持久化并返回列表。"""
        self.api_skills = [s for s in self.api_skills if s.get("id") != sid]
        self._save_runtime()
        return self.api_skills

    def set_api_skill_enabled(self, sid: str, enabled: bool) -> None:
        """启用/停用一条 API 技能并持久化。"""
        for s in self.api_skills:
            if s.get("id") == sid:
                s["enabled"] = bool(enabled)
                break
        self._save_runtime()

    # ===== AI 配置辅助（04-AI 指导）=====
    def _single_profile(self) -> Optional[AIProfile]:
        if not _is_valid_key(self.ai_api_key):
            return None
        return AIProfile(
            id="default", name="default",
            base_url=self.ai_base_url, model=self.ai_model,
            api_key=self.ai_api_key, proxy=self.ai_proxy,
            user_agent=self.ai_user_agent,
        )

    def active_ai_profile(self) -> Optional[AIProfile]:
        """多模型库优先：找到 active id 且带有效 key 的；否则退回单配置。"""
        for m in self.ai_models:
            if m.get("id") == self.ai_active and _is_valid_key(m.get("api_key", "")):
                return AIProfile.from_dict(m)
        # 退回单模型配置
        sp = self._single_profile()
        if sp:
            return sp
        # 再退回模型库里任意带 key 的
        for m in self.ai_models:
            if _is_valid_key(m.get("api_key", "")):
                return AIProfile.from_dict(m)
        return None

    def get_scenario_profile(self, scenario: str) -> Optional[AIProfile]:
        """场景指派优先，未指派退回 active。"""
        mid = self.scenario_models.get(scenario)
        if mid:
            for m in self.ai_models:
                if m.get("id") == mid and _is_valid_key(m.get("api_key", "")):
                    return AIProfile.from_dict(m)
        return self.active_ai_profile()

    def available(self) -> bool:
        """放宽判定：ai_enabled 且 active 或模型库任一项带有效 key。"""
        if not self.ai_enabled:
            return False
        if self.active_ai_profile() is not None:
            return True
        return any(_is_valid_key(m.get("api_key", "")) for m in self.ai_models)


# 单例
settings = Settings()
settings.load_env()
