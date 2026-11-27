# -*- coding: utf-8 -*-
"""
阳光田园超近景 2D 活体骨骼 AI 娘直播间 & Colab 算力工坊 Web 服务主入口 (server.py · v10 精炼加固版)
- 直播舞台 (1080x1920 竖屏，支持 ?layout=landscape & ?obs=1)：http://127.0.0.1:8765/stage
- 精简导播中控台：http://127.0.0.1:8765/director
- Colab 算力与官方 Google 登录面板：http://127.0.0.1:8765/colab-studio (或 /colab)
- 全局健康诊断接口：http://127.0.0.1:8765/health
"""

import asyncio
import json
import os
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

from arena_engine import ArenaEngine  # noqa: E402
from bili_agent_service import BiliAgentService  # noqa: E402
from colab_manager import ColabManager  # noqa: E402
from douyin_bridge import DouyinBridge  # noqa: E402
from personas import DEBATE_TOPICS, PERSONA_ORDER, PERSONAS, STOOLS, WORLD_PROPS  # noqa: E402
from promo_studio_service import PromoStudioService  # noqa: E402
from tts_service import (  # noqa: E402
    VOICE_BANK_DIR,
    VOICE_BANK_PHRASES,
    VOICE_BANK_TAGS,
    VOICE_REFS_DIR,
    VOICE_TAG_EMOTION_MAP,
    close_tts_clients,
    get_tts_runtime_status,
    get_voice_bank_counts,
    list_public_refs,
    synthesize_speech_detailed,
)

clients: Set[WebSocket] = set()


async def _safe_ws_send(ws: WebSocket, raw: str, dead: Set[WebSocket]) -> None:
    try:
        await asyncio.wait_for(ws.send_text(raw), timeout=2.0)
    except Exception:
        dead.add(ws)


async def broadcast_all(message: Dict[str, Any]) -> None:
    """并发带 2.0s 超时保护广播，杜绝慢连接阻塞主引擎事件循环"""
    if not clients:
        return
    dead: Set[WebSocket] = set()
    raw = json.dumps(message, ensure_ascii=False)
    await asyncio.gather(*(_safe_ws_send(ws, raw, dead) for ws in list(clients)), return_exceptions=True)
    for ws in dead:
        clients.discard(ws)


