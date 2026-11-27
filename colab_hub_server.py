# -*- coding: utf-8 -*-
"""
Universal Colab AI Compute Hub · 全项目通用多模态算力中枢独立服务 (colab_hub_server.py · 默认端口 :8768)
定位：
- 与直播间 (:8765)、B站视频工坊 (:8769)、本地生成/修图软件、AI 视频工具完全解耦独立运行；
- 任何本地或远程项目只需将接口地址指向 http://127.0.0.1:8768 即可共享 Colab GPU 大显存能力：
  1. 图像接口：POST /v1/images/generations (文生图)、/v1/images/edits (图生图修图)、/v1/images/matting (透明抠图)、/v1/images/upscale (超分放大)
  2. 视频接口：POST /v1/videos/generations (文生视频 T2V / 图生视频 I2V)、GET /v1/videos/tasks/{id}、GET /v1/videos/tasks/{id}/content
  3. 语音接口：POST /v1/audio/speech (零样本克隆配音，云端离线时自动回退本地 Edge-TTS 兜底保障永不中断！)
  4. 账号与代理：内置 Windows 系统代理实时探测 (自动走 127.0.0.1:7897，过滤死端口防 ConnectTimeout) + Google OAuth 登录 + 多项目接入监控。
"""

import base64
import io
import json
import os
import secrets
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import edge_tts
import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, Header, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from colab_manager import ColabManager  # noqa: E402

STATIC_DIR = BASE_DIR / "static"
HUB_OUT_DIR = STATIC_DIR / "hub_outputs"
HUB_OUT_DIR.mkdir(parents=True, exist_ok=True)
HUB_APPS_CFG = BASE_DIR / "colab_hub_apps.json"


class HubEngineAdapter:
    """轻量级独立配置适配器，使 ColabManager 无需实例化直播 ArenaEngine 即可完全独立运行。"""

    def __init__(self) -> None:
        self.colab_url: str = os.getenv("COLAB_TTS_URL", os.getenv("COLAB_URL", "")).strip().rstrip("/")
        self.colab_token: str = os.getenv("COLAB_TTS_TOKEN", os.getenv("COLAB_ADMIN_TOKEN", "")).strip()
        self.colab_tts_model: str = "qwen3-tts-1.7b"
        self.use_colab_tts: bool = True
        self.local_genie_url: str = os.getenv("LOCAL_GENIE_URL", "http://127.0.0.1:8766")
        self.llm_base_url: str = os.getenv("LLM_BASE_URL", "https://bbying.me/v1")
        self.llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
        self._load_saved()

    def _load_saved(self) -> None:
        if HUB_APPS_CFG.exists():
            try:
                d = json.loads(HUB_APPS_CFG.read_text(encoding="utf-8"))
                if d.get("colab_url"):
                    self.colab_url = str(d["colab_url"]).rstrip("/")
                if d.get("colab_token"):
                    self.colab_token = str(d["colab_token"])
            except Exception:
                pass

    def snapshot(self) -> Dict[str, Any]:
        return {"colab_url": self.colab_url, "use_colab_tts": self.use_colab_tts}

    def update_config(self, _b: str, _k: str, _m: str, colab_url: str = "", colab_token: str = "", **_kw: Any) -> None:
        if colab_url:
            self.colab_url = colab_url.strip().rstrip("/")
        if colab_token:
            self.colab_token = colab_token.strip()
        save_hub_registry()


hub_adapter = HubEngineAdapter()


async def _noop_broadcast(_msg: Dict[str, Any]) -> None:
    return None


colab_mgr = ColabManager(engine=hub_adapter, broadcast_fn=_noop_broadcast)

