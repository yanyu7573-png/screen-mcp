@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ════════════════════════════════════
echo   screen-mcp 安装脚本 - Windows
echo ════════════════════════════════════

set SCRIPT_DIR=%~dp0
set VENV=%SCRIPT_DIR%.venv

:: ── 1. 检查 Python ────────────────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 python
    echo        请从 https://python.org 安装 Python 3.10+
    echo        安装时勾选 "Add Python to PATH"
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [✓] %%v

:: ── 2. 创建虚拟环境 ───────────────────────────────────────────────────────────
if not exist "%VENV%" (
    echo [...] 创建虚拟环境...
    python -m venv "%VENV%"
)
echo [✓] 虚拟环境: %VENV%

:: ── 3. 安装 Python 依赖 ───────────────────────────────────────────────────────
echo [...] 安装依赖...
"%VENV%\Scripts\pip" install -q --upgrade pip
"%VENV%\Scripts\pip" install -q "mcp[cli]>=1.0.0" Pillow pywin32 psutil

:: OCR（可选）
where tesseract >nul 2>&1
if not errorlevel 1 (
    "%VENV%\Scripts\pip" install -q pytesseract
    echo [✓] Tesseract OCR 已启用
) else (
    echo [i] Tesseract 未安装（可选）
    echo     下载地址: https://github.com/UB-Mannheim/tesseract/wiki
    echo     安装后将自动支持 OCR
)

echo [✓] 依赖安装完成

:: ── 4. 生成配置 ───────────────────────────────────────────────────────────────
set PYTHON_PATH=%VENV%\Scripts\python.exe
set SERVER_PATH=%SCRIPT_DIR%server.py

echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   Claude Desktop 配置（复制以下内容）
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo {
echo   "mcpServers": {
echo     "screen-assistant": {
echo       "command": "%PYTHON_PATH:\=\\%",
echo       "args": ["%SERVER_PATH:\=\\%"]
echo     }
echo   }
echo }
echo.
echo 配置文件路径:
echo   %%APPDATA%%\Claude\claude_desktop_config.json
echo.

:: 自动写入
set CONFIG_PATH=%APPDATA%\Claude\claude_desktop_config.json

set /p CONFIRM="是否自动写入 Claude Desktop 配置？(y/N): "
if /i "%CONFIRM%"=="y" (
    if not exist "%APPDATA%\Claude" mkdir "%APPDATA%\Claude"
    if exist "%CONFIG_PATH%" (
        copy "%CONFIG_PATH%" "%CONFIG_PATH%.backup" >nul
        echo [✓] 原配置已备份
        "%VENV%\Scripts\python" -c "
import json, sys
config_path = r'%CONFIG_PATH%'
new_entry = {'screen-assistant': {'command': r'%PYTHON_PATH%', 'args': [r'%SERVER_PATH%']}}
try:
    with open(config_path) as f:
        config = json.load(f)
except:
    config = {}
config.setdefault('mcpServers', {}).update(new_entry)
with open(config_path, 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print('[✓] 配置已合并写入')
"
    ) else (
        "%VENV%\Scripts\python" -c "
import json
config = {'mcpServers': {'screen-assistant': {'command': r'%PYTHON_PATH%', 'args': [r'%SERVER_PATH%']}}}
import os; os.makedirs(r'%APPDATA%\Claude', exist_ok=True)
with open(r'%CONFIG_PATH%', 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print('[✓] 配置文件已创建')
"
    )
    echo 重启 Claude Desktop 即可生效
)

echo.
echo ════════════════════════════════════
echo   安装完成！
echo   测试: "%PYTHON_PATH%" "%SERVER_PATH%"
echo ════════════════════════════════════
pause
