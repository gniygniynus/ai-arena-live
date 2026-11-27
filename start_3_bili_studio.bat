@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [3] 正在启动独立【B站推广视频工坊(可调语速) ^& AI 智能托管面板】 (http://127.0.0.1:8769) ...
start "" "http://127.0.0.1:8769"
".venv\Scripts\python.exe" -u bili_studio_server.py