# 多项目通用接入注册表（内置四大场景模板，支持任意新增外部项目）
CONNECTED_APPS: Dict[str, Dict[str, Any]] = {
    "local_image_studio": {
        "id": "local_image_studio",
        "name": "🎨 本地图片生成与精修项目 (PixForge / 外部图像客户端)",
        "category": "image_and_video",
        "endpoint_hint": "http://127.0.0.1:8768/v1/images/*",
        "calls": 0,
        "last_active": "-",
        "api_key": "",
    },
    "bili_video_studio": {
        "id": "bili_video_studio",
        "name": "📺 B站视频制作工坊 & AI 托管面板 (:8769)",
        "category": "video_and_tts",
        "endpoint_hint": "http://127.0.0.1:8768/v1/videos/* & /v1/audio/speech",
        "calls": 0,
        "last_active": "-",
        "api_key": "",
    },
    "sunny_live_stage": {
        "id": "sunny_live_stage",
        "name": "🌻 阳光小院 2D 活体直播间 (:8765)",
        "category": "tts_and_matting",
        "endpoint_hint": "http://127.0.0.1:8768/v1/audio/speech",
        "calls": 0,
        "last_active": "-",
        "api_key": "",
    },
}


def load_hub_registry() -> None:
    if HUB_APPS_CFG.exists():
        try:
            d = json.loads(HUB_APPS_CFG.read_text(encoding="utf-8"))
            if isinstance(d.get("apps"), dict):
                CONNECTED_APPS.update(d["apps"])
        except Exception:
            pass


def save_hub_registry() -> None:
    payload = {
        "colab_url": hub_adapter.colab_url,
        "colab_token": hub_adapter.colab_token,
        "apps": CONNECTED_APPS,
    }
    tmp = BASE_DIR / "colab_hub_apps.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HUB_APPS_CFG)


load_hub_registry()


def record_app_call(client_id: str) -> None:
    cid = (client_id or "external_app").strip()
    if cid not in CONNECTED_APPS:
        CONNECTED_APPS[cid] = {
            "id": cid,
            "name": f"🔌 外部接入项目 ({cid})",
            "category": "multimodal",
            "endpoint_hint": "http://127.0.0.1:8768/v1",
            "calls": 0,
            "last_active": "-",
            "api_key": "",
        }
    CONNECTED_APPS[cid]["calls"] = int(CONNECTED_APPS[cid].get("calls") or 0) + 1
    CONNECTED_APPS[cid]["last_active"] = time.strftime("%H:%M:%S")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await colab_mgr.start()
    yield
    await colab_mgr.stop()


