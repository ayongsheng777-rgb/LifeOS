"""SkillHub 技能集成：扫描挂载的技能目录、读写 key 配置、安装技能。

设计约定（阿勇拍板「飞书只装+配，跑还是小布」）：
- 技能目录：SKILLHUB_SKILLS_DIR（容器内 /app/skillhub_skills，即宿主机 ~/.workbuddy/skills 挂载）
- key 配置：<skills_dir>/.skill_config.json（宿主机/容器共享，WorkBuddy 执行时读取并设为环境变量）
- 安装：调用挂载进来的 skillhub CLI（SKILLHUB_CLI，纯 Python 标准库）
- 本模块只负责「装 + 配」，执行由 WorkBuddy 侧完成。
"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("lifeos.skillhub")

SKILLHUB_SKILLS_DIR = os.environ.get("SKILLHUB_SKILLS_DIR", "/app/skillhub_skills")
SKILLHUB_CLI_DIR = os.environ.get("SKILLHUB_CLI_DIR", "/app/skillhub_cli")
SKILLHUB_CLI = os.path.join(SKILLHUB_CLI_DIR, "skills_store_cli.py")
CONFIG_FILE = os.path.join(SKILLHUB_SKILLS_DIR, ".skill_config.json")


def mask_secret(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return "****" + s[-4:]


def _parse_skill_md(path: Path) -> dict:
    """解析 SKILL.md 的 YAML frontmatter，返回 {name,title,description}。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        import yaml
        data = yaml.safe_load(fm_text) or {}
    except Exception:
        return {}
    return {
        "name": data.get("name", ""),
        "title": data.get("title", data.get("name", "")),
        "description": data.get("description", ""),
    }


def load_config() -> dict:
    try:
        p = Path(CONFIG_FILE)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.warning("读 skill 配置失败: %s", e)
    return {}


def save_config(config: dict) -> None:
    p = Path(CONFIG_FILE)
    p.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _skill_slug_and_ns(skill_dir: Path):
    """从技能目录推断 (slug, namespace)。@ns/slug 两层，普通 skill 一层。"""
    slug = skill_dir.name
    ns = skill_dir.parent.name if skill_dir.parent.name.startswith("@") else ""
    return slug, ns


def list_skillhub_skills() -> list:
    """扫描技能目录，返回技能列表（含 namespace/slug/配置状态/脱敏 key）。"""
    root = Path(SKILLHUB_SKILLS_DIR)
    config = load_config()
    skills = []
    if not root.is_dir():
        return skills
    for md in sorted(root.rglob("SKILL.md")):
        skill_dir = md.parent
        slug, ns = _skill_slug_and_ns(skill_dir)
        meta = _parse_skill_md(md)
        cfg = config.get(slug, {})
        skills.append({
            "slug": slug,
            "namespace": ns,
            "name": meta.get("title") or meta.get("name") or slug,
            "description": meta.get("description", ""),
            "config": {k: mask_secret(v) for k, v in cfg.items()},
            "configured": bool(cfg),
            "config_key_names": list(cfg.keys()),
        })
    return skills


def get_skill_config(slug: str) -> dict:
    config = load_config()
    return config.get(slug, {})


def set_skill_config(slug: str, key_name: str, key_value: str) -> dict:
    """给某技能写一条 key 配置，返回更新后的该技能配置（脱敏）。"""
    key_name = (key_name or "").strip()
    key_value = (key_value or "").strip()
    if not key_name:
        raise ValueError("key 名不能为空")
    if not key_value:
        raise ValueError("key 值不能为空")
    config = load_config()
    entry = config.setdefault(slug, {})
    entry[key_name] = key_value
    save_config(config)
    return {k: mask_secret(v) for k, v in entry.items()}


def install_skill(namespace: str, slug: str) -> dict:
    """调用 skillhub CLI 安装技能到技能目录。返回 {ok, message, slug, namespace}。"""
    slug = (slug or "").strip()
    namespace = (namespace or "").strip()
    if not slug:
        return {"ok": False, "message": "技能名不能为空"}
    if not os.path.isfile(SKILLHUB_CLI):
        return {"ok": False, "message": "skillhub CLI 未挂载（容器缺 /app/skillhub_cli）"}

    # 幂等：同名 slug 已存在则跳过，避免重复安装报 Target exists
    root = Path(SKILLHUB_SKILLS_DIR)
    for md in root.rglob("SKILL.md"):
        s, _ = _skill_slug_and_ns(md.parent)
        if s == slug:
            return {"ok": True, "message": f"技能 {slug} 已安装，跳过", "slug": slug,
                    "namespace": namespace, "already": True}

    cmd = [sys.executable, SKILLHUB_CLI, "install", slug]
    if namespace:
        cmd += ["--namespace", namespace]
    cmd += ["--dir", SKILLHUB_SKILLS_DIR]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "安装超时（>180s）"}
    except Exception as e:
        return {"ok": False, "message": f"安装异常: {type(e).__name__}: {e}"}
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return {"ok": True, "message": f"技能 {slug} 安装完成", "slug": slug,
                "namespace": namespace, "output": out[-500:]}
    return {"ok": False, "message": f"安装失败（码 {proc.returncode}）: {out[-300:]}"}
