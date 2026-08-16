"""Skill 插件契约测试：每个技能必须有 skill.yaml（用 safe_load 解析）+ handler.py。"""
import os

import yaml

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


def test_skills_have_valid_contract():
    for name in ("todo", "expense", "news", "health"):
        d = os.path.join(SKILLS_DIR, f"{name}_skill")
        assert os.path.isfile(os.path.join(d, "skill.yaml")), f"{name} 缺 skill.yaml"
        assert os.path.isfile(os.path.join(d, "handler.py")), f"{name} 缺 handler.py"
        with open(os.path.join(d, "skill.yaml"), encoding="utf-8") as f:
            meta = yaml.safe_load(f)  # 必须用 safe_load（无反序列化漏洞）
        assert meta.get("name")
        assert isinstance(meta.get("trigger_keywords"), list)
        assert len(meta["trigger_keywords"]) > 0
