# 一次性创建虚拟环境并安装 local 模式依赖。
# relay 模式不需要第三方包，但建议统一用 .venv 运行。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host "完成。用 .\.venv\Scripts\python.exe main.py status 检查。"
