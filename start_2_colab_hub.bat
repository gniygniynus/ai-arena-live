@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [2] 正在启动独立【Universal Colab 通用多模态算力中枢】 (http://127.0.0.1:8768) ...
echo     任何生图/修图项目、视频生成项目、直播间均可连接 http://127.0.0.1:8768/v1
start "" "http://127.0.0.1:8768"
".venv\Scripts\python.exe" -u colab_hub_server.py
