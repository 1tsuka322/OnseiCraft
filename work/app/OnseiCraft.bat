@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 解释器：优先环境变量 KIKOERU_PYTHON，否则用本机装了 PySide6 的那个 venv。
rem 换机器时只改下面这一行（或设 KIKOERU_PYTHON）。
set "PY=%KIKOERU_PYTHON%"
if not exist "%PY%" set "PY=C:\Users\zyk\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [OnseiCraft] 解释器：%PY%
"%PY%" "%~dp0main.py" %*
if errorlevel 1 (
  echo.
  echo 启动失败。上面是错误信息；按任意键关闭。
  pause >nul
)
