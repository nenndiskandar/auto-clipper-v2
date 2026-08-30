@echo off
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM --- Jalankan Telegram CLIENT (userbot, tanpa Bot API) ---
REM Cari Python: CLIPPER_PY env > py launcher > python di PATH
set "PY=%CLIPPER_PY%"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"

%PY% telegram_client.py
pause