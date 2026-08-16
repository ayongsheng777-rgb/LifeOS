"""Skill 自动发现与热插拔（对齐《实现指南》第四节）。

扫描 skills_dir 下每个子目录的 skill.yaml，动态 import `<dir>.handler` 的 SkillHandler 类并实例化。
每个 skill 需提供：
- skill.yaml: {name, description, ...}
- handler.py: class SkillHandler: def __init__(self, metadata): ...; async def execute(self, message, context) -> str
"""
import os
import yaml
import importlib


class SkillRegistry:
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = skills_dir
        self.skills = {}

    def load_all_skills(self):
        base_path = os.path.abspath(self.skills_dir)
        if not os.path.exists(base_path):
            return
        for skill_folder in sorted(os.listdir(base_path)):
            folder_path = os.path.join(base_path, skill_folder)
            if os.path.isdir(folder_path):
                yaml_path = os.path.join(folder_path, "skill.yaml")
                if os.path.exists(yaml_path):
                    self._register_skill(folder_path, yaml_path)

    def _register_skill(self, folder_path, yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                metadata = yaml.safe_load(f)
        except Exception as e:
            print(f"[Skill Error] 读取 {yaml_path} 失败: {e}")
            return
        skill_name = metadata.get("name")
        if not skill_name:
            return
        folder = os.path.basename(folder_path)
        module_name = f"{self.skills_dir}.{folder}.handler"
        try:
            module = importlib.import_module(module_name)
            handler_instance = module.SkillHandler(metadata)
            self.skills[skill_name] = handler_instance
            print(f"[Skill] 已加载: {skill_name}")
        except Exception as e:
            print(f"[Skill Error] 加载 {skill_name} 失败: {e}")

    def get_available_skills(self):
        out = []
        for name, h in self.skills.items():
            meta = getattr(h, "metadata", {}) or {}
            out.append({
                "name": name,
                "desc": meta.get("description"),
                "trigger_keywords": meta.get("trigger_keywords", []),
            })
        return out

    def has_skill(self, name: str) -> bool:
        return name in self.skills

    def get_skill(self, name: str):
        return self.skills.get(name)
