@echo off
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM --- Jalankan Telegram CLIENT (userbot, tanpa Bot API) via Python 3.11 ---
set "PY=C:\Users\ISKAN-PC\AppData\Local\Python\pythoncore-3.11-64\python.exe"
if not exist %PY% set "PY=python"

%PY% telegram_client.py
pause