app = FastAPI(title="Universal Colab Multi-Modal Compute Hub (:8768)", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _cloud_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {hub_adapter.colab_token}"} if hub_adapter.colab_token else {}


# ==================== 1. 通用算力中枢 Web 面板与系统管理接口 ====================
@app.get("/")
@app.get("/colab-hub")
@app.get("/colab-studio")
async def hub_index_page():
    return FileResponse(str(STATIC_DIR / "colab_hub.html"))


@app.get("/health")
@app.get("/v1/capabilities")
async def hub_health():
    # 直接读取看门狗异步维护的缓存状态，确保 /health 永远 1ms 极速响应不阻塞任何接入客户端！
    ch = colab_mgr.health_cache
    return {
        "ok": True,
        "service": "universal_colab_compute_hub",
        "hub_url": "http://127.0.0.1:8768",
        "cloud_connected": bool(ch.get("connected")),
        "colab_url": hub_adapter.colab_url,
        "cloud_health": ch,
        "detected_proxy": colab_mgr._detect_local_http_proxy() or "",
        "capabilities": [
            "images.generations (FLUX / SDXL)",
            "images.edits (Img2Img / Inpaint)",
            "images.matting (BiRefNet Transparent PNG)",
            "images.upscale (2x / 4x HD)",
            "videos.generations (Wan2.1 T2V / LTX I2V)",
            "audio.speech (Qwen3-TTS Zero-Shot + Local Fallback)",
        ],
        "connected_apps": list(CONNECTED_APPS.values()),
    }


@app.get("/api/hub/dashboard")
@app.get("/api/colab/dashboard")
async def hub_dashboard():
    colab_mgr._load_account()
    if colab_mgr.account.get("google_email"):
        colab_mgr.cu_info["email"] = colab_mgr.account["google_email"]
    await colab_mgr.check_local_cloner()
    st = colab_mgr.get_dashboard_state()
    st["colab_url"] = hub_adapter.colab_url
    st["connected_apps"] = list(CONNECTED_APPS.values())
    st["recent_outputs"] = _list_recent_outputs()
    st["voice_status"] = _get_local_voice_generation_status()
    return st


def _get_local_voice_generation_status() -> Dict[str, Any]:
    from personas import PERSONA_ORDER, PERSONAS
    refs_dir = STATIC_DIR / "voice_refs"
    bank_dir = STATIC_DIR / "voice_bank"
    items = []
    for pid in PERSONA_ORDER:
        p = PERSONAS[pid]
        has_ref = (refs_dir / f"{pid}_baby_ref.wav").exists()
        p_bank = bank_dir / pid
        bank_cnt = len(list(p_bank.glob("*.mp3")) + list(p_bank.glob("*.wav"))) if p_bank.exists() else 0
        items.append({
            "id": pid,
            "name": p.get("nickname", pid),
            "label": p.get("internal_label", pid),
            "prompt": p.get("voice_design_prompt", ""),
            "has_ref": has_ref,
            "ref_url": f"/static/voice_refs/{pid}_baby_ref.wav" if has_ref else "",
            "bank_count": bank_cnt,
        })
    return {"personas": items}


@app.post("/api/hub/voices/generate_local_pack")
async def hub_generate_local_voice_pack(payload: Dict[str, Any] = Body(default={})):
    """供开源用户免 GPU 一键自主生成全套 8 位角色基础声线与常用语语音库（默认不覆盖已有自定义干声，除非 overwrite=true）。"""
    from personas import PERSONA_ORDER, PERSONAS
    from tts_service import VOICE_BANK_PHRASES

    overwrite = bool(payload.get("overwrite", False))
    target_id = str(payload.get("character_id") or "").strip()
    pids = [target_id] if target_id in PERSONAS else list(PERSONA_ORDER)

    refs_dir = STATIC_DIR / "voice_refs"
    bank_dir = STATIC_DIR / "voice_bank"
    refs_dir.mkdir(parents=True, exist_ok=True)
    bank_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    for pid in pids:
        p = PERSONAS[pid]
        voice_name = p.get("voice", "zh-CN-XiaoxiaoNeural")
        bp = int(p.get("base_pitch", 10))
        br = int(p.get("base_rate", 0))
        pitch_s = f"{bp:+d}Hz"
        rate_s = f"{br:+d}%"

        p_bank = bank_dir / pid
        p_bank.mkdir(parents=True, exist_ok=True)
        phrases = VOICE_BANK_PHRASES.get(pid, {})
        for tag, txt in phrases.items():
            out_mp3 = p_bank / f"{tag}.mp3"
            if overwrite or not out_mp3.exists():
                comm = edge_tts.Communicate(txt, voice=voice_name, rate=rate_s, pitch=pitch_s)
                await comm.save(str(out_mp3))
                (p_bank / f"{tag}.txt").write_text(txt, encoding="utf-8")
                generated += 1

        ref_wav = refs_dir / f"{pid}_baby_ref.wav"
        if overwrite or not ref_wav.exists():
            import imageio_ffmpeg
            import subprocess
            enter_mp3 = p_bank / "enter.mp3"
            if enter_mp3.exists():
                subprocess.run(
                    [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(enter_mp3), "-ar", "24000", "-ac", "1", str(ref_wav)],
                    capture_output=True,
                )
    return {"ok": True, "generated_clips": generated, "voice_status": _get_local_voice_generation_status()}


@app.post("/api/hub/voices/design_cloud/{character_id}")
async def hub_design_cloud_voice(character_id: str, payload: Dict[str, Any] = Body(default={})):
    """供用户连接自己的 Colab GPU 后，使用 Qwen3-TTS-1.7B-VoiceDesign 自主捏制专属干声并拉回本地。"""
    if not hub_adapter.colab_url:
        return JSONResponse({"ok": False, "error": "请先连接你的 Colab GPU 隧道地址，或使用左侧『免显卡本地一键生成』"}, status_code=400)
    try:
        r = await colab_mgr._req(
            "POST",
            f"{hub_adapter.colab_url}/v1/voices/design/{character_id}",
            timeout=90.0,
            headers=_cloud_headers(),
            json={"instruct": str(payload.get("instruct") or ""), "ref_text": str(payload.get("ref_text") or "")},
        )
        d = r.json()
        if r.status_code == 200 and d.get("ok"):
            dl = await colab_mgr._req("GET", f"{hub_adapter.colab_url}/v1/voices/ref/{character_id}", timeout=30.0, headers=_cloud_headers())
            if dl.status_code == 200 and len(dl.content) > 1000:
                dst = STATIC_DIR / "voice_refs" / f"{character_id}_baby_ref.wav"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(dl.content)
        return JSONResponse(d, status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Colab 捏声请求失败: {exc}"}, status_code=502)


def _list_recent_outputs() -> List[Dict[str, Any]]:
    items = []
    for f in sorted(HUB_OUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:16]:
        if f.is_file():
            items.append({
                "filename": f.name,
                "url": f"/static/hub_outputs/{f.name}",
                "kind": "video" if f.suffix.lower() == ".mp4" else ("audio" if f.suffix.lower() in (".mp3", ".wav") else "image"),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "ts": time.strftime("%H:%M:%S", time.localtime(f.stat().st_mtime)),
            })
    return items


@app.post("/api/hub/tunnel")
async def hub_save_tunnel(payload: Dict[str, Any] = Body(...)):
    url = str(payload.get("colab_url") or "").strip().rstrip("/")
    tok = str(payload.get("colab_token") or "").strip()
    hub_adapter.colab_url = url
    if tok and not tok.startswith("***"):
        hub_adapter.colab_token = tok
    save_hub_registry()
    ch = await colab_mgr.probe_cloud_studio()
    return {"ok": True, "colab_url": hub_adapter.colab_url, "cloud_health": ch}


@app.post("/api/hub/apps/register")
async def hub_register_app(payload: Dict[str, Any] = Body(...)):
    app_id = str(payload.get("id") or f"app_{int(time.time())}").strip()
    name = str(payload.get("name") or app_id).strip()
    key = "sk-hub-" + secrets.token_urlsafe(16)
    CONNECTED_APPS[app_id] = {
        "id": app_id,
        "name": name,
        "category": str(payload.get("category") or "multimodal"),
        "endpoint_hint": "http://127.0.0.1:8768/v1",
        "calls": 0,
        "last_active": "刚刚注册",
        "api_key": key,
    }
    save_hub_registry()
    return {"ok": True, "app": CONNECTED_APPS[app_id]}


# ==================== 2. Google OAuth 与代理防超时测试 ====================
@app.post("/api/colab/account")
async def colab_save_account(payload: Dict[str, Any] = Body(...)):
    view = colab_mgr.update_account(payload)
    return {"ok": True, "account": view, "cu_info": colab_mgr.cu_info}


@app.post("/api/colab/test_proxy")
async def colab_test_proxy(payload: Dict[str, Any] = Body(default={})):
    return await colab_mgr.test_google_connectivity(override_proxy=str(payload.get("proxy_url") or "").strip())


@app.post("/api/colab/oauth/import_json")
async def colab_oauth_import_json(file: UploadFile = File(...)):
    res = colab_mgr.import_google_client_json(await file.read())
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.post("/api/colab/oauth/start")
async def colab_oauth_start(request: Request, payload: Dict[str, Any] = Body(default={})):
    # 若本机 8765 同时在线且 client_id 为固定回调 Web 类型，优先兼容已注册回调地址；桌面型 OAuth 则任意本地端口均直接放行
    req_base = str(payload.get("callback_base") or "").strip() or str(request.base_url).rstrip("/")
    res = colab_mgr.start_official_oauth_flow(base_url=req_base)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/colab/oauth/callback")
async def colab_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(
            f"<meta charset='utf-8'><body style='font-family:Microsoft YaHei,sans-serif;padding:40px;'><h2>⚠️ Google 授权提示 ({error})</h2>"
            "<p>请在 <a href='https://console.cloud.google.com/auth/audience' target='_blank'>Google Cloud Audience 页面</a> 点击 Publish App 或将邮箱加入测试名单后重试。</p></body>",
            status_code=400,
        )
    res = await colab_mgr.complete_official_oauth_callback(code=code, state=state)
    if res.get("ok"):
        return HTMLResponse(
            "<meta charset='utf-8'><body style='font-family:Microsoft YaHei,sans-serif;padding:48px;text-align:center;background:#f0fdf4;color:#14532d;'>"
            f"<h2>✅ Google 账号登录授权成功！</h2><p>已提取账号：<b>{res.get('email','')}</b></p>"
            "<script>if(window.opener){try{window.opener.postMessage({type:'colab_oauth_ok'},'*');}catch(e){}}setTimeout(()=>window.close(),1500);</script></body>"
        )
    return HTMLResponse(f"<meta charset='utf-8'><body style='padding:40px;text-align:center;'><h2>⚠️ 自动提取失败</h2><p>{res.get('error','')}</p></body>", status_code=400)


@app.post("/api/colab/refresh_cu")
async def colab_refresh_cu():
    return {"ok": True, "cu_info": await colab_mgr.fetch_google_cu_info()}


@app.post("/api/colab/recover")
async def colab_recover():
    return await colab_mgr.recover_cloud_via_admin()


@app.post("/v1/models/unload")
async def hub_unload_models(payload: Dict[str, Any] = Body(default={})):
    if not hub_adapter.colab_url:
        return JSONResponse({"ok": False, "error": "尚未连接 Colab GPU 隧道"}, status_code=400)
    r = await colab_mgr._req("POST", f"{hub_adapter.colab_url}/v1/models/unload", timeout=20.0, headers=_cloud_headers(), json=payload)
    return JSONResponse(r.json(), status_code=r.status_code)


# ==================== 3. 通用图像生成与编辑网关（供本地生图/修图软件直连） ====================
@app.post("/v1/images/generations")
async def hub_images_generations(payload: Dict[str, Any] = Body(...), x_app_id: Optional[str] = Header(default="local_image_studio")):
    record_app_call(x_app_id or "local_image_studio")
    if hub_adapter.colab_url:
        try:
            r = await colab_mgr._req(
                "POST", f"{hub_adapter.colab_url}/v1/images/generations", timeout=120.0, headers=_cloud_headers(), json=payload
            )
            if r.status_code == 200:
                d = r.json()
                b64 = ((d.get("data") or [{}])[0]).get("b64_json", "")
                if b64:
                    fname = f"img_{int(time.time())}.png"
                    (HUB_OUT_DIR / fname).write_bytes(base64.b64decode(b64))
                    d["data"][0]["url"] = f"http://127.0.0.1:8768/static/hub_outputs/{fname}"
                return d
            return JSONResponse({"ok": False, "error": f"Colab 返回 HTTP {r.status_code}: {r.text[:180]}"}, status_code=r.status_code)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"请求 Colab 生图失败: {exc}"}, status_code=502)

    return JSONResponse(
        {"ok": False, "need_colab_gpu": True, "error": "当前 Colab GPU 隧道未连接。请在 :8768 面板填入 Colab 隧道地址后再调用文生图大模型。"},
        status_code=503,
    )


