#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LifeOS 灾变备份（命令行入口 / 薄封装）
=====================================
统一调用 app.backup.run_backup_all，支持「本地磁盘 + NAS」双目标。

环境变量（与 .env 对齐；兼容旧脚本的 LIFEOS_BACKUP_ROOT / LIFEOS_BACKUP_KEEP）：
  LOCAL_BACKUP_ROOT   本地备份根目录（默认 F:\\LifeOS_BAK）
  NAS_BACKUP_ROOT     NAS 挂载目录（已挂载可写路径）；留空则不备份 NAS
  BACKUP_RETENTION_DAYS  保留天数（默认 7）
  DATA_DIR            配置目录 data/（默认 ./data）
  LIFEOS_BACKUP_ROOT  [兼容] 旧变量，等同 LOCAL_BACKUP_ROOT
  LIFEOS_BACKUP_KEEP  [兼容] 旧变量，等同 BACKUP_RETENTION_DAYS

用法：
  python scripts/lifeos_backup.py
"""
import os
import sys
import json

# 让脚本能 import app 包（脚本在 scripts/，项目根在上一级）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.backup import run_backup_all  # noqa: E402


def _resolve_local_root() -> str:
    return (os.environ.get("LOCAL_BACKUP_ROOT")
            or os.environ.get("LIFEOS_BACKUP_ROOT")
            or r"F:\LifeOS_BAK")


def _resolve_nas_root() -> str:
    return os.environ.get("NAS_BACKUP_ROOT", "") or ""


def _resolve_retention() -> int:
    try:
        return int(os.environ.get("BACKUP_RETENTION_DAYS")
                   or os.environ.get("LIFEOS_BACKUP_KEEP")
                   or "7")
    except ValueError:
        return 7


def main():
    local_root = _resolve_local_root()
    nas_root = _resolve_nas_root()
    retention = _resolve_retention()
    data_dir = os.environ.get("DATA_DIR", "./data")

    print(f"[backup] 本地={local_root}  NAS={nas_root or '无'}  保留={retention}天  data={data_dir}")
    summary = run_backup_all(local_root, nas_root, retention, data_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