engine = ArenaEngine(broadcast_fn=broadcast_all)
dy_bridge = DouyinBridge(engine=engine, ws_url=os.getenv("DOUYIN_WS_URL", "ws://127.0.0.1:8888"))
colab_mgr = ColabManager(engine=engine, broadcast_fn=broadcast_all)
promo_svc = PromoStudioService(engine=engine)
bili_svc = BiliAgentService(engine=engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await engine.start()
    await colab_mgr.start()
    # 自动检测本机《抖音直播伴侣》是否正在开播，开播状态下自动挂载实时弹幕与礼物监听（0手动操作！）
    try:
        from douyin_direct import detect_companion_live_room
        comp = detect_companion_live_room()
        if comp and comp.get("room_id"):
            dy_bridge.start("auto")
    except Exception:
        pass
    yield
    bili_svc.stop_agent()
    await colab_mgr.stop()
    dy_bridge.stop()
    await engine.stop()
    await close_tts_clients()


app = FastAPI(title="AI Garden 2D Live & Colab Studio v10", lifespan=lifespan)
STATIC_DIR = BASE_DIR / "static"
SCENE_DIR = STATIC_DIR / "scene"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
SCENE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

LIVE_ACCESS_TOKEN = os.getenv("LIVE_ACCESS_TOKEN", "").strip()
_AUTH_COOKIE_NAME = "ai_live_token"


def _is_token_valid(candidate: str) -> bool:
    if not LIVE_ACCESS_TOKEN:
        return True
    import hmac
    return hmac.compare_digest((candidate or "").strip(), LIVE_ACCESS_TOKEN)


@app.middleware("http")
async def live_access_token_guard(request: Request, call_next):
    """公网部署口令守卫：未配置 LIVE_ACCESS_TOKEN 时本机完全透明放行；配置后支持 ?token= / Header / Cookie 免登！"""
    if not LIVE_ACCESS_TOKEN:
        return await call_next(request)

    path = request.url.path
    if path in ("/health", "/favicon.ico", "/api/colab/oauth/callback"):
        return await call_next(request)

    q_tok = request.query_params.get("token", "").strip()
    h_tok = request.headers.get("x-live-token", "").strip()
    c_tok = request.cookies.get(_AUTH_COOKIE_NAME, "").strip()

    if _is_token_valid(q_tok) or _is_token_valid(h_tok) or _is_token_valid(c_tok):
        resp = await call_next(request)
        if q_tok and _is_token_valid(q_tok):
            resp.set_cookie(
                key=_AUTH_COOKIE_NAME,
                value=LIVE_ACCESS_TOKEN,
                max_age=2592000,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return resp

    if path.startswith("/api/") or path.startswith("/static/"):
        return JSONResponse({"ok": False, "error": "访问口令无效，请先通过导播台登录或携带 token"}, status_code=401)

    login_html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>🔐 阳光小院 · 远程导播台口令验证</title>
    <style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#fffbeb,#ecfdf5);font-family:Microsoft YaHei,sans-serif;color:#1e293b;}
    .box{background:#fff;padding:28px 26px;border-radius:20px;box-shadow:0 16px 40px rgba(0,0,0,.1);width:min(92vw,380px);border:2px solid #f59e0b;}
    h2{margin:0 0 10px;color:#92400e;font-size:20px;}p{margin:0 0 16px;font-size:13px;color:#64748b;line-height:1.5;}
    input{width:100%;box-sizing:border-box;padding:11px 14px;border-radius:12px;border:2px solid #d6c3a5;font-size:15px;margin-bottom:12px;}
    button{width:100%;padding:11px;border:none;border-radius:12px;background:#16a34a;color:#fff;font-size:15px;font-weight:800;cursor:pointer;}
    </style></head><body><div class="box"><h2>🌻 阳光小院远程中控台</h2>
    <p>云端后端已开启口令保护，请输入你的访问口令（30 天免重复登录）：</p>
    <form method="GET"><input name="token" type="password" placeholder="请输入访问口令" autofocus required/>
    <button type="submit">🔓 进入导播台</button></form></div></body></html>"""
    return HTMLResponse(login_html, status_code=401)


# ==================== 页面路由与健康巡检 ====================
@app.get("/favicon.ico", include_in_schema=False)
async def favicon_no_content():
    return Response(status_code=204)


@app.get("/health")
async def global_health():
    return {
        "ok": True,
        "live_status": engine.live_status,
        "llm_calls": engine.llm_calls,
        "mode": engine.mode,
        "desk_prop_mode": engine.desk_prop_mode,
        "stage": list(engine.slots),
        "ws_clients": len(clients),
        "auth_enabled": bool(LIVE_ACCESS_TOKEN),
        "tts": get_tts_runtime_status(),
        "local_cloner": colab_mgr.local_cloner_health,
        "cloud_studio": {"connected": bool(colab_mgr.health_cache.get("connected")), "url": engine.colab_url if not LIVE_ACCESS_TOKEN else "configured"},
    }


@app.get("/")
async def root_page(code: str = "", state: str = "", error: str = ""):
    if code or error:
        return await colab_oauth_callback(code=code, state=state, error=error)
    return FileResponse(str(STATIC_DIR / "director.html"))


@app.get("/stage")
async def stage_page():
    return FileResponse(str(STATIC_DIR / "stage.html"))


@app.get("/director")
async def director_page():
    return FileResponse(str(STATIC_DIR / "director.html"))


@app.get("/colab-studio")
@app.get("/colab")
async def colab_studio_page():
    return FileResponse(str(STATIC_DIR / "colab_studio.html"))


@app.get("/studio")
async def promo_and_bili_studio_page():
    return FileResponse(str(STATIC_DIR / "studio.html"))


@app.get("/api/studio/bootstrap")
async def studio_bootstrap_meta():
    """返回完整视频制作工坊、多声线清单与 B 站托管引导状态（Carbon 完整后端已激活）。"""
    boot = promo_svc.get_bootstrap_data()
    return {
        "ok": True,
        "voices": boot["voices"],
        "videos": boot["videos"],
        "bgm_list": boot["bgm_tracks"],
        "bgm_tracks": boot["bgm_tracks"],
        "default_voice_speed": boot["default_voice_speed"],
        "default_scenes": boot["default_scenes"],
        "colab_connected": bool(colab_mgr.health_cache.get("connected")),
        "bili_logged_in": bool(bili_svc.cookies.get("SESSDATA")),
        "bili_agent_running": bili_svc.running,
        "backend_ready": True,
        "backend_note": "Carbon 全链路后端已就绪：支持 0.70x~1.65x 无级倍速配音渲染、B站扫码分片投稿与 AI 观影评论托管。",
    }


@app.post("/api/studio/voice_preview")
async def studio_voice_preview(payload: Dict[str, Any] = Body(...)):
    vid = str(payload.get("voice_id") or "gpt")
    speed = float(payload.get("voice_speed") or 1.18)
    txt = str(payload.get("text") or "推开阳光小院的小木门啦！听听现在这个讲解语速是不是干脆利落刚刚好呀？")
    wav_p = await promo_svc.synthesize_single_line(txt, vid, voice_speed=speed)
    return {
        "ok": True,
        "audio_url": f"/static/promo/{wav_p.name}?t={int(time.time())}",
        "local_path": str(wav_p),
        "voice_speed": speed,
    }


@app.post("/api/studio/render/start")
async def studio_render_start(payload: Dict[str, Any] = Body(...)):
    task_id = promo_svc.start_render_task(payload)
    return {"ok": True, "task_id": task_id}


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
async def studio_voices_upload(
    name: str = Body("自定义干声"),
    ref_text: str = Body(""),
    file: UploadFile = File(...),
):
    raw = await file.read()
    return promo_svc.save_uploaded_voice_ref(name=name, ref_text=ref_text, filename=file.filename or "ref.wav", raw=raw)


@app.post("/api/studio/voices/sync_project")
async def studio_voices_sync_project():
    return {"ok": True, "voices": promo_svc.list_all_voices()}


@app.post("/api/studio/voices/import_remote")
async def studio_voices_import_remote(payload: Dict[str, Any] = Body(...)):
    url = str(payload.get("url") or "").strip().rstrip("/")
    tok = str(payload.get("token") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "远程 TTS 地址不能为空"}, status_code=400)
    engine.update_config(
        engine.llm_base_url,
        "",
        engine.llm_model,
        colab_url=url,
        colab_token=tok,
        colab_tts_model="qwen3-tts-1.7b",
        use_colab_tts=True,
        save_env=True,
    )
    await colab_mgr.probe_cloud_studio()
    return {"ok": True, "voices_added": 8, "connected": colab_mgr.health_cache.get("connected", False)}


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
    safe_name = Path(file.filename or f"bgm_{int(time.time())}.mp3").name
    dst = STATIC_DIR / "promo" / safe_name
    dst.write_bytes(await file.read())
    return {"ok": True, "filename": safe_name, "url": f"/static/promo/{safe_name}"}


# ==================== Bilibili 授权、分片投稿 & AI 托管接口 ====================
@app.post("/api/bili/auth/qrcode")
async def bili_auth_qrcode():
    return await bili_svc.generate_login_qrcode()


@app.get("/api/bili/auth/qrcode/poll")
async def bili_auth_qrcode_poll(qrcode_key: str = ""):
    return await bili_svc.poll_login_qrcode(qrcode_key)


@app.post("/api/bili/auth/password")
async def bili_auth_password(_payload: Dict[str, Any] = Body(...)):
    # 遵循防风控与反人机绕过安全红线：B 站 Web 账密登录强制触发极验滑块，直接返回官方登录引导或推荐扫码
    return {
        "ok": False,
        "need_manual_captcha": True,
        "captcha_url": "https://passport.bilibili.com/login",
        "error": "B 站账密接口需人工完成官方极验滑块验证；强烈建议直接使用左侧「官方扫码登录」（免滑块 3 秒直通）或在浏览器登录后导入 Cookie。",
    }


@app.post("/api/bili/auth/cookie")
async def bili_auth_cookie(payload: Dict[str, Any] = Body(...)):
    raw = str(payload.get("raw_cookie") or "")
    res = await bili_svc.import_raw_cookie(raw)
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
async def bili_upload_sync(payload: Dict[str, Any] = Body(...)):
    if payload.get("async_mode"):
        tid = bili_svc.start_upload_task(payload)
        return {"ok": True, "task_id": tid}
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


@app.get("/api/colab/notebook")
async def download_colab_notebook():
    nb = BASE_DIR / "colab" / "ai_girls_voice_studio.ipynb"
    return FileResponse(str(nb), filename="ai_girls_voice_studio.ipynb", media_type="application/x-ipynb+json")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if LIVE_ACCESS_TOKEN:
        q_tok = ws.query_params.get("token", "").strip()
        h_tok = ws.headers.get("x-live-token", "").strip()
        c_tok = ws.cookies.get(_AUTH_COOKIE_NAME, "").strip()
        if not (_is_token_valid(q_tok) or _is_token_valid(h_tok) or _is_token_valid(c_tok)):
            await ws.close(code=1008, reason="Unauthorized")
            return
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_text(json.dumps(engine.snapshot(), ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "douyin_status", **dy_bridge.status()}, ensure_ascii=False))
        while True:
            raw = await ws.receive_text()
            # 支持本机物理闭麦小托盘通过 WS 回传真实硬件静音状态
            if raw and raw.startswith("{"):
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "tray_mic_report" and "muted" in msg:
                        new_muted = bool(msg["muted"])
                        if new_muted != engine.physical_mic_muted:
                            engine.physical_mic_muted = new_muted
                            await broadcast_all(engine.snapshot())
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        clients.discard(ws)


# ==================== 请求数据模型 ====================
class ConfigReq(BaseModel):
    base_url: str = "https://bbying.me/v1"
    api_key: str = ""
    model: str = "deepseek-v4.1-flash"
    colab_url: str = ""
    colab_token: str = ""
    colab_tts_model: str = "qwen3-tts-1.7b"
    use_colab_tts: bool = False
    local_genie_url: Optional[str] = None
    use_local_genie: Optional[bool] = None


class BakeSceneReq(BaseModel):
    time_of_day: str = "both"
    style: str = "ghibli_blue_archive"
    prompt_extra: str = ""


class ClearSceneReq(BaseModel):
    time_of_day: str = "both"


class BakeVoicesReq(BaseModel):
    personas: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    overwrite: bool = True


class PushPublicRefReq(BaseModel):
    filename: str = ""
    ref_text: str = ""


class DesignVoiceReq(BaseModel):
    instruct: str = ""
    ref_text: str = ""


class LiveStatusReq(BaseModel):
    status: str  # "live" | "paused" | "stopped"


class GameReq(BaseModel):
    game: str = "life"


class BarrageReq(BaseModel):
    user: str = "测试抖友"
    text: str


class GiftReq(BaseModel):
    target: str
    power: int = 30
    user: str = "田园小伙伴"
    gift_name: str = "小红花"


class SummonReq(BaseModel):
    incoming: str
    user: str = "田园访客"
    gift_name: str = "田园邀请函"


class RoastReq(BaseModel):
    attacker: str
    target: str
    user: str = "吃瓜朋友"


class TopicReq(BaseModel):
    topic: str


class DouyinReq(BaseModel):
    enable: bool
    ws_url: str = ""
    room_id: str = ""


class VoiceTestReq(BaseModel):
    persona_id: str = "deepseek"
    emotion: str = "happy"
    custom_text: str = ""
    voice_tag: str = ""


class ActivityReq(BaseModel):
    persona_id: str
    activity: str


# ==================== 导播核心控制接口 ====================
@app.get("/api/meta")
async def get_meta():
    return JSONResponse({
        "personas": PERSONAS,
        "topics": DEBATE_TOPICS,
        "stools": STOOLS,
        "world_props": WORLD_PROPS,
        "snapshot": engine.snapshot(),
        "douyin": dy_bridge.status(),
    })


@app.get("/api/state")
async def get_state_snapshot():
    return JSONResponse(engine.snapshot())


@app.get("/api/puppets/meta")
async def get_puppets_meta():
    rig_path = STATIC_DIR / "puppets" / "rig_meta.json"
    if not rig_path.exists():
        return JSONResponse({"error": "rig_meta.json not found", "puppets": {}}, status_code=404)
    try:
        return JSONResponse(json.loads(rig_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return JSONResponse({"error": f"Failed to read rig_meta.json: {exc}"}, status_code=500)


@app.post("/api/do_activity")
async def do_activity(req: ActivityReq):
    return engine.trigger_garden_activity(req.persona_id, req.activity)


@app.post("/api/config")
async def save_config(req: ConfigReq):
    engine.update_config(
        req.base_url,
        req.api_key,
        req.model,
        req.colab_url,
        req.colab_token,
        req.colab_tts_model,
        req.use_colab_tts,
        local_genie_url=req.local_genie_url,
        use_local_genie=req.use_local_genie,
        save_env=True,
    )
    await broadcast_all(engine.snapshot())
    return {
        "ok": True,
        "llm_ready": bool(engine.llm_api_key),
        "use_colab_tts": engine.use_colab_tts,
        "local_genie_url": engine.local_genie_url,
        "use_local_genie": engine.use_local_genie,
    }


@app.post("/api/live_status")
async def set_live_status(req: LiveStatusReq):
    await engine.set_live_status(req.status)
    return {"ok": True, "live_status": engine.live_status, "llm_calls": engine.llm_calls}


@app.post("/api/toggle_silence")
async def toggle_silence():
    engine.allow_silence = not engine.allow_silence
    await broadcast_all(engine.snapshot())
    return {"ok": True, "allow_silence": engine.allow_silence}


@app.post("/api/toggle_random")
async def toggle_random():
    engine.random_activities = not engine.random_activities
    await broadcast_all(engine.snapshot())
    return {"ok": True, "random_activities": engine.random_activities}


@app.post("/api/toggle_fixed_seating")
async def toggle_fixed_seating():
    engine.fixed_seating = not engine.fixed_seating
    await broadcast_all(engine.snapshot())
    return {"ok": True, "fixed_seating": engine.fixed_seating}


@app.post("/api/start_game")
async def start_game(req: GameReq):
    if req.game == "solo":
        await engine.set_stage_count(1)
    else:
        await engine.return_to_life_mode()
    return {"ok": True, "mode": engine.mode, "slots": list(engine.slots)}


@app.post("/api/desk_prop")
async def post_desk_prop(payload: Dict[str, Any] = Body(...)):
    prop = str(payload.get("mode") or "none").strip()
    if prop not in ("none", "tea", "mic"):
        prop = "none"
    engine.desk_prop_mode = prop
    await broadcast_all(engine.snapshot())
    return {"ok": True, "desk_prop_mode": engine.desk_prop_mode}


@app.post("/api/guide_card")
async def post_guide_card(payload: Dict[str, Any] = Body(...)):
    """控制直播画面上的合规互动提示牌开关与自定义文案（自动经过合规风控过滤！）"""
    from safety import sanitize_barrage

    if "show" in payload:
        engine.show_guide_card = bool(payload["show"])
    if isinstance(payload.get("title"), str) and payload["title"].strip():
        ok, clean_t = sanitize_barrage(payload["title"].strip()[:24])
        if ok:
            engine.guide_title = clean_t
    if isinstance(payload.get("items"), list):
        cleaned: List[str] = []
        for raw_item in payload["items"][:5]:
            s = str(raw_item or "").strip()
            if not s:
                continue
            ok, c = sanitize_barrage(s[:40])
            if ok and c:
                cleaned.append(c)
        if cleaned:
            engine.guide_items = cleaned
    await broadcast_all(engine.snapshot())
    return {"ok": True, "show_guide_card": engine.show_guide_card, "guide_title": engine.guide_title, "guide_items": engine.guide_items}


@app.post("/api/manual_speak")
async def post_manual_speak(payload: Dict[str, Any] = Body(...)):
    """导播紧急插麦/救场口播（优先级 0 最高优，0 LLM 消耗，应对真人抽检与实时控场！）"""
    pid = str(payload.get("persona_id") or "")
    text = str(payload.get("text") or "").strip()
    emo = str(payload.get("emotion") or "happy").strip()
    return await engine.manual_speak(pid, text, emo)


@app.post("/api/mic_takeover")
async def post_mic_takeover(payload: Dict[str, Any] = Body(default={})):
    """真人开麦接管控制：激活时自动解除物理麦静音+暂停AI让麦，关闭后若直播中自动硬件闭麦"""
    active = bool(payload.get("active", False))
    return await engine.set_human_mic_takeover(active)


@app.post("/api/auto_mute_mic")
async def post_auto_mute_mic(payload: Dict[str, Any] = Body(default={})):
    """开关「开播非接管自动硬件静音物理麦克风」"""
    enabled = bool(payload.get("enabled", True))
    return await engine.set_auto_mute_mic(enabled)


@app.get("/api/mic_status")
async def get_mic_status():
    """查询当前 Windows 物理麦克风真实硬件静音状态"""
    from mic_guard import async_get_mic_mute
    st = await async_get_mic_mute()
    if st.get("ok"):
        engine.physical_mic_muted = bool(st.get("muted", False))
    return {
        "ok": True,
        "auto_mute_mic": engine.auto_mute_mic,
        "human_mic_active": engine.human_mic_active,
        "physical_mic_muted": engine.physical_mic_muted,
        "hardware": st,
    }


@app.post("/api/pk/start")
async def post_pk_start(payload: Dict[str, Any] = Body(default={})):
    """开启 2 人红蓝 PK 辩论实时投票（固定 2 位 AI、冻结精力消耗、仅统计 1/2 弹幕投票）"""
    topic = str(payload.get("topic") or "").strip()
    red_stance = str(payload.get("red_stance") or "").strip()
    blue_stance = str(payload.get("blue_stance") or "").strip()
    dur = int(payload.get("duration_sec") or 60)
    return await engine.start_pk(topic=topic, red_stance=red_stance, blue_stance=blue_stance, duration_sec=dur)


@app.post("/api/pk/stop")
async def post_pk_stop():
    """结束红蓝 PK 辩论，恢复常规互动与精力计时"""
    return await engine.stop_pk()


@app.post("/api/audience_topic")
async def post_audience_topic(payload: Dict[str, Any] = Body(...)):
    """触发观众出题：顶部挂横幅 -> 凑齐 3 位 AI 从左到右依次吐槽 -> 第 3 位 AI 自动退场"""
    user = str(payload.get("user") or "热心观众").strip()
    topic = str(payload.get("topic") or "").strip()
    force = bool(payload.get("force", True))
    res = await engine.trigger_audience_topic(user=user, topic=topic, force=force)
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return res


@app.post("/api/audience_enter")
async def post_audience_enter(payload: Dict[str, Any] = Body(...)):
    """观众进入直播间接口（供联调与抓包桥接：仅场上有认识他的 AI 才从左到右主动打招呼）"""
    user = str(payload.get("user") or "").strip()
    return await engine.handle_audience_enter(user)


@app.get("/api/memory")
async def get_character_memory():
    """查看全部 8 位角色的独立持久化记忆库摘要"""
    return {"ok": True, "memory_summary": engine.memory_store.get_summary(PERSONA_ORDER)}


@app.post("/api/memory/delete_user")
async def post_delete_user_memory(payload: Dict[str, Any] = Body(...)):
    user = str(payload.get("user") or "").strip()
    removed = engine.memory_store.delete_user_memory(user)
    await broadcast_all(engine.snapshot())
    return {"ok": True, "removed": removed}


@app.post("/api/set_stage_count")
async def post_set_stage_count(payload: Dict[str, Any] = Body(...)):
    cnt = int(payload.get("count", 3))
    lead = str(payload.get("lead_persona") or "").strip()
    roster = payload.get("roster") if isinstance(payload.get("roster"), list) else None
    return await engine.set_stage_count(cnt, lead_persona=lead or None, roster=roster)


@app.post("/api/drain_stamina")
async def post_drain_stamina(payload: Dict[str, Any] = Body(default={})):
    pid = str(payload.get("persona_id") or (engine.slots[0] if engine.slots else ""))
    if pid not in engine.states:
        return {"ok": False, "reason": "角色不在场"}
    engine.states[pid]["stamina"] = 4.0
    await broadcast_all(engine.snapshot())
    return {"ok": True, "persona_id": pid, "stamina": 4.0}


@app.post("/api/topic")
async def set_topic(req: TopicReq):
    if req.topic.strip():
        engine.current_topic = req.topic.strip()[:80]
        await broadcast_all(engine.snapshot())
    return {"ok": True}


@app.post("/api/barrage")
async def post_barrage(req: BarrageReq):
    return await engine.handle_audience_barrage(req.user, req.text, source="文字弹幕")


@app.post("/api/gift")
async def post_gift(req: GiftReq):
    return await engine.boost_character(req.target, req.power, user=req.user, gift_name=req.gift_name, is_real_gift=True)


@app.post("/api/summon")
async def post_summon(req: SummonReq):
    if engine.live_status != "live":
        await engine.set_live_status("live")
    await engine.enqueue_event({"kind": "swap_character", "incoming": req.incoming, "reason": "invite", "user": req.user})
    return {"ok": True}


@app.post("/api/roast")
async def post_roast(req: RoastReq):
    if engine.live_status != "live":
        await engine.set_live_status("live")
    await engine.enqueue_event({"kind": "targeted_roast", "attacker": req.attacker, "target": req.target, "user": req.user})
    return {"ok": True}


@app.post("/api/flip_table")
async def post_flip_table():
    """兼容旧按钮调用：桌游已移除，温和转为双人俏皮斗嘴互动"""
    if len(engine.slots) >= 2 and engine.live_status == "live":
        await engine.enqueue_event({"kind": "targeted_roast", "attacker": engine.slots[0], "target": engine.slots[-1], "user": "导播互动"})
    return {"ok": True, "mode": engine.mode}


@app.post("/api/voice_test")
async def voice_test(req: VoiceTestReq):
    p = PERSONAS.get(req.persona_id, PERSONAS["deepseek"])
    sample = req.custom_text.strip() or "大家好呀，听听看我现在的声音是不是清脆又软萌呀~"
    ext_url = f"{engine.colab_url}/v1/audio/speech" if (engine.use_colab_tts and engine.colab_url) else ""
    url, dur, engine_used = await synthesize_speech_detailed(
        text=sample,
        voice=p["voice"],
        base_rate=p["base_rate"],
        base_pitch=p["base_pitch"],
        emotion=req.emotion,
        speaker_id=p["id"],
        external_url=ext_url,
        external_key=engine.colab_token,
        external_model=engine.colab_tts_model,
        local_genie_url=engine.local_genie_url,
        use_local_genie=engine.use_local_genie,
        voice_tag=req.voice_tag,
    )
    return {
        "ok": bool(url),
        "audio_url": url,
        "duration": dur,
        "playback_rate": round(engine.voice_playback_rate, 2),
        "text": sample,
        "engine": engine_used,
    }


@app.post("/api/playback_rate")
async def post_playback_rate(payload: Dict[str, Any] = Body(...)):
    """实时调节全局 AI 说话语速倍率 (0.72x ~ 1.18x)，导播台与 OBS 舞台即时生效！"""
    try:
        rate = float(payload.get("rate", 0.88))
    except (TypeError, ValueError):
        rate = 0.88
    engine.voice_playback_rate = round(max(0.72, min(1.18, rate)), 2)
    await broadcast_all(engine.snapshot())
    return {"ok": True, "voice_playback_rate": engine.voice_playback_rate}


@app.post("/api/demo_speak")
async def post_demo_speak(req: VoiceTestReq):
    """0 LLM 消耗快捷流式字音同步演练"""
    pid = req.persona_id if req.persona_id in engine.slots else (engine.slots[0] if engine.slots else "deepseek")
    custom = (req.custom_text or "").strip()
    vtag = (req.voice_tag or "").strip() or ("" if custom else "greeting")
    sample = custom or "大家好呀，快看看我在阳光露台上说话的样子，字音是不是同步得超丝滑呀！"
    others = [o for o in engine.slots if o != pid]
    rx_map = {o: ("happy" if i % 2 == 0 else "wink") for i, o in enumerate(others)}
    look_t = others[0] if others else ""

    async def _run_demo() -> None:
        async with engine.speaking_lock:
            await engine._speak(
                pid,
                sample,
                emotion=req.emotion or "happy",
                reactions=rx_map,
                look_at=look_t,
                pose="talking",
                voice_tag=vtag,
            )

    asyncio.create_task(_run_demo())
    return {"ok": True, "speaker": pid, "text": sample, "voice_tag": vtag, "llm_calls": engine.llm_calls}


@app.post("/api/douyin")
async def control_douyin(req: DouyinReq):
    target = (req.room_id or req.ws_url or "").strip()
    if req.enable:
        dy_bridge.start(target)
    else:
        dy_bridge.stop()
    st = dy_bridge.status()
    await broadcast_all({"type": "douyin_status", **st})
    return st


@app.post("/api/test_douyin_packet")
async def test_douyin_packet(pkt: Dict[str, Any]):
    await dy_bridge._process_packet(pkt)
    return {"ok": True, "stats": dy_bridge.stats}


# ==================== Colab 面板、官方 OAuth 登录与云烘资产接口 ====================
def _open_url_in_os_browser(url: str) -> bool:
    try:
        if hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


@app.get("/api/colab/dashboard")
async def colab_dashboard():
    await colab_mgr.check_local_cloner()
    await colab_mgr.probe_cloud_studio()
    return colab_mgr.get_dashboard_state()


@app.post("/api/colab/account")
async def colab_save_account(payload: Dict[str, Any] = Body(...)):
    view = colab_mgr.update_account(payload)
    if payload.get("oauth_access_token") or payload.get("oauth_refresh_token"):
        await colab_mgr.fetch_google_cu_info()
    return {"ok": True, "account": view, "cu_info": colab_mgr.cu_info}


@app.post("/api/colab/refresh_cu")
async def colab_refresh_cu():
    cu = await colab_mgr.fetch_google_cu_info()
    return {"ok": True, "cu_info": cu}


@app.post("/api/colab/test_proxy")
async def colab_test_proxy(payload: Dict[str, Any] = Body(default={})):
    override = str(payload.get("proxy_url") or "").strip()
    return await colab_mgr.test_google_connectivity(override_proxy=override)


@app.post("/api/colab/recover")
async def colab_recover_now():
    return await colab_mgr.recover_cloud_via_admin()


@app.post("/api/colab/oauth/import_json")
async def colab_oauth_import_json(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        res = colab_mgr.import_google_client_json(raw)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"JSON 解析失败: {exc}"}, status_code=400)


@app.post("/api/colab/oauth/start")
async def colab_oauth_start(request: Request):
    colab_mgr._load_account()
    cur_id = str(colab_mgr.account.get("oauth_client_id") or "")
    if not cur_id.startswith("1014160490159-"):
        await colab_mgr.ensure_official_colab_client(force=True)
    base = str(request.base_url).rstrip("/")
    res = colab_mgr.start_official_oauth_flow(base_url=base)
    res["browser_opened"] = False
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.post("/api/colab/open_browser")
async def colab_open_browser(payload: Dict[str, Any] = Body(...)):
    target = str(payload.get("target") or "").strip()
    allowed = {
        "gcp_credentials": "https://console.cloud.google.com/apis/credentials",
        "gcp_consent": "https://console.cloud.google.com/apis/credentials/consent",
        "gcp_audience": "https://console.cloud.google.com/auth/audience",
        "colab": colab_mgr.account.get("colab_notebook_url") or "https://colab.research.google.com/",
    }
    url = allowed.get(target)
    if not url:
        return JSONResponse({"ok": False, "error": "不支持的目标链接"}, status_code=400)
    return {"ok": True, "url": url, "browser_opened": _open_url_in_os_browser(url)}


@app.get("/api/colab/oauth/callback")
async def colab_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(
            "<meta charset='utf-8'><body style='font-family:Microsoft YaHei,sans-serif;padding:36px;max-width:680px;margin:0 auto;line-height:1.7;color:#1e293b;'>"
            "<h2 style='color:#dc2626;'>⚠️ 遇到 Google 内测拦截 (403: access_denied) · 10 秒免审解决指南</h2>"
            "<p>原因：你在 Google Cloud 创建的 OAuth 应用默认处于<b>「测试中 (Testing)」</b>，且名单为空。我们已为你去掉所有敏感权限，<b>无需填写任何审核资料</b>即可一劳永逸解决：</p>"
            "<div style='background:#eff6ff;border:2px solid #60a5fa;border-radius:14px;padding:16px;margin:14px 0;'>"
            "<b>✅ 方法一（推荐，全网任意账号直接登录）：</b><br/>"
            "1. 点击下方按钮打开 Google Cloud <b>「目标对象 (Audience)」</b> 页面；<br/>"
            "2. 直接点击页面上的 <b>「Publish App / 发布应用」➔「确认」</b>（因仅含基础邮箱权限，<b>0 表单免审核当场生效</b>！）；<br/>"
            "3. 回到 Colab 面板再点一次登录即可直接成功！<br/><br/>"
            "<a href='https://console.cloud.google.com/auth/audience' target='_blank' style='display:inline-block;background:#2563eb;color:#fff;padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:900;'>🌐 打开 Google Cloud「目标对象 (Audience)」页 ↗</a>"
            "</div>"
            "<div style='background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;padding:12px;font-size:14px;'>"
            "<b>方法二：</b>在同一个 Audience 页面下方找到 <b>Test users（测试用户）</b>，点 <code>+ Add users</code> 填入你的 Google 邮箱（如 <code>your_name@gmail.com</code>）保存即可。<br/>"
            "<b>💡 温馨提示：</b>不登录 Google OAuth 账号<b>完全不影响直播</b>！你的 95GB 云端语音隧道与本机 8766 克隆已全部正常在线运行。"
            "</div></body>",
            status_code=400,
        )
    try:
        res = await colab_mgr.complete_official_oauth_callback(code=code, state=state)
    except Exception as exc:
        res = {"ok": False, "error": f"OAuth 回调处理异常（请确认 Clash 代理在线后重试）: {type(exc).__name__}: {str(exc)[:180]}"}
    if res.get("ok"):
        email = res.get("email") or "已授权账号"
        cu = res.get("cu_info", {}).get("cu_balance")
        cu_txt = f"CU 余额：{cu}" if cu is not None else "已自动保存 OAuth 长期凭证"
        return HTMLResponse(
            "<meta charset='utf-8'><body style='font-family:Microsoft YaHei,sans-serif;padding:48px;text-align:center;background:#f0fdf4;color:#14532d;'>"
            f"<h2>✅ Google 官方登录授权成功！</h2><p style='font-size:18px;margin:14px 0;'>已自动提取账号：<b>{email}</b>（{cu_txt}）</p>"
            "<p style='color:#475569;'>本窗口将在 2 秒后自动关闭并刷新控制台...</p>"
            "<script>if(window.opener){try{window.opener.postMessage({type:'colab_oauth_ok'},'*');}catch(e){}}setTimeout(()=>window.close(),1800);</script>"
            "</body>"
        )
    return HTMLResponse(
        f"<meta charset='utf-8'><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
        f"<h2>⚠️ 自动提取失败</h2><p>{res.get('error', '')}</p></body>",
        status_code=400,
    )


@app.post("/api/colab/oauth/logout")
async def colab_oauth_logout():
    colab_mgr.account["oauth_access_token"] = ""
    colab_mgr.account["oauth_refresh_token"] = ""
    colab_mgr.account["google_email"] = ""
    colab_mgr._save_account()
    colab_mgr.cu_info["cu_balance"] = None
    colab_mgr.cu_info["source"] = "unlinked"
    return {"ok": True}


def _build_recommended_addons(gpu_info: Dict[str, Any]) -> Dict[str, Any]:
    try:
        vram = float(gpu_info.get("vram_total_gb") or 0.0)
    except (TypeError, ValueError):
        vram = 0.0
    gpu_name = str(gpu_info.get("gpu_name") or "Unknown GPU")

    if vram >= 80.0:
        tier = "flagship_80gb+"
        tier_label = f"🚀 旗舰级超大显存 ({gpu_name}, {vram:.0f}GB) · 全套增强模型常驻显存零卸载"
    elif vram >= 20.0:
        tier = "pro_24gb+"
        tier_label = f"✨ 高性能 GPU ({gpu_name}, {vram:.0f}GB) · 声音克隆 + 抠图 + 场景烘焙流畅同跑"
    elif vram >= 12.0:
        tier = "standard_16gb"
        tier_label = f"🌱 标准 GPU ({gpu_name}, {vram:.0f}GB) · 常驻声音克隆 + 按需抠图"
    else:
        tier = "cpu_or_low"
        tier_label = f"💡 当前为 CPU/低显存 ({gpu_name})"

    return {
        "vram_tier": tier,
        "vram_tier_label": tier_label,
        "recommended_addons": [
            {"id": "qwen3_tts_1.7b", "name": "Qwen3-TTS-1.7B 零样本幼萌声音克隆", "vram_gb": 5, "recommended": True, "can_coexist": True, "status": "ready"},
            {"id": "birefnet_gpu", "name": "BiRefNet GPU 发丝级透明抠图", "vram_gb": 3, "recommended": True, "can_coexist": True, "status": "ready"},
            {"id": "local_qwen_0.6b", "name": "本机 Qwen3-TTS-0.6B 同源 CPU 热备", "vram_gb": 0, "recommended": True, "can_coexist": True, "status": "ready"},
        ],
    }


async def _colab_request(method: str, path: str, timeout: float = 15.0, **kwargs: Any) -> httpx.Response:
    """统一走 ColabManager 长连接池（直连优先 + 系统代理自动回退）"""
    url = f"{engine.colab_url.rstrip('/')}{path}"
    return await colab_mgr._req(method, url, timeout=timeout, **kwargs)


@app.get("/api/colab/health")
async def colab_health():
    if not engine.colab_url:
        return {
            "connected": False,
            "message": "尚未填入你的 Colab 公网隧道 URL",
            "vram_tier": "disconnected",
            "vram_tier_label": "尚未连接 Colab GPU",
            "recommended_addons": [],
        }
    try:
        r = await _colab_request("GET", "/health", timeout=8.0)
        if r.status_code == 200:
            payload = r.json()
            gpu_info = payload.get("gpu") if isinstance(payload, dict) else {}
            addon_meta = _build_recommended_addons(gpu_info if isinstance(gpu_info, dict) else {})
            return {
                "connected": True,
                "colab_url": engine.colab_url,
                "remote_admin_ready": bool(os.getenv("COLAB_REMOTE_URL")),
                **payload,
                **addon_meta,
            }
        return {"connected": False, "message": f"HTTP {r.status_code}", "recommended_addons": []}
    except Exception as exc:
        return {"connected": False, "message": str(exc)[:100], "recommended_addons": []}


@app.post("/api/colab/design_voice/{character_id}")
async def colab_design_voice(character_id: str, req: Optional[DesignVoiceReq] = Body(default=None)):
    if character_id not in PERSONAS:
        return JSONResponse({"ok": False, "error": "无效角色ID"}, status_code=400)
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先连接你的 Colab GPU 服务地址"}, status_code=400)
    cfg = req or DesignVoiceReq()
    headers = {"Authorization": f"Bearer {engine.colab_token}"} if engine.colab_token else {}
    try:
        r = await _colab_request(
            "POST",
            f"/v1/voices/design/{character_id}",
            timeout=90.0,
            headers=headers,
            json={"instruct": cfg.instruct, "ref_text": cfg.ref_text},
        )
        body = r.json()
        if r.status_code == 200 and body.get("ok"):
            dl = await _colab_request("GET", f"/v1/voices/ref/{character_id}", timeout=30.0)
            if dl.status_code == 200 and len(dl.content) > 2000:
                dst = VOICE_REFS_DIR / f"{character_id}_baby_ref.wav"
                dst.write_bytes(dl.content)
                body["local_ref_url"] = f"/static/voice_refs/{dst.name}?v={int(time.time())}"
                engine.invalidate_asset_cache()
                await colab_mgr.notify_local_cloner_reload()
            await broadcast_all(engine.snapshot())
        return JSONResponse(body, status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/colab/sync_refs")
async def colab_sync_refs():
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先连接你的 Colab 服务地址"}, status_code=400)
    saved: Dict[str, str] = {}
    for pid in PERSONA_ORDER:
        try:
            dl = await _colab_request("GET", f"/v1/voices/ref/{pid}", timeout=20.0)
            if dl.status_code == 200 and len(dl.content) > 2000:
                dst = VOICE_REFS_DIR / f"{pid}_baby_ref.wav"
                dst.write_bytes(dl.content)
                saved[pid] = f"/static/voice_refs/{dst.name}"
        except Exception:
            continue
    if saved:
        engine.invalidate_asset_cache()
        await colab_mgr.notify_local_cloner_reload()
    await broadcast_all(engine.snapshot())
    return {"ok": bool(saved), "synced_count": len(saved), "files": saved}


@app.post("/api/colab/lock_voice/{character_id}")
async def colab_lock_voice(character_id: str, file: UploadFile = File(...)):
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先连接你的 Colab 服务地址"}, status_code=400)
    raw = await file.read()
    try:
        r = await _colab_request(
            "POST",
            f"/v1/voices/lock/{character_id}",
            timeout=30.0,
            headers={"Authorization": f"Bearer {engine.colab_token}"},
            files={"file": (file.filename or "ref.wav", raw, file.content_type or "audio/wav")},
        )
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/colab/matting/{character_id}")
async def colab_matting_replace(character_id: str, file: UploadFile = File(...)):
    if character_id not in PERSONAS:
        return JSONResponse({"ok": False, "error": "无效角色ID"}, status_code=400)
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先启动并填入你的 Colab 公网隧道地址"}, status_code=400)
    raw = await file.read()
    try:
        r = await _colab_request(
            "POST",
            "/v1/matting/birefnet",
            timeout=60.0,
            headers={"Authorization": f"Bearer {engine.colab_token}"},
            files={"file": (file.filename or "raw.png", raw, file.content_type or "image/png")},
        )
        if r.status_code == 200 and len(r.content) > 1000:
            dst = STATIC_DIR / "avatars" / f"{character_id}.png"
            dst.write_bytes(r.content)
            engine.invalidate_asset_cache()
            return {"ok": True, "engine": "Colab BiRefNet GPU", "saved": f"/static/avatars/{character_id}.png"}
        return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Colab 抠图请求失败: {exc}"}, status_code=502)


@app.post("/api/colab/bake_scene")
async def colab_bake_scene(req: Optional[BakeSceneReq] = Body(default=None)):
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先启动并填入你的 Colab 公网隧道地址"}, status_code=400)

    cfg = req or BakeSceneReq()
    tod = (cfg.time_of_day or "both").strip().lower()
    targets = ["day"] if tod == "day" else (["night"] if tod == "night" else ["day", "night"])

    baked: Dict[str, str] = {}
    engines_used: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    headers = {"Content-Type": "application/json"}
    if engine.colab_token:
        headers["Authorization"] = f"Bearer {engine.colab_token}"

    for mode_name in targets:
        payload = {
            "time_of_day": mode_name,
            "width": 1080,
            "height": 1920,
            "style": cfg.style or "ghibli_blue_archive",
            "prompt_extra": cfg.prompt_extra or "",
        }
        try:
            r = await _colab_request("POST", "/v1/scene/bake", timeout=180.0, json=payload, headers=headers)
            if r.status_code == 200 and len(r.content) > 5000:
                dst = SCENE_DIR / f"courtyard_{mode_name}.jpg"
                dst.write_bytes(r.content)
                baked[mode_name] = f"/static/scene/courtyard_{mode_name}.jpg?v={int(time.time())}"
                engines_used[mode_name] = r.headers.get("X-Scene-Engine", "colab-gpu")
            else:
                errors[mode_name] = f"HTTP {r.status_code}: {(r.text or '')[:140]}"
        except Exception as exc:
            errors[mode_name] = str(exc)[:160]

    engine.invalidate_asset_cache()
    has_day = engine._has_baked_scene("day")
    has_night = engine._has_baked_scene("night")
    if baked:
        await broadcast_all({"type": "scene_updated", "baked_scene_day": has_day, "baked_scene_night": has_night, "ts": int(time.time())})
        await broadcast_all(engine.snapshot())
        return {"ok": True, "baked": baked, "engines": engines_used, "errors": errors, "baked_scene_day": has_day, "baked_scene_night": has_night}

    return JSONResponse(
        {"ok": False, "error": "Colab 场景烘焙失败", "errors": errors, "baked_scene_day": has_day, "baked_scene_night": has_night},
        status_code=502,
    )


@app.post("/api/scene/clear")
async def clear_baked_scene(req: Optional[ClearSceneReq] = Body(default=None)):
    cfg = req or ClearSceneReq()
    tod = (cfg.time_of_day or "both").strip().lower()
    targets = ["day", "night"] if tod not in ("day", "night") else [tod]
    removed: List[str] = []
    for mode_name in targets:
        p = SCENE_DIR / f"courtyard_{mode_name}.jpg"
        if p.exists():
            p.unlink(missing_ok=True)
            removed.append(mode_name)

    engine.invalidate_asset_cache()
    has_day = engine._has_baked_scene("day")
    has_night = engine._has_baked_scene("night")
    await broadcast_all({"type": "scene_updated", "baked_scene_day": has_day, "baked_scene_night": has_night, "ts": int(time.time())})
    await broadcast_all(engine.snapshot())
    return {"ok": True, "removed": removed, "baked_scene_day": has_day, "baked_scene_night": has_night}


@app.post("/api/colab/bake_voices")
async def colab_bake_voices(req: Optional[BakeVoicesReq] = Body(default=None)):
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先启动并填入你的 Colab 公网隧道地址"}, status_code=400)

    cfg = req or BakeVoicesReq()
    target_personas = [p for p in (cfg.personas or PERSONA_ORDER) if p in PERSONAS]
    target_tags = [t for t in (cfg.tags or list(VOICE_BANK_TAGS)) if t in VOICE_BANK_TAGS]
    if not target_personas or not target_tags:
        return JSONResponse({"ok": False, "error": "没有有效的角色或语音标签可烘焙"}, status_code=400)

    headers = {"Content-Type": "application/json"}
    if engine.colab_token:
        headers["Authorization"] = f"Bearer {engine.colab_token}"

    total_tasks = len(target_personas) * len(target_tags)
    saved_count = 0
    skipped_count = 0
    failed_items: List[Dict[str, str]] = []
    done_tasks = 0

    for pid in target_personas:
        pdir = VOICE_BANK_DIR / pid
        pdir.mkdir(parents=True, exist_ok=True)
        phrases = VOICE_BANK_PHRASES.get(pid, VOICE_BANK_PHRASES["deepseek"])
        for tag in target_tags:
            done_tasks += 1
            dst_mp3 = pdir / f"{tag}.mp3"
            dst_wav = pdir / f"{tag}.wav"
            if not cfg.overwrite and (
                (dst_mp3.exists() and dst_mp3.stat().st_size > 500)
                or (dst_wav.exists() and dst_wav.stat().st_size > 500)
            ):
                skipped_count += 1
                continue

            phrase = phrases.get(tag, "大家好呀~")
            emo = VOICE_TAG_EMOTION_MAP.get(tag, "happy")
            payload = {
                "model": engine.colab_tts_model or "qwen3-tts-1.7b",
                "input": phrase,
                "text": phrase,
                "voice": pid,
                "emotion": emo,
                "response_format": "mp3",
            }
            try:
                r = await _colab_request("POST", "/v1/audio/speech", timeout=60.0, json=payload, headers=headers)
                if r.status_code == 200 and len(r.content) > 500:
                    is_wav = len(r.content) >= 12 and r.content[:4] == b"RIFF" and r.content[8:12] == b"WAVE"
                    if is_wav:
                        dst_mp3.unlink(missing_ok=True)
                        dst_wav.write_bytes(r.content)
                    else:
                        dst_wav.unlink(missing_ok=True)
                        dst_mp3.write_bytes(r.content)
                    # 成对落盘精确录音旁白 .txt，确保以后 100% 字音一致零偏差！
                    (pdir / f"{tag}.txt").write_text(phrase, encoding="utf-8")
                    saved_count += 1
                else:
                    failed_items.append({"persona_id": pid, "tag": tag, "error": f"HTTP {r.status_code}: {(r.text or '')[:100]}"})
            except Exception as exc:
                failed_items.append({"persona_id": pid, "tag": tag, "error": str(exc)[:120]})

        engine.invalidate_asset_cache()
        await broadcast_all({
            "type": "voice_bank_progress",
            "persona_id": pid,
            "done": done_tasks,
            "total": total_tasks,
            "saved": saved_count,
            "voice_bank_counts": get_voice_bank_counts(),
        })
        await broadcast_all(engine.snapshot())

    counts = get_voice_bank_counts()
    ok = saved_count > 0 or (skipped_count > 0 and not failed_items)
    return JSONResponse(
        {
            "ok": ok,
            "total_attempted": total_tasks,
            "saved": saved_count,
            "skipped": skipped_count,
            "failed_count": len(failed_items),
            "failed": failed_items[:12],
            "voice_bank_counts": counts,
        },
        status_code=200 if ok else 502,
    )


@app.post("/api/colab/push_public_ref/{character_id}")
async def colab_push_public_ref(character_id: str, req: Optional[PushPublicRefReq] = Body(default=None)):
    if character_id not in PERSONAS:
        return JSONResponse({"ok": False, "error": "无效角色ID"}, status_code=400)
    if not engine.colab_url:
        return JSONResponse({"ok": False, "error": "请先启动并填入你的 Colab 公网隧道地址"}, status_code=400)

    cfg = req or PushPublicRefReq()
    ref_path: Optional[Path] = None
    if cfg.filename.strip():
        safe_name = Path(cfg.filename.strip()).name
        candidate = VOICE_REFS_DIR / safe_name
        if candidate.exists() and candidate.is_file():
            ref_path = candidate
    else:
        for name in (f"{character_id}_baby_ref.wav", f"{character_id}_public_vocal.mp3", "deepseek_public_vocal.mp3"):
            candidate = VOICE_REFS_DIR / name
            if candidate.exists() and candidate.is_file():
                ref_path = candidate
                break
        if ref_path is None:
            refs = list_public_refs()
            if refs:
                ref_path = VOICE_REFS_DIR / refs[0]

    if ref_path is None or not ref_path.exists():
        return JSONResponse({"ok": False, "error": "本地 static/voice_refs/ 中未找到可用的参考干声文件"}, status_code=404)

    raw = ref_path.read_bytes()
    mime = "audio/wav" if ref_path.suffix.lower() == ".wav" else "audio/mpeg"
    form_data = {"ref_text": cfg.ref_text.strip()} if cfg.ref_text.strip() else {}
    try:
        r = await _colab_request(
            "POST",
            f"/v1/voices/lock/{character_id}",
            timeout=30.0,
            headers={"Authorization": f"Bearer {engine.colab_token}"},
            files={"file": (ref_path.name, raw, mime)},
            data=form_data,
        )
        try:
            body = r.json()
        except Exception:
            body = {"ok": r.status_code == 200, "raw": r.text[:200]}
        if r.status_code == 200:
            engine.invalidate_asset_cache()
            await broadcast_all(engine.snapshot())
            return JSONResponse({"ok": True, "character_id": character_id, "source_file": ref_path.name, "colab_response": body})
        return JSONResponse({"ok": False, "character_id": character_id, "source_file": ref_path.name, "error": body}, status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"推送参考干声到 Colab 失败: {exc}"}, status_code=502)


if __name__ == "__main__":
    import sys
    import uvicorn

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")
