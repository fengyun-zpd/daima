#!/usr/bin/env pwsh
# CodePilot 本地开发启动脚本（Windows PowerShell）
#
#   ./scripts/dev.ps1                 # 安装依赖 + 迁移 + 构建沙箱镜像 + 启动 API
#   ./scripts/dev.ps1 -SkipSandbox    # 跳过沙箱镜像构建

param(
    [switch]$SkipSandbox,
    [string]$DatabaseUrl = $env:CODEPILOT_DATABASE_URL,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $DatabaseUrl) {
    $DatabaseUrl = "postgresql+psycopg://codepilot:codepilot@localhost:55432/codepilot"
}
$env:CODEPILOT_DATABASE_URL = $DatabaseUrl
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[1/4] 安装依赖" -ForegroundColor Cyan
python -m pip install -e ".[dev]"

Write-Host "[2/4] 执行数据库迁移（$DatabaseUrl）" -ForegroundColor Cyan
python -m alembic upgrade head

if (-not $SkipSandbox) {
    Write-Host "[3/4] 构建沙箱镜像" -ForegroundColor Cyan
    python scripts/build_sandbox_image.py
} else {
    Write-Host "[3/4] 跳过沙箱镜像构建（Fix/Verify 将返回 SANDBOX_UNAVAILABLE）" -ForegroundColor Yellow
}

Write-Host "[4/4] 启动 API：http://127.0.0.1:$Port" -ForegroundColor Cyan
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port $Port
