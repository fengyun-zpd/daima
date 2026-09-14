FROM python:3.12-slim

# 沙箱镜像：只包含确定性检查所需的工具，不包含任何业务代码或凭据。
# 运行期约束由 sandbox/docker.py 强制注入（network=none、非 root、只读根、
# tmpfs 的 /tmp 与 /work、cap_drop=ALL、no-new-privileges），不依赖镜像内的默认配置。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "ruff==0.6.9" "pytest>=8.3" "pytest-cov>=5.0" "coverage>=7.6"

# 非 root 运行（FR-057）：固定 uid/gid，避免与宿主机用户混淆。
RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin sandbox

WORKDIR /work
USER sandbox

CMD ["python", "--version"]
