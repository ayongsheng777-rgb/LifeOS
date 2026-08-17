#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LifeOS 灾变备份脚本（本地磁盘）
==============================
备份对象（按实际情况自动探测）：
  - PostgreSQL  (容器 lifeos-postgres)  → pg_dump 自定义格式
  - Redis       (容器 lifeos-redis)     → redis-cli --rdb 快照
  - Qdrant      (容器 lifeos-qdrant)     → 仅当容器实际运行才备份，未启用则跳过
存储位置：BACKUP_ROOT（默认 E:\\Backups\\LifeOS，可用环境变量 LIFEOS_BACKUP_ROOT 覆盖）
保留策略：每个备份一个以时间戳命名的子目录，保留最近 RETENTION_DAYS(默认7) 天
日志：BACKUP_ROOT/backup.log
安全：每个组件独立 try/except，单点失败不影响其它；不阻塞；二进制用字节流处理
"""
import os
import sys
import json
import shutil
import subprocess
import datetime
import logging

# ====================== 可配置项 ======================
BACKUP_ROOT = os.environ.get("LIFEOS_BACKUP_ROOT", r"E:\Backups\LifeOS")
RETENTION_DAYS = int(os.environ.get("LIFEOS_BACKUP_KEEP", "7"))

PG_CONTAINER = "lifeos-postgres"
PG_USER = "lifeos"
PG_DB = "lifeos"
# 密码优先级：环境变量 > 默认值（与 compose/.env 保持一致）
PG_PASSWORD = os.environ.get("LIFEOS_DB_PW", "lifeos_pg_2026")

REDIS_CONTAINER = "lifeos-redis"
QDRANT_CONTAINER = "lifeos-qdrant"
# ====================================================

DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def docker_exec(container, args, extra_env=None, text=False):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run([DOCKER, "exec", container] + args,
                          capture_output=True, env=env, text=text)


def backup_postgres(run_dir, ts):
    """pg_dump 自定义格式，经 stdout 落盘。"""
    out = os.path.join(run_dir, f"lifeos-pg-{ts}.dump")
    r = docker_exec(PG_CONTAINER,
                    ["pg_dump", "-U", PG_USER, "-d", PG_DB, "-F", "c"],
                    extra_env={"PGPASSWORD": PG_PASSWORD})
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "pg_dump 非零退出")
    if not r.stdout:
        raise RuntimeError("pg_dump 输出为空")
    with open(out, "wb") as f:
        f.write(r.stdout)
    return os.path.getsize(out)


def backup_redis(run_dir, ts):
    """redis-cli --rdb - 把 RDB 快照经 stdout 输出（二进制）。"""
    out = os.path.join(run_dir, f"lifeos-redis-{ts}.rdb")
    r = docker_exec(REDIS_CONTAINER, ["redis-cli", "--rdb", "-"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "redis-cli 非零退出")
    if not r.stdout:
        raise RuntimeError("RDB 输出为空")
    with open(out, "wb") as f:
        f.write(r.stdout)
    return os.path.getsize(out)


def backup_qdrant(run_dir, ts):
    """仅当 lifeos-qdrant 容器实际运行才备份（tar 容器内 /qdrant/storage）。"""
    insp = subprocess.run([DOCKER, "inspect", "-f", "{{.State.Running}}", QDRANT_CONTAINER],
                          capture_output=True, text=True)
    if insp.stdout.strip() != "true":
        return None  # 明确跳过（当前 LifeOS 未启用 Qdrant）
    out = os.path.join(run_dir, f"lifeos-qdrant-{ts}.tar.gz")
    r = subprocess.run([DOCKER, "exec", QDRANT_CONTAINER, "tar", "czf", "-", "-C", "/qdrant/storage", "."],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "tar 非零退出")
    if not r.stdout:
        raise RuntimeError("Qdrant 快照为空")
    with open(out, "wb") as f:
        f.write(r.stdout)
    return os.path.getsize(out)


def prune_old(run_dir_parent, now):
    removed = 0
    for d in [os.path.join(run_dir_parent, x) for x in os.listdir(run_dir_parent)]:
        if not os.path.isdir(d):
            continue
        name = os.path.basename(d.rstrip("\\/"))
        try:
            bts = datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if (now - bts).days > RETENTION_DAYS:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def main():
    now = datetime.datetime.now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    run_dir = os.path.join(BACKUP_ROOT, ts)
    os.makedirs(run_dir, exist_ok=True)

    logging.basicConfig(
        filename=os.path.join(BACKUP_ROOT, "backup.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    logging.info("==== 开始 LifeOS 备份 %s ====", ts)
    results = {}

    # 1) PostgreSQL
    try:
        sz = backup_postgres(run_dir, ts)
        results["postgres"] = f"OK ({sz} bytes)"
        logging.info("PostgreSQL 备份成功: %d bytes", sz)
    except Exception as e:
        results["postgres"] = f"FAIL: {e}"
        logging.error("PostgreSQL 备份失败: %s", e)

    # 2) Redis
    try:
        sz = backup_redis(run_dir, ts)
        results["redis"] = f"OK ({sz} bytes)"
        logging.info("Redis 备份成功: %d bytes", sz)
    except Exception as e:
        results["redis"] = f"FAIL: {e}"
        logging.error("Redis 备份失败: %s", e)

    # 3) Qdrant（可选）
    try:
        sz = backup_qdrant(run_dir, ts)
        if sz is None:
            results["qdrant"] = "SKIP (lifeos-qdrant 未运行/未启用)"
            logging.warning("Qdrant 未启用，跳过")
        else:
            results["qdrant"] = f"OK ({sz} bytes)"
            logging.info("Qdrant 备份成功: %d bytes", sz)
    except Exception as e:
        results["qdrant"] = f"FAIL: {e}"
        logging.error("Qdrant 备份失败: %s", e)

    # 4) 清理旧备份
    try:
        removed = prune_old(BACKUP_ROOT, now)
        logging.info("清理旧备份 %d 个（保留 %d 天）", removed, RETENTION_DAYS)
    except Exception as e:
        logging.error("清理旧备份失败: %s", e)

    # 清单
    manifest = {"timestamp": ts, "backup_root": BACKUP_ROOT, "results": results}
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logging.info("备份完成: %s", json.dumps(results, ensure_ascii=False))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
