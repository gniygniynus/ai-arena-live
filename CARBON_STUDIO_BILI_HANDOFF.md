# Carbon 后端开发交接契约单 · 视频制作工坊 & B站 AI 托管总控台

> **前端已完成文件**：`d:\gemini\ai-arena-live\static\studio.html`（已挂载路由 `GET /studio`，并已在导播台 `/director` 与算力工坊 `/colab-studio` 顶部添加直达导航）。  
> **已预置只读引导接口**：`GET /api/studio/bootstrap`（位于 `server.py`，已返回 8 位角色声线与 `static/promo/` 成品视频列表；Carbon 后端实现完成后请将返回中的 `"backend_ready"` 置为 `true`）。  
> **参考成品脚本**：首支 1080P 龙宝配音推广视频 `static/promo/promo_longbao_v1.mp4` 与原创无版权 BGM `static/promo/bgm_sunny_garden_musicbox.wav` 已生成，可直接作为渲染流水线标准输出样板。

---

## 一、推荐技术栈选型（已调研 2025~2026 主流开源方案）

| 模块 | 推荐开源库 / 架构 | 说明 |
| :--- | :--- | :--- |
| **视频渲染合成** | `Playwright` + `Pillow` + `imageio-ffmpeg` (FFmpeg 7.1 已装) | 复用项目已有 58 部件骨骼舞台 `/stage?layout=landscape` 录屏 + 字幕叠印 + 人声闪避 (Ducking) BGM 混音 |
| **多声线 TTS** | 复用现有 `tts_service.py`（95GB Colab 1.7B 克隆 + `edge-tts` 参数预设） | 支持自定义上传 `.wav/.mp3` 参考干声推送到 Colab 锁定 |
| **B站登录 & 自动投稿** | **`biliup` (`stream_gears`)** + **`bilibili-api-python`** | `passport.bilibili.com/x/passport-login/web/qrcode/generate` 官方扫码登录 + `stream_gears.upload` 多线路分片上传（最防风控！） |
| **B站 AI 托管 Agent** | 参考 `xiaoyaya191/bilibili_learning_bot` + `Janson20/BiliCommentBot` + `Shanoa2/BiliInsight` | 用 `bilibili-api-python` 读取自己稿件新评论（自动楼中楼回复）+ 获取推荐/关键词视频字幕与元数据生成学习笔记与互动评论草稿 |
| **状态持久化** | SQLite (`studio_bili.db`) + 加密凭证 `bili_cookies.json` (`chmod 600`) | 持久化投稿记录、已互动 BV 去重表、待审批评论草稿箱、AI 观影笔记 |

> **安全与合规红线（务必强制执行）**：
> 1. **绝不绕过 B 站人机/滑块 CAPTCHA**：账号密码登录如遇极验验证，必须返回 `{"ok": false, "need_manual_captcha": true, "captcha_url": "..."}` 引导用户在可见页面手动验证；优先推荐二维码扫码登录。
> 2. **防刷屏/防封号硬限制**：默认开启「人工审批草稿箱 (`approval_queue`)」模式；强制执行每日看视频/发评论配额上限、90~260s 随机冷却抖动、深夜休眠、同 BV 永久去重，严禁硬广引流词。
> 3. **本地隔离**：所有请求 B 站接口必须 `trust_env=False` 强制直连国内网络，绝不走本机海外 Clash 代理触发异地风控！

---

## 二、模块 A：视频制作工坊与多声线管理 API 契约

### 1. `POST /api/studio/render/start` — 启动全流程视频渲染
- **Request JSON**：
  ```json
  {
    "title": "视频主标题",
    "resolution": "1920x1080",
    "fps": 30,
    "visual_mode": "live_stage_record | stage_and_director | custom_clips",
    "subtitle_style": "pastoral_cream | bili_yellow | cinema_clean",
    "voice_rate": 0.90,
    "outro_cta": true,
    "tts_engine": "auto | colab_clone | edge_preset",
    "primary_voice_id": "gpt",
    "bgm_url": "/static/promo/bgm_sunny_garden_musicbox.wav",
    "bgm_volume": 0.36,
    "bgm_ducking": true,
    "scenes": [
      { "id": "s1", "tag": "章节角标", "voice_id": "gpt", "mode": "opening_3p", "emotion": "happy", "text": "旁白文案" }
    ]
  }
  ```
- **Response JSON**：`{ "ok": true, "task_id": "render_20261127_001" }`

### 2. `GET /api/studio/render/status/{task_id}` — 查询渲染进度
- **Response JSON**：
  ```json
  {
    "ok": true,
    "status": "running | done | failed",
    "progress": 65,
    "stage_text": "正在录制第 4/6 段 58 部件活体骨骼画面...",
    "output_url": "/static/promo/promo_xxx.mp4",
    "cover_url": "/static/promo/promo_xxx.jpg"
  }
  ```

### 3. `POST /api/studio/script/generate` — 调用项目 LLM 一键生成推广脚本
- **Request JSON**：`{ "topic": "主题", "voice_id": "gpt", "scene_count": 6 }`
- **Response JSON**：`{ "ok": true, "scenes": [ { "id": "s1", "tag": "...", "voice_id": "gpt", "mode": "opening_3p", "emotion": "happy", "text": "..." } ] }`