@app.post("/v1/images/edits")
@app.post("/v1/images/img2img")
async def hub_images_edits(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    strength: float = Form(default=0.65),
    steps: int = Form(default=8),
    guidance_scale: float = Form(default=3.5),
    x_app_id: Optional[str] = Header(default="local_image_studio"),
):
    record_app_call(x_app_id or "local_image_studio")
    raw = await image.read()
    if not hub_adapter.colab_url:
        return JSONResponse({"ok": False, "need_colab_gpu": True, "error": "请先在 :8768 面板连接 Colab GPU 隧道"}, status_code=503)
    try:
        r = await colab_mgr._req(
            "POST",
            f"{hub_adapter.colab_url}/v1/images/edits",
            timeout=120.0,
            headers=_cloud_headers(),
            data={"prompt": prompt, "strength": str(strength), "steps": str(steps), "guidance_scale": str(guidance_scale)},
            files={"image": (image.filename or "input.png", raw, image.content_type or "image/png")},
        )
        d = r.json()
        b64 = ((d.get("data") or [{}])[0]).get("b64_json", "")
        if b64:
            fname = f"edit_{int(time.time())}.png"
            (HUB_OUT_DIR / fname).write_bytes(base64.b64decode(b64))
            d["data"][0]["url"] = f"http://127.0.0.1:8768/static/hub_outputs/{fname}"
        return JSONResponse(d, status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"请求图生图修图失败: {exc}"}, status_code=502)


