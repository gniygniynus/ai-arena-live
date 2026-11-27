# 🌻 AI Arena Live · 阳光小院 2D 活体骨骼 AI 直播间 & 通用多模态算力中枢

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-green.svg)](./static/coopanion/LICENSE)
[![Original Backend: MIT](https://img.shields.io/badge/Original_Backend_Modules-MIT-blue.svg)](./OPEN_SOURCE_CREDITS.md)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Multi-Modal Hub](https://img.shields.io/badge/Colab_Hub-TTS%20%7C%20Image%20%7C%20Video-purple.svg)](./colab_hub_server.py)

一套**三大服务完全解耦独立、又可自由互联**的全栈开源系统：
1. **🌻 独立服务 ①：2D 58 部件活体骨骼 AI 互动直播系统 (`server.py` · `:8765`)** —— 8 位性格迥异的 2D 弹簧骨骼萌娘轮流值班、免抓包器直连弹幕与礼物、红蓝辩论 PK 实时计票、观众出题 3 人接力吐槽、角色独立长期记忆库、开播物理麦克风硬件静音防漏音。
2. **⚡ 独立服务 ②：Universal Colab AI Compute Hub 全项目通用多模态算力中枢 (`colab_hub_server.py` · `:8768`)** —— **面向任意外部项目开放的统一 GPU 网关**！无论你的**本地生图/修图项目**、**AI 视频项目**还是直播间，只需将接口指向 `http://127.0.0.1:8768/v1`，即可共享 Colab GPU 的**文生图 (FLUX/SDXL)**、**图生图修图**、**BiRefNet 发丝级透明抠图**、**高清超分放大**、**AI 文生视频/图生视频 (Wan2.1 / LTX-Video)** 与 **Qwen3-TTS 零样本声音克隆**！
3. **🎬 独立服务 ③：B站推广视频工坊（无级可调语速）& AI 智能托管面板 (`bili_studio_server.py` · `:8769`)** —— 支持 `0.70x ~ 1.65x` 无级调节讲解音速与实时倍速试听、原创无版权 DSP 编曲自动闪避混音、15 秒极速重出 1080P 成片、B站官方扫码自动分片投稿、AI 自动看视频写学习笔记与人工审批防风控评论草稿箱。

---

## 🚀 快速开始（三端完全独立，按需启动）

### 1. 安装依赖
```bash
git clone https://github.com/gniygniynus/ai-arena-live.git
cd ai-arena-live
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

> **🎙️ 关于角色声线生成（重要）**：本仓库**不包含任何预生成的角色声线音频文件**（`static/voice_refs/` 与 `static/voice_bank/` 默认为空）。启动 `:8768` 算力面板后，切到 **「🎙️ 2. 角色声线自主生成工坊」** 即可选择：**① 免显卡 15 秒一键生成本地基础声线库**，或 **② 连接你自己的 Colab GPU 用 `Qwen3-TTS-1.7B-VoiceDesign` 自主捏制专属音色**！

### 2. 独立启动任一面板（支持互连互通）

| 独立服务模块 | 启动命令 / 脚本 | 默认访问入口 | 说明 |
| :--- | :--- | :--- | :--- |
| **① 直播舞台与导播面板** | `python server.py` 或双击 `start_1_live.bat` | 导播：`http://127.0.0.1:8765/director`<br>舞台：`http://127.0.0.1:8765/stage` | 专职直播驱动、OBS 1080×1920 竖屏源与抖音弹幕直连 |
| **② 通用 Colab 多模态算力中枢** | `python colab_hub_server.py` 或双击 `start_2_colab_hub.bat` | `http://127.0.0.1:8768` | **任何生图/修图/生视频/TTS 项目通用接入**，内置系统代理防超时 |
| **③ B站视频工坊 & AI 托管面板** | `python bili_studio_server.py` 或双击 `start_3_bili_studio.bat` | `http://127.0.0.1:8769` | 0.70x~1.65x 可调倍速成片合成、B站扫码投稿与观影评论托管 |

> 也支持运行 `python start_all.py` 一键静默拉起后台引擎、物理闭麦小托盘与 OBS 虚拟摄像头环境。

---

## 🔌 将你的「本地生图/修图项目」或「视频项目」接入通用 Colab 面板 (`:8768`)

`colab_hub_server.py` (`:8768`) 与云端 `colab/cloud_studio_server.py` (v8) 内置了 **动态显存水位编排器 (`VRAMOrchestrator`)**，在 95GB 大显存卡上多模型常驻，在 16GB/24GB T4/L4 上自动按需切换模型防止 OOM，跨域（CORS）默认全放开：

- **文生图 (OpenAI Images 兼容)**：`POST http://127.0.0.1:8768/v1/images/generations` (`{ "prompt": "...", "size": "1024x1024" }`)
- **图生图 / 局部精修**：`POST http://127.0.0.1:8768/v1/images/edits` (`multipart/form-data`: `image`, `prompt`, `strength`)
- **BiRefNet 透明背景抠图**：`POST http://127.0.0.1:8768/v1/images/matting` (`multipart/form-data`: `file` -> 返回透明 PNG)
- **2x / 4x 超清画质放大（支持离线本地兜底）**：`POST http://127.0.0.1:8768/v1/images/upscale` (`multipart/form-data`: `file`, `scale=2`)
- **AI 文生视频 & 图生视频 (Wan2.1 / LTX-Video)**：
  - 提交任务：`POST http://127.0.0.1:8768/v1/videos/generations`（传入 `image_b64` 即为图生视频，让本地做好的图片动起来！）
  - 查询进度：`GET http://127.0.0.1:8768/v1/videos/tasks/{task_id}`
  - 获取 MP4：`GET http://127.0.0.1:8768/v1/videos/tasks/{task_id}/content`
- **零样本语音克隆 (OpenAI Speech 兼容)**：`POST http://127.0.0.1:8768/v1/audio/speech`

---

## 📜 原创与二创模块划分声明（重要 · 开源透明公示）

完整逐文件清单请参阅 **[OPEN_SOURCE_CREDITS.md](./OPEN_SOURCE_CREDITS.md)**，核心划分摘要如下：

| 类别 | 涵盖模块与文件 | 版权与来源说明 |
| :--- | :--- | :--- |
| **🟢 100% 原创自研** | • 互动直播导演状态机与长期记忆库 (`arena_engine.py`, `memory_store.py`, `safety.py`)<br>• **通用多模态 Colab 算力网关 (`:8768`)** 与动态显存编排 (`colab_hub_server.py`, `cloud_studio_server.py`, `colab_manager.py`, `tts_service.py`)<br>• 物理麦硬件防漏音守护与系统托盘 (`mic_guard.py`, `mic_tray.py`)<br>• **可调倍速宣传片合成引擎 (`:8769`)** 与纯数学合成无版权 BGM (`promo_studio_service.py`, `bili_studio_server.py`, `bgm_sunny_garden_musicbox.wav`) | 本项目从零完全原创编写与数学波形合成；除整仓遵循联合协议外，所有独立原创后端模块同时提供宽松 **MIT** 复用授权。 |
| **🟡 二创改编 & 灵感参考** | • **2D 58 部件弹簧骨骼与玩偶拆件** (`static/coopanion/`, `static/puppets/`, `static/stage.html`)<br>• **抖音弹幕直连协议层** (`douyin_proto/douyin.py`, `sign.js`, `douyin_direct.py`)<br>• **B站扫码投稿与 AI 观影托管** (`bili_agent_service.py`)<br>• **8 位角色同人设定** (`personas.py`) | • 底层 2D 弹簧骨骼库源自开源项目 **Coopanion** (`Pal-AI-Lab/cortico-world-desktop-pet`, `AGPL-3.0-or-later`)，基础拆件原设「溟月(上善无形)」/ 女仆装参考「ZipZipPipe」，本项目二创扩展了 8 角色配饰换装、昼夜多人舞台与实时口型联动（贴图仅供非商演示，商用请替换）；<br>• 抖音 Protobuf 定义源自 [`scx567888/live-room-watcher`](https://github.com/scx567888/live-room-watcher) 及公开签名适配，二创实现免抓包直连；<br>• B站托管致敬参考 [`bilibili_learning_bot`](https://github.com/xiaoyaya191/bilibili_learning_bot)、[`BiliCommentBot`](https://github.com/Janson20/BiliCommentBot)、[`biliup`](https://github.com/biliup/biliup) 理念，结合人工审批防风控护盾全新独立编写。 |
| **🔵 第三方模型与依赖** | `Qwen3-TTS`、`BiRefNet`、`FLUX.1-schnell`、`Wan2.1`、`LTX-Video`、`edge-tts`、`FastAPI` 等 | 遵循各自上游开源许可证（Apache-2.0 / MIT / OpenRAIL-M / LGPL 等）。 |

---

## 🛡️ 合规与防风控安全准则

1. **AI 生成标识合规**：直播舞台默认带有显式「AI生成互动 · 真人直播」合规提示标牌。
2. **国内平台网络隔离**：抖音弹幕直连与 B 站扫码/投稿/托管接口均在代码层硬编码 `trust_env=False` 强制直连国内网络，严禁走海外代理触发异地风控；Google OAuth 则自动探测本机系统代理精准分流。
3. **拒绝验证码绕过与垃圾刷屏**：绝不包含任何绕过人机 CAPTCHA 的逻辑；B 站外部评论默认进入**「人工审批草稿箱」**，叠加每日上限、随机冷却抖动与同 BV 永久去重。

## 📄 License

整仓联合分发遵循 **[AGPL-3.0-or-later](./static/coopanion/LICENSE)**（因舞台包含上游 `static/coopanion/` 骨骼库）；本项目全部独立原创后端与多模态网关模块（见 [OPEN_SOURCE_CREDITS.md](./OPEN_SOURCE_CREDITS.md)）同时授予 **[MIT License](./LICENSE)** 双重宽松许可。
