FROM python:3.12-slim

# CodePilot 服务镜像：包含 API、Agent 服务、迁移与脚本。
# 提交代码**不会**在本镜像对应的容器中执行：所有不可信代码都在独立沙箱镜像里运行。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY apps ./apps
COPY domain ./domain
COPY agents ./agents
COPY a2a ./a2a
COPY tools ./tools
COPY rules ./rules
COPY sandbox ./sandbox
COPY repositories ./repositories
COPY evals ./evals
COPY migrations ./migrations
COPY scripts ./scripts
COPY schemas ./schemas
COPY examples ./examples
COPY conftest.py ./

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --no-build-isolation .

EXPOSE 8099
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8099"]