@app.post("/v1/images/matting")
@app.post("/v1/matting/birefnet")
async def hub_images_matting(file: UploadFile = File(...), x_app_id: Optional[str] = Header(default="local_image_studio")):
    record_app_call(x_app_id or "local_image_studio")
    raw = await file.read()
    if hub_adapter.colab_url:
        try:
            r = await colab_mgr._req(
                "POST",
                f"{hub_adapter.colab_url}/v1/matting/birefnet",
                timeout=60.0,
                headers=_cloud_headers(),
                files={"file": (file.filename or "input.png", raw, file.content_type or "image/png")},
            )
            if r.status_code == 200 and len(r.content) > 500:
                fname = f"matting_{int(time.time())}.png"
                (HUB_OUT_DIR / fname).write_bytes(r.content)
                return Response(content=r.content, media_type="image/png", headers={"X-Output-Url": f"http://127.0.0.1:8768/static/hub_outputs/{fname}"})
        except Exception:
            pass
    return JSONResponse({"ok": False, "need_colab_gpu": True, "error": "BiRefNet GPU 抠图需先连接 Colab 隧道"}, status_code=503)


@app.post("/v1/images/upscale")
async def hub_images_upscale(
    file: UploadFile = File(...),
    scale: int = Form(default=2),
    x_app_id: Optional[str] = Header(default="local_image_studio"),
):
    """超分高清放大：若 Colab 在线走云端 GPU；若 Colab 离线自动使用本机 Lanczos + 自适应锐化即时完成，确保本地修图软件随时可用！"""
    record_app_call(x_app_id or "local_image_studio")
    raw = await file.read()
    if hub_adapter.colab_url and colab_mgr.health_cache.get("connected"):
        try:
            r = await colab_mgr._req(
                "POST",
                f"{hub_adapter.colab_url}/v1/images/upscale",
                timeout=60.0,
                headers=_cloud_headers(),
                data={"scale": str(scale)},
                files={"file": (file.filename or "in.png", raw, "image/png")},
            )
            if r.status_code == 200 and len(r.content) > 500:
                fname = f"upscale_{scale}x_{int(time.time())}.png"
                (HUB_OUT_DIR / fname).write_bytes(r.content)
                return Response(content=r.content, media_type="image/png", headers={"X-Engine": "colab-gpu", "X-Output-Url": f"http://127.0.0.1:8768/static/hub_outputs/{fname}"})
        except Exception:
            pass

    # 本机零依赖高清兜底放大
    sc = 4 if int(scale) >= 4 else 2
    img = Image.open(io.BytesIO(raw))
    up = img.resize((min(4096, img.width * sc), min(4096, img.height * sc)), Image.Resampling.LANCZOS)
    if up.mode != "RGBA":
        up = up.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.3, percent=120, threshold=3))
        up = ImageEnhance.Contrast(up).enhance(1.03)
    buf = io.BytesIO()
    up.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    fname = f"upscale_local_{sc}x_{int(time.time())}.png"
    (HUB_OUT_DIR / fname).write_bytes(png_bytes)
    return Response(content=png_bytes, media_type="image/png", headers={"X-Engine": "local-lanczos-sharp", "X-Output-Url": f"http://127.0.0.1:8768/static/hub_outputs/{fname}"})