### 4. 多声线导入 & BGM 上传接口
- `POST /api/studio/voices/upload` (`multipart/form-data`: `name`, `ref_text`, `file`) -> 保存到 `static/voice_refs/` 并自动写入 `refs_meta.json`，返回 `{ "ok": true, "voice_id": "..." }`
- `POST /api/studio/voices/sync_project` -> 刷新 8 位角色干声与 48 条语音库索引，返回 `{ "ok": true }`
- `POST /api/studio/voices/import_remote` (`{ "url": "...", "token": "..." }`) -> 探测远程 TTS 服务并同步到声线列表，返回 `{ "ok": true, "voices_added": 1 }`
- `POST /api/studio/voices/edge_preset` (`{ "name": "...", "edge_voice": "...", "pitch": "+11Hz", "rate": "-5%" }`) -> 持久化自定义 Edge-TTS 声线预设，返回 `{ "ok": true, "id": "..." }`
- `POST /api/studio/bgm/upload` (`multipart/form-data`: `file`) -> 保存至 `static/promo/`，返回 `{ "ok": true, "url": "/static/promo/xxx.mp3" }`
- `POST /api/studio/cover/extract` (`{ "video_filename": "promo_longbao_v1.mp4" }`) -> FFmpeg 抽帧并叠加爆款标题生成封面，返回 `{ "ok": true, "cover_url": "/static/promo/..." }`

---

## 三、模块 B：B站账号授权与自动投稿 API 契约

### 1. 账号授权（3 种登录方式 + 状态查询）
- `POST /api/bili/auth/qrcode` -> 调用 B 站 `qrcode/generate` 并用 `qrcode` 库生成 PNG Base64，返回：
  `{ "ok": true, "qrcode_key": "...", "qr_image_base64": "data:image/png;base64,..." }`
- `GET /api/bili/auth/qrcode/poll?qrcode_key=...` -> 轮询扫码状态，成功时落盘 `bili_cookies.json` 并返回：
  `{ "ok": true, "logged_in": true, "status_text": "扫码登录成功", "user": { "mid": 12345, "uname": "昵称", "level": 5 } }`
- `POST /api/bili/auth/password` (`{ "username": "...", "password": "..." }`) -> 成功返回同上；若触发图形验证码则返回 `{ "ok": false, "need_manual_captcha": true, "captcha_url": "..." }`
- `POST /api/bili/auth/cookie` (`{ "raw_cookie": "..." }`) -> 解析并调 `nav` 接口校验有效性后落盘，返回 `{ "ok": true, "user": { ... } }`
- `GET /api/bili/auth/status` -> 返回 `{ "ok": true, "logged_in": true/false, "user": { ... } }`
- `POST /api/bili/auth/logout` -> 清除本地 `bili_cookies.json`，返回 `{ "ok": true }`

### 2. AI 爆款元数据生成 & 一键投稿
- `POST /api/bili/meta/generate` (`{ "video_filename": "...", "script_summary": "..." }`) -> 返回 `{ "ok": true, "title": "...", "desc": "...", "tags": ["..."] }`
- `POST /api/bili/upload` -> 调用 `stream_gears.upload` / `biliup` 多线路上传：
  - **Request JSON**：`{ "video_filename": "promo_longbao_v1.mp4", "title": "...", "tid": 231, "copyright": 1, "tags": [...], "desc": "...", "dynamic": "...", "schedule_time": null, "upload_line": "auto" }`
  - **Response JSON**：`{ "ok": true, "bvid": "BV1xxxxxxxxx", "aid": 123456789 }`

---

## 四、模块 C：B站 AI 智能托管 Agent API 契约

- `POST /api/bili/agent/config` -> 持久化托管策略（字段与前端 `collectAgentConfig()` 一一对应：观看开关、收藏开关、关键词、观看时长区间、自己评论区楼中楼自动回复、外部评论模式 `approval_queue | auto_strict`、人设、每日上限、冷却间隔、活跃时段、风控熔断开关、BV 去重开关），返回 `{ "ok": true }`
- `POST /api/bili/agent/start` -> 启动后台 asyncio 托管巡航协程，返回 `{ "ok": true, "running": true }`
- `POST /api/bili/agent/stop` -> 立即优雅急停托管巡航协程，返回 `{ "ok": true, "running": false }`
- `GET /api/bili/agent/status` -> 返回：
  ```json
  {
    "ok": true,
    "running": false,
    "today_watched": 3,
    "today_commented": 1,
    "drafts": [
      { "id": "d_001", "bvid": "BV1xxx", "video_title": "...", "content": "AI 基于字幕生成的真诚评论..." }
    ],
    "logs": [
      { "ts": "13:45:10", "msg": "👀 已观看 [BV1xxx] 并生成 3 条学习摘要笔记" }
    ]
  }
  ```
- `POST /api/bili/agent/drafts/{draft_id}/approve` (`{ "content": "人工微调后的评论正文" }`) -> 发送真实评论并记录 BV 去重，返回 `{ "ok": true }`
- `POST /api/bili/agent/drafts/{draft_id}/reject` -> 丢弃草稿并标记该 BV 不再重复生成，返回 `{ "ok": true }`
