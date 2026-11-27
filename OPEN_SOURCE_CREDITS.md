# 📜 原创与二创模块划分及开源合规声明 (Originality & Derivative Credits)

为保障开源社区透明性、严格遵守上游开源协议并帮助二次开发者清晰识别各模块的版权边界，现将本仓库（**AI Arena Live · 阳光小院 2D 活体 AI 直播系统 & 通用多模态算力中枢**）中 **100% 原创自研模块**、**二创/上游改编模块** 与 **第三方模型/基础库依赖** 完整公示如下：

---

## 一、🟢 100% 原创自研核心模块（Original Works）

以下全部后端引擎、跨项目网关、音频合成算法与控制台前端均由本项目从零独立原创编写（除整仓遵循联合协议 `AGPL-3.0-or-later` 外，作者同时授权以下独立原创文件可按宽松 `MIT` 协议单独抽取复用）：

1. **多智能体互动直播导演状态机与长期记忆库**
   - 文件：`arena_engine.py`、`memory_store.py`、`safety.py`、`server.py`
   - 原创内容：8 位 AI 角色队列轮流上下班机制、精力衰减/充能数学模型、红蓝辩论 PK 实时计票状态机、观众出题 3 人接力吐槽状态机、五子棋对弈与掀桌演出逻辑、SQLite 角色独立观众长期记忆库（老熟人进房自动识别迎宾）、直播违禁词实时脱敏护盾。
2. **Universal Colab AI Compute Hub 全项目通用多模态算力中枢 (`:8768`)**
   - 文件：`colab_hub_server.py`、`static/colab_hub.html`、`colab_manager.py`、`tts_service.py`、`local_voice_server.py`、`colab/cloud_studio_server.py`
   - 原创内容：
     - 面向任意外部项目（本地图片生成/精修项目、AI 视频工具、直播系统、B站工坊）开放的跨项目共享 GPU 算力网关；
     - 统一封装文生图 (`/v1/images/generations`)、图生图修图 (`/v1/images/edits`)、透明抠图 (`/v1/images/matting`)、高清超分 (`/v1/images/upscale`)、异步文生视频/图生视频 (`/v1/videos/generations`) 与零样本语音克隆 (`/v1/audio/speech`)；
     - 动态显存水位编排器 (`VRAMOrchestrator`)、四级零卡顿语音熔断路由、Windows 注册表实时系统代理探测（防 `ConnectTimeout`）与 Google OAuth2 PKCE 自动登录守护。
3. **本机物理麦克风硬件防漏音守护与系统托盘**
   - 文件：`mic_guard.py`、`mic_tray.py`、`start_all.py`
   - 原创内容：基于 Windows CoreAudio `IAudioEndpointVolume` 的开播非接管自动硬件闭麦、真人接管秒开麦、下播自动恢复及 OBS 硬件编码一键配平逻辑。
4. **可调倍速推广视频全流程引擎与 DSP 纯数学原创无版权 BGM (`:8769`)**
   - 文件：`promo_studio_service.py`、`bili_studio_server.py`、`static/studio.html`、`static/promo/bgm_sunny_garden_musicbox.wav`
   - 原创内容：
     - `bgm_sunny_garden_musicbox.wav` 由纯 Python/NumPy 数学正弦泛音与包络（DSP）从零合成，**100% 原创无任何外部音频采样，零版权风险**；
     - `0.70x ~ 1.65x` 保音调无级变速配音合成、$O(N)$ 前缀和自动人声闪避（Ducking）母带混音器、CDP 高清逐帧录屏封装流水线。

---

## 二、🟡 二创改编、协议适配与灵感致谢模块（Derivative Works & Upstream Credits）

以下模块基于上游开源代码、公开协议逆向成果或社区美术原设进行了**二次开发与定制改编**，使用时请务必保留对应上游版权声明：