# ==================== 4. 通用 AI 视频生成网关（支持 T2V 文生视频 & I2V 图生视频） ====================
@app.post("/v1/videos/generations")
async def hub_videos_generations(payload: Dict[str, Any] = Body(...), x_app_id: Optional[str] = Header(default="bili_video_studio")):
    record_app_call(x_app_id or "bili_video_studio")
    if not hub_adapter.colab_url:
        return JSONResponse(
            {"ok": False, "need_colab_gpu": True, "error": "AI 视频生成 (Wan2.1 / LTX-Video) 需要 GPU 算力，请先在 :8768 面板连接 Colab 隧道。"},
            status_code=503,
        )
    try:
        r = await colab_mgr._req("POST", f"{hub_adapter.colab_url}/v1/videos/generations", timeout=30.0, headers=_cloud_headers(), json=payload)
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"提交视频生成任务失败: {exc}"}, status_code=502)


@app.get("/v1/videos/tasks/{task_id}")
async def hub_videos_task_status(task_id: str):
    if not hub_adapter.colab_url:
        return JSONResponse({"ok": False, "error": "Colab 未连接"}, status_code=503)
    r = await colab_mgr._req("GET", f"{hub_adapter.colab_url}/v1/videos/tasks/{task_id}", timeout=15.0, headers=_cloud_headers())
    d = r.json()
    if d.get("status") == "succeeded":
        local_mp4 = HUB_OUT_DIR / f"{task_id}.mp4"
        if not local_mp4.exists():
            dl = await colab_mgr._req("GET", f"{hub_adapter.colab_url}/v1/videos/tasks/{task_id}/content", timeout=120.0, headers=_cloud_headers())
            if dl.status_code == 200 and len(dl.content) > 2000:
                local_mp4.write_bytes(dl.content)
        if local_mp4.exists():
            d["local_url"] = f"http://127.0.0.1:8768/static/hub_outputs/{local_mp4.name}"
    return JSONResponse(d, status_code=r.status_code)


