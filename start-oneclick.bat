@echo off
chcp 936 >nul
setlocal enabledelayedexpansion

REM ---- 一键启动 Buddy2api（自包含，不依赖 Client2API / AIClient2API）----
REM ---- 静默执行：无 hidden 参数时，用 vbs 以无窗口方式重启自身 ----
if /i not "%~1"=="hidden" (
  if exist "%~dp0start-oneclick.vbs" (
    wscript.exe "%~dp0start-oneclick.vbs" "%~f0" hidden
    exit /b 0
  )
)

cd /d "%~dp0"

REM ---- 端口 / 主机 / 管理台鉴权（按需修改）----
set "BUDDY_PORT=8787"
set "BUDDY_HOST=0.0.0.0"
set "BUDDY_AUTH=--admin-token cb-admin-Zr491Q_rKVcTlbiyST2gaY_sADFn3gX9"

REM ---- Qoder 多账号：全局登录 + %USERPROFILE%\.qoder\snapshots\<name>\.auth 快照 ----
set "CB_QODER_AUTH_DIRS=%USERPROFILE%\.qoder\.auth"
for /d %%d in ("%USERPROFILE%\.qoder\snapshots\*") do (
  if exist "%%d\.auth" set "CB_QODER_AUTH_DIRS=!CB_QODER_AUTH_DIRS!;%%d\.auth"
)

set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ---- 已在运行则跳过，避免端口冲突 ----
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr /r ":%BUDDY_PORT% " ^| findstr /i "LISTENING"') do (
  echo [Buddy2api] 端口 %BUDDY_PORT% 已在运行，跳过启动。
  goto :eof
)

REM ---- Python 运行时：优先受管版本，缺失回退 PATH ----
set "PY=C:\Users\zhaoj\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

REM ---- 首次运行：创建虚拟环境并安装依赖 ----
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [Buddy2api] 首次运行：创建虚拟环境并安装依赖，详见 logs\buddy-install.log
  "%PY%" -m venv "%~dp0.venv" > "%LOG_DIR%\buddy-install.log" 2>&1
  "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" >> "%LOG_DIR%\buddy-install.log" 2>&1
)

REM ---- 启动前体检：修复孤儿外键 + 按需同步依赖 ----
if exist "%~dp0preflight-buddy.py" (
  "%PY%" "%~dp0preflight-buddy.py" > "%LOG_DIR%\buddy-preflight.log" 2>&1
)

echo [%date% %time%] Buddy2api 启动 >> "%LOG_DIR%\buddy.log"
echo [Buddy2api] 启动： http://%BUDDY_HOST%:%BUDDY_PORT%/  日志见 logs\buddy.log
"%~dp0.venv\Scripts\python.exe" server.py --port %BUDDY_PORT% --host %BUDDY_HOST% %BUDDY_AUTH% >> "%LOG_DIR%\buddy.log" 2>&1

if errorlevel 1 (
  echo.
  echo [Buddy2api] 启动失败，日志末尾如下（完整见 logs\buddy.log）：
  powershell -NoProfile -Command "Get-Content '%LOG_DIR%\buddy.log' -Tail 25"
)
