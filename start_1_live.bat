@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1] 正在启动独立【直播舞台与导播面板】 (http://127.0.0.1:8765/director) ...
start "" "http://127.0.0.1:8765/director"
".venv\Scripts\python.exe" -u server.py