### 1. 2D 58 部件弹簧骨骼内核与基础玩偶拆件（上游开源改编 + 形象二创）
- **涉及目录**：`static/coopanion/`、`static/puppets/`、`static/avatars_coop/`、`static/stage.html`（骨骼挂载部分）
- **上游来源与协议**：
  - 底层 Web 2D 弹簧骨骼驱动库（`static/coopanion/web/kit/rig.js`, `body.js`, `frame-loop.js`）源自开源项目 **Coopanion**（并入前独立仓库为 `Pal-AI-Lab/cortico-world-desktop-pet`），遵循 **`AGPL-3.0-or-later`** 协议（原始许可与第三方声明已完整保留于 [`static/coopanion/LICENSE`](./static/coopanion/LICENSE) 与 [`static/coopanion/THIRD_PARTY_NOTICES.md`](./static/coopanion/THIRD_PARTY_NOTICES.md)）；
  - 基础大头女仆玩偶拆件结构源自 Coopanion `web/whale/`（角色原设：**「溟月」/ 上善无形**；DeepSeek 女仆装二创参考：**ZipZipPipe**，由 AI 图像模型辅助生成拆件）；
  - **本项目的二创内容**：在此骨骼框架上扩展了多角色同台阳光庭院剧场（1~3 人动态排布、昼夜粒子光影、PK 投票条、流式语音口型同步）以及 8 位小院角色专属配色、头饰（皇冠/向日葵/星星/桂花等）与尾巴部件重制。
  - **商用注意**：依据上游 `THIRD_PARTY_NOTICES.md` 声明，`whale` 衍生贴图及各家 AI 标识配色仅作社区趣味展示，不在 AGPL 商业授权范围内；如需商业化运营，请自行替换为自有版权的角色贴图。

### 2. 抖音直播间直连协议层（开源逆向协议二创适配）
- **涉及文件**：`douyin_proto/douyin.py`、`douyin_proto/sign.js`、`douyin_direct.py`、`douyin_bridge.py`
- **上游来源与二创说明**：
  - `douyin_proto/douyin.py` 的 Webcast Protobuf 消息结构定义源自开源项目 **[scx567888/live-room-watcher](https://github.com/scx567888/live-room-watcher)** 并经 `python-betterproto` 生成（源码头部保留原出处注释）；`douyin_proto/sign.js` 为社区公开的 Web 端连接签名适配脚本；
  - 本项目在 `douyin_direct.py` / `douyin_bridge.py` 中二次开发了免第三方抓包软件直连、自动识别本机《抖音直播伴侣》开播房间号、国内直连强制防代理隔离与断线重连机制。仅限技术学习与连接本人直播间互动使用。

### 3. B 站扫码投稿与 AI 观影/评论托管模块（开源社区理念参考）
- **涉及文件**：`bili_agent_service.py`
- **灵感致谢与说明**：
  - 自动刷视频提取字幕记笔记、自己评论区自动楼中楼陪聊的设计理念参考致敬了开源项目 **[xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot)**、**[Janson20/BiliCommentBot](https://github.com/Janson20/BiliCommentBot)**、**[Shanoa2/BiliInsight](https://github.com/Shanoa2/BiliInsight)**，扫码与 UPOS 分片上传参考了 **[biliup/biliup](https://github.com/biliup/biliup)** / **[Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api)** 整理的公开接口规范；
  - 代码实现由本项目独立重写，并原创加入了「外部评论人工审批草稿箱 (`approval_queue`)」与「风控码自动熔断急停」安全护盾。

### 4. 8 位 AI 角色人设与声线生成说明
- **涉及文件**：`personas.py`
- **说明**：8 位角色人设是对主流大模型社区文化梗的趣味同人二创（与 OpenAI、Anthropic、DeepSeek、Google、智谱、xAI、月之暗面、阿里云官方无关）。**本开源仓库不预置、不分发任何作者自用的角色声线音频文件**（`static/voice_refs/` 与 `static/voice_bank/` 仅保留空目录占位符），使用者可在 `:8768` 面板自行一键合成基础声线或连接本人 Colab GPU 通过 `Qwen3-TTS-1.7B-VoiceDesign` 文本指令自主生成。

---

## 三、🔵 第三方开源大模型与软件库依赖（Third-Party Models & Libraries）

- **AI 大模型（按需加载）**：`Qwen/Qwen3-TTS-12Hz-1.7B` (Apache-2.0)、`ZhengPeng7/BiRefNet` (MIT)、`black-forest-labs/FLUX.1-schnell` (Apache-2.0)、`stabilityai/sdxl-turbo`、`Wan-AI/Wan2.1-T2V-1.3B-Diffusers` (Apache-2.0)、`Lightricks/LTX-Video` (OpenRAIL-M)、`edge-tts` (LGPL-3.0)
- **基础框架**：`FastAPI`、`Uvicorn`、`HTTPX`、`WebSockets`、`Pillow`、`NumPy`、`Playwright`、`imageio-ffmpeg`、`betterproto`、`py-mini-racer`、`qrcode`、`pystray`、`pycaw`
