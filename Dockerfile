# ===== LifeOS V2.0 多阶段构建 =====
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 系统依赖（segno/lark 仅纯 Python，无需编译；保留 ca-certs）
# 国内构建走清华镜像源，直连 deb.debian.org 会卡死
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- 依赖层（先拷依赖再拷源码，命中缓存）----
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir -r /app/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# ---- 源码层 ----
COPY app /app/app
COPY skills /app/skills
# 前端构建产物（宿主先 npm run build 产出 frontend/dist 再构建镜像）
COPY frontend/dist /app/frontend/dist

# 降权运行
RUN useradd -m -u 1000 lifeos && mkdir -p /app/data /app/frontend/dist && chown -R lifeos:lifeos /app
USER lifeos

ENV FRONTEND_DIST=/app/frontend/dist
EXPOSE 8000
# PORT 可外部覆盖（默认 8000，compose 外映射 7208）；host 直跑用 --port 7208
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