@app.get("/v1/videos/tasks/{task_id}/content")
async def hub_videos_task_content(task_id: str):
    local_mp4 = HUB_OUT_DIR / f"{task_id}.mp4"
    if local_mp4.exists():
        return FileResponse(str(local_mp4), media_type="video/mp4", filename=local_mp4.name)
    if not hub_adapter.colab_url:
        return JSONResponse({"ok": False, "error": "视频未缓存且 Colab 未连接"}, status_code=404)
    dl = await colab_mgr._req("GET", f"{hub_adapter.colab_url}/v1/videos/tasks/{task_id}/content", timeout=120.0, headers=_cloud_headers())
    if dl.status_code == 200:
        local_mp4.write_bytes(dl.content)
        return FileResponse(str(local_mp4), media_type="video/mp4", filename=local_mp4.name)
    return JSONResponse({"ok": False, "error": f"HTTP {dl.status_code}"}, status_code=dl.status_code)


# ==================== 5. 通用语音克隆网关（云端 GPU 优先 + 本地自动兜底） ====================
@app.post("/v1/audio/speech")
async def hub_audio_speech(payload: Dict[str, Any] = Body(...), x_app_id: Optional[str] = Header(default="sunny_live_stage")):
    record_app_call(x_app_id or "sunny_live_stage")
    text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Empty text"}, status_code=400)

    if hub_adapter.colab_url and colab_mgr.health_cache.get("connected"):
        try:
            r = await colab_mgr._req("POST", f"{hub_adapter.colab_url}/v1/audio/speech", timeout=25.0, headers=_cloud_headers(), json=payload)
            if r.status_code == 200 and len(r.content) > 500:
                return Response(content=r.content, media_type=r.headers.get("content-type", "audio/mpeg"), headers={"X-TTS-Engine": "colab-qwen3-1.7b"})
        except Exception:
            pass

    # 云端未连时自动使用 Edge-TTS 兜底，保证任何接入项目的语音请求 100% 成功返回
    speed = max(0.65, min(1.65, float(payload.get("speed") or 1.0)))
    pct = int(round((speed - 1.0) * 100))
    tmp_mp3 = HUB_OUT_DIR / f"tts_{int(time.time()*1000)}.mp3"
    comm = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural", rate=f"{pct:+d}%", pitch="+10Hz")
    await comm.save(str(tmp_mp3))
    return FileResponse(str(tmp_mp3), media_type="audio/mpeg", headers={"X-TTS-Engine": "hub-edge-fallback"})


if __name__ == "__main__":
    import sys
    import uvicorn

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    port = int(os.getenv("COLAB_HUB_PORT", "8768"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
