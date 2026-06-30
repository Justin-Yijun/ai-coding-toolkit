<#
.SYNOPSIS
  ai_toolkit 一键环境搭建（Windows / PowerShell）。
.DESCRIPTION
  自动创建虚拟环境 .venv 并安装全部依赖。
  已处理「requirements.txt 含中文注释 → 简体中文系统 pip 以 gbk 解码报错」的坑
  （强制 PYTHONUTF8=1）。
.PARAMETER Proxy
  内网代理地址，例如 http://10.144.1.10:8080。外网环境留空即可。
.PARAMETER NoVenv
  跳过虚拟环境，直接装进当前 Python。
.EXAMPLE
  ./setup.ps1
.EXAMPLE
  ./setup.ps1 -Proxy http://10.144.1.10:8080
#>
param(
    [string]$Proxy = "",
    [switch]$NoVenv
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# 关键：requirements.txt 含 UTF-8 中文注释，简中系统默认 gbk 解码会报错，强制 UTF-8。
$env:PYTHONUTF8 = "1"

$python = "python"
if (-not $NoVenv) {
    if (-not (Test-Path ".venv")) {
        Write-Host "[1/3] 创建虚拟环境 .venv ..." -ForegroundColor Cyan
        & $python -m venv .venv
    }
    $python = Join-Path (Resolve-Path ".venv") "Scripts\python.exe"
}

$pipArgs = @("-m", "pip", "install", "--upgrade", "pip")
if ($Proxy) { $pipArgs += @("--proxy", $Proxy) }
Write-Host "[2/3] 升级 pip ..." -ForegroundColor Cyan
& $python @pipArgs

$pipArgs = @("-m", "pip", "install", "-r", "requirements.txt")
if ($Proxy) { $pipArgs += @("--proxy", $Proxy) }
Write-Host "[3/3] 安装依赖（requests / PyYAML / pytest / mypy）..." -ForegroundColor Cyan
& $python @pipArgs

Write-Host "`n[OK] 环境就绪。" -ForegroundColor Green
Write-Host "确保 Ollama 已运行后，试跑（无需联网，只需本地模型）：" -ForegroundColor Green
Write-Host "  $python main.py regex --desc '匹配邮箱' --pos a@b.com --neg not_email"
