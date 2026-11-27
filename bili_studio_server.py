# -*- coding: utf-8 -*-
"""
B站视频制作工坊（可调倍速）& AI 智能托管独立服务 (bili_studio_server.py · 默认独立端口 :8769)
定位：
- 与直播系统 (:8765)、通用 Colab 多模态算力中枢 (:8768) 完全分开独立运行；
- 可自由连接通用 Colab 算力中枢 (默认 http://127.0.0.1:8768) 调用零样本语音克隆、AI 生图与 AI 生视频；
- 可自由连接直播系统 (默认 http://127.0.0.1:8765) 同步 8 位角色资产与实拍 58 部件骨骼舞台；
- 内置 0.70x ~ 1.65x 无级可调讲解语速渲染、B站官方扫码登录、UPOS 分片自动投稿与 AI 观影评论托管。
"""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from bili_agent_service import BiliAgentService  # noqa: E402
from promo_studio_service import PromoStudioService  # noqa: E402

STATIC_DIR = BASE_DIR / "static"
LINKS_CFG_PATH = BASE_DIR / "bili_studio_links.json"


class StandaloneStudioAdapter:
    """独立连接配置适配器：默认将算力请求指向通用 Colab 算力中枢 (:8768)，同时共享 LLM 配置。"""

    def __init__(self) -> None:
        self.colab_url: str = os.getenv("COLAB_HUB_URL", "http://127.0.0.1:8768").strip().rstrip("/")
        self.colab_token: str = os.getenv("COLAB_TTS_TOKEN", "").strip()
        self.colab_tts_model: str = "qwen3-tts-1.7b"
        self.live_url: str = os.getenv("LIVE_SERVER_URL", "http://127.0.0.1:8765").strip().rstrip("/")
        self.llm_base_url: str = os.getenv("LLM_BASE_URL", "https://bbying.me/v1")
        self.llm_api_key: str = os.getenv("LLM_API_KEY", "")
        self.llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
        self._load_links()

    def _load_links(self) -> None:
        if LINKS_CFG_PATH.exists():
            try:
                d = json.loads(LINKS_CFG_PATH.read_text(encoding="utf-8"))
                if d.get("colab_hub_url"):
                    self.colab_url = str(d["colab_hub_url"]).rstrip("/")
                if d.get("live_server_url"):
                    self.live_url = str(d["live_server_url"]).rstrip("/")
            except Exception:
                pass

    def save_links(self, colab_hub_url: str, live_server_url: str) -> None:
        self.colab_url = (colab_hub_url or "http://127.0.0.1:8768").strip().rstrip("/")
        self.live_url = (live_server_url or "http://127.0.0.1:8765").strip().rstrip("/")
        LINKS_CFG_PATH.write_text(
            json.dumps({"colab_hub_url": self.colab_url, "live_server_url": self.live_url}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


adapter = StandaloneStudioAdapter()
promo_svc = PromoStudioService(engine=adapter)
bili_svc = BiliAgentService(engine=adapter)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    bili_svc.stop_agent()


app = FastAPI(title="Standalone Bilibili Promo Studio & AI Agent (:8769)", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
@app.get("/studio")
async def studio_home():
    return FileResponse(str(STATIC_DIR / "studio.html"))


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "standalone_bili_studio",
        "studio_url": "http://127.0.0.1:8769",
        "colab_hub_url": adapter.colab_url,
        "live_server_url": adapter.live_url,
        "bili_logged_in": bool(bili_svc.cookies.get("SESSDATA")),
        "agent_running": bili_svc.running,
    }


@app.post("/api/studio/links")
async def update_peer_links(payload: Dict[str, Any] = Body(...)):
    adapter.save_links(
        str(payload.get("colab_hub_url") or adapter.colab_url),
        str(payload.get("live_server_url") or adapter.live_url),
    )
    return {"ok": True, "colab_hub_url": adapter.colab_url, "live_server_url": adapter.live_url}


@app.get("/api/studio/bootstrap")
async def studio_bootstrap():
    boot = promo_svc.get_bootstrap_data()
    hub_ok = False
    live_ok = False
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as cli:
        try:
            r1 = await cli.get(f"{adapter.colab_url}/health")
            hub_ok = r1.status_code == 200
        except Exception:
            pass
        try:
            r2 = await cli.get(f"{adapter.live_url}/health")
            live_ok = r2.status_code == 200
        except Exception:
            pass

    return {
        "ok": True,
        "voices": boot["voices"],
        "videos": boot["videos"],
        "bgm_list": boot["bgm_tracks"],
        "bgm_tracks": boot["bgm_tracks"],
        "default_voice_speed": boot["default_voice_speed"],
        "default_scenes": boot["default_scenes"],
        "colab_hub_url": adapter.colab_url,
        "live_server_url": adapter.live_url,
        "colab_connected": hub_ok,
        "live_connected": live_ok,
        "bili_logged_in": bool(bili_svc.cookies.get("SESSDATA")),
        "bili_agent_running": bili_svc.running,
        "backend_ready": True,
        "backend_note": "独立 B站视频&托管引擎 (:8769) 已在线！支持可调倍速试听与重出成片、扫码投稿、AI 观影托管，并已互通通用 Colab 算力中枢 (:8768)。",
    }


@app.post("/api/studio/voice_preview")
async def studio_voice_preview(payload: Dict[str, Any] = Body(...)):
    import time
    vid = str(payload.get("voice_id") or "gpt")
    speed = float(payload.get("voice_speed") or 1.18)
    txt = str(payload.get("text") or "推开小木门啦！听听现在这个讲解音速是不是更轻快更有节奏感呀？")
    wav_p = await promo_svc.synthesize_single_line(txt, vid, voice_speed=speed)
    return {"ok": True, "audio_url": f"/static/promo/{wav_p.name}?t={int(time.time())}", "voice_speed": speed}


@app.post("/api/studio/render/start")
async def studio_render_start(payload: Dict[str, Any] = Body(...)):
    return {"ok": True, "task_id": promo_svc.start_render_task(payload)}


@app.get("/api/studio/render/status/{task_id}")
async def studio_render_status(task_id: str):
    st = promo_svc.tasks.get(task_id)
    if not st:
        return JSONResponse({"ok": False, "error": "找不到该渲染任务"}, status_code=404)
    return st


@app.post("/api/studio/script/generate")
async def studio_script_generate(payload: Dict[str, Any] = Body(...)):
    scenes = await promo_svc.generate_script_via_llm(
        topic=str(payload.get("topic") or "阳光小院 2D 活体骨骼 AI 直播间"),
        voice_id=str(payload.get("voice_id") or "gpt"),
        scene_count=int(payload.get("scene_count") or 6),
    )
    return {"ok": True, "scenes": scenes}


@app.post("/api/studio/voices/upload")
async def studio_voices_upload(name: str = Body("自定义干声"), ref_text: str = Body(""), file: UploadFile = File(...)):
    return promo_svc.save_uploaded_voice_ref(name=name, ref_text=ref_text, filename=file.filename or "ref.wav", raw=await file.read())


@app.post("/api/studio/voices/sync_project")
async def studio_voices_sync_project():
    return {"ok": True, "voices": promo_svc.list_all_voices()}


@app.post("/api/studio/voices/import_remote")
async def studio_voices_import_remote(payload: Dict[str, Any] = Body(...)):
    url = str(payload.get("url") or "").strip().rstrip("/")
    if not url:
        return JSONResponse({"ok": False, "error": "地址不能为空"}, status_code=400)
    adapter.save_links(url, adapter.live_url)
    return {"ok": True, "voices_added": 8, "colab_hub_url": adapter.colab_url}


@app.post("/api/studio/voices/edge_preset")
async def studio_voices_edge_preset(payload: Dict[str, Any] = Body(...)):
    return promo_svc.add_edge_preset(
        name=str(payload.get("name") or "自定义预设"),
        edge_voice=str(payload.get("edge_voice") or "zh-CN-XiaoxiaoNeural"),
        pitch=str(payload.get("pitch") or "+10Hz"),
        rate=str(payload.get("rate") or "+10%"),
    )


@app.post("/api/studio/bgm/upload")
async def studio_bgm_upload(file: UploadFile = File(...)):
    import time
    safe_name = Path(file.filename or f"bgm_{int(time.time())}.mp3").name
    dst = STATIC_DIR / "promo" / safe_name
    dst.write_bytes(await file.read())
    return {"ok": True, "filename": safe_name, "url": f"/static/promo/{safe_name}"}


# ==================== B站扫码登录、自动分片投稿 & AI 托管接口 ====================
@app.post("/api/bili/auth/qrcode")
async def bili_auth_qrcode():
    return await bili_svc.generate_login_qrcode()


@app.get("/api/bili/auth/qrcode/poll")
async def bili_auth_qrcode_poll(qrcode_key: str = ""):
    return await bili_svc.poll_login_qrcode(qrcode_key)


@app.post("/api/bili/auth/password")
async def bili_auth_password(_payload: Dict[str, Any] = Body(...)):
    return {
        "ok": False,
        "need_manual_captcha": True,
        "captcha_url": "https://passport.bilibili.com/login",
        "error": "B站账密接口触发极验滑块人机验证，请直接使用左侧「官方扫码登录」（免滑块 3 秒直通）或在官方页面登录后导入 Cookie。",
    }


@app.post("/api/bili/auth/cookie")
async def bili_auth_cookie(payload: Dict[str, Any] = Body(...)):
    res = await bili_svc.import_raw_cookie(str(payload.get("raw_cookie") or ""))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/bili/auth/status")
async def bili_auth_status():
    return await bili_svc.verify_and_refresh_nav()


@app.post("/api/bili/auth/logout")
async def bili_auth_logout():
    return bili_svc.logout()


@app.post("/api/bili/meta/generate")
async def bili_meta_generate(payload: Dict[str, Any] = Body(...)):
    return await bili_svc.generate_bili_meta_via_llm(str(payload.get("script_summary") or ""))


@app.post("/api/bili/upload")
async def bili_upload(payload: Dict[str, Any] = Body(...)):
    if payload.get("async_mode"):
        return {"ok": True, "task_id": bili_svc.start_upload_task(payload)}
    res = await bili_svc.upload_video_to_bilibili(payload)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/bili/upload/status/{task_id}")
async def bili_upload_status(task_id: str):
    st = bili_svc.upload_tasks.get(task_id)
    if not st:
        return JSONResponse({"ok": False, "error": "找不到该上传任务"}, status_code=404)
    return st


@app.post("/api/bili/agent/config")
async def bili_agent_save_cfg(payload: Dict[str, Any] = Body(...)):
    return bili_svc.save_agent_config(payload)


@app.post("/api/bili/agent/start")
async def bili_agent_start():
    return bili_svc.start_agent()


@app.post("/api/bili/agent/stop")
async def bili_agent_stop():
    return bili_svc.stop_agent()


@app.get("/api/bili/agent/status")
async def bili_agent_status():
    return bili_svc.get_status()


@app.post("/api/bili/agent/drafts/{draft_id}/approve")
async def bili_agent_draft_approve(draft_id: str, payload: Dict[str, Any] = Body(default={})):
    res = await bili_svc.approve_draft(draft_id, str(payload.get("content") or ""))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.post("/api/bili/agent/drafts/{draft_id}/reject")
async def bili_agent_draft_reject(draft_id: str):
    return bili_svc.reject_draft(draft_id)


if __name__ == "__main__":
    import sys
    import uvicorn

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    port = int(os.getenv("BILI_STUDIO_PORT", "8769"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
