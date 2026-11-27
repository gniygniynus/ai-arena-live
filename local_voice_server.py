# -*- coding: utf-8 -*-
"""
本机 0 显卡同源实时声音克隆微服务 (Local Qwen3-TTS-12Hz-0.6B-Base · 127.0.0.1:8766)
核心特性：
- 与 95GB Colab 云端的 Qwen3-TTS-12Hz-1.7B-Base 属于同家族同声码器架构；
- 预编码 8 位极幼态参考干声 (static/voice_refs/{pid}_baby_ref.wav) 常驻本机内存；
- 支持 POST /reload_refs 热刷新参考干声缓存，采用标准 FastAPI lifespan 无弃用警告。
"""

import io
import json
import os
import subprocess
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

CPU_THREADS = max(4, min(12, (os.cpu_count() or 8) - 2))
torch.set_num_threads(CPU_THREADS)

BASE_DIR = Path(__file__).parent
VOICE_REFS_DIR = BASE_DIR / "static" / "voice_refs"

DEFAULT_REF_TEXTS: Dict[str, str] = {
    "gpt": "唔……首先呢，今天小院的阳光暖烘烘的，其次呀，龙宝摇着白色大尾巴来陪大家玩咯~",
    "claude": "对、对不起呀……今天的微风吹着向日葵花环好舒服呢，请让葵葵乖乖坐在这里陪大家聊天吧~",
    "deepseek": "嘿咻~ 鲸宝摇着蓝鲸大尾巴跑进来啦！小院里今天有没有香喷喷的小米糕分我吃呀？",
    "gemini": "叮铃铃~ 快看快看，露台上有好多亮晶晶的樱花瓣在飞呀，星喵全都记住啦！",
    "zhipu": "嗯，先别急哦，班长已经把今天的阳光和大家的名字都工工整整记进小本本里啦。",
    "grok": "嘿嘿嘿~ 太阳这么好别光顾着发呆呀，快告诉瓜瓜直播间里藏着什么好玩的小秘密！",
    "kimi": "叮当~ 快看月月头顶的金色小弯月，今天的阳光折出了好漂亮的七色小彩虹呢~",
    "qwen": "咔嗒~ 微风不燥花香正好，小桂花的口袋里还装着甜甜的桂花糖分大家吃哦~",
}


def load_ref_texts() -> Dict[str, str]:
    """优先从 static/voice_refs/refs_meta.json 动态加载最新重捏干声的精确文本，100% 避免声纹编码吞字！"""
    out = dict(DEFAULT_REF_TEXTS)
    meta_p = VOICE_REFS_DIR / "refs_meta.json"
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                for cid, info in meta.items():
                    if isinstance(info, dict) and str(info.get("ref_text") or "").strip():
                        out[cid] = str(info["ref_text"]).strip()
        except Exception as exc:
            print(f"[LocalCloner] refs_meta.json parse warn: {exc}", flush=True)
    return out


class LocalCloner:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.model = None
        self.prompts: Dict[str, Any] = {}
        self.status: str = "booting"
        self.detail: str = "正在加载本机同源 Qwen3-TTS-12Hz-0.6B-Base 到内存..."
        self.last_error: str = ""
        self.start_ts: float = time.time()
        self.last_infer_sec: float = 0.0

    def encode_all_refs(self) -> int:
        if self.model is None:
            return 0
        loaded = 0
        ref_texts = load_ref_texts()
        with self.lock:
            for cid, rtext in ref_texts.items():
                wav_p = VOICE_REFS_DIR / f"{cid}_baby_ref.wav"
                if wav_p.exists() and wav_p.stat().st_size > 2000:
                    wav, sr = sf.read(str(wav_p), dtype="float32")
                    if wav.ndim > 1:
                        wav = wav.mean(axis=-1)
                    self.prompts[cid] = self.model.create_voice_clone_prompt(
                        ref_audio=(wav, sr),
                        ref_text=rtext,
                        x_vector_only_mode=False,
                    )
                    loaded += 1
                    print(f"[LocalCloner] Cached prompt: {cid} ({len(wav)/sr:.1f}s)", flush=True)
        return loaded

    def warmup(self) -> None:
        try:
            from qwen_tts import Qwen3TTSModel

            self.status = "loading_model"
            self.detail = f"正在加载 Qwen/Qwen3-TTS-12Hz-0.6B-Base (CPU {CPU_THREADS}线程 bfloat16)..."
            print("[LocalCloner]", self.detail, flush=True)

            self.model = Qwen3TTSModel.from_pretrained(
                "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                device_map="cpu",
                dtype=torch.bfloat16,
            )

            self.status = "encoding_refs"
            self.detail = "正在将 8 位极幼态参考干声预编码常驻本机内存..."
            print("[LocalCloner]", self.detail, flush=True)
            self.encode_all_refs()

            self.status = "ready"
            self.detail = f"本机同源 Qwen3-TTS-0.6B 实时克隆已就绪（{len(self.prompts)}/8 位音色常驻内存，离线无损接力！）"
            print("[LocalCloner] READY!", self.detail, flush=True)
        except Exception as exc:
            self.status = "error"
            self.last_error = f"{exc}\n{traceback.format_exc()[-500:]}"
            self.detail = f"本机克隆加载失败: {exc}"
            print("[LocalCloner ERR]", self.last_error, flush=True)

    def synthesize(self, text: str, speaker: str) -> tuple[bytes, float]:
        with self.lock:
            if self.status != "ready" or self.model is None:
                raise HTTPException(status_code=503, detail=f"Local cloner status={self.status}: {self.detail}")
            cid = speaker if speaker in self.prompts else next(iter(self.prompts.keys()), "deepseek")
            if cid not in self.prompts:
                raise HTTPException(status_code=404, detail="No cached reference audio available")

            t0 = time.time()
            with torch.inference_mode():
                wavs, sr = self.model.generate_voice_clone(
                    text=text,
                    language="Chinese",
                    voice_clone_prompt=self.prompts[cid],
                )
            wav = np.asarray(wavs[0], dtype=np.float32)
            if wav.ndim > 1:
                wav = wav.mean(axis=-1)
            peak = float(np.max(np.abs(wav))) if wav.size else 0.0
            if peak > 0.98:
                wav = wav * (0.95 / peak)

            buf = io.BytesIO()
            sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
            elapsed = round(time.time() - t0, 2)
            self.last_infer_sec = elapsed
            return buf.getvalue(), elapsed


cloner = LocalCloner()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=cloner.warmup, daemon=True).start()
    yield


app = FastAPI(title="Local Same-Family Qwen3-TTS-0.6B Voice Cloner", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class SpeechReq(BaseModel):
    model: str = "qwen3-tts-0.6b-local"
    input: str = ""
    text: str = ""
    voice: str = "deepseek"
    speaker: str = ""
    character_name: str = ""
    emotion: str = "talking"


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "Qwen3-TTS-12Hz-0.6B-Base (Local CPU Same-Family)",
        "status": cloner.status,
        "detail": cloner.detail,
        "last_error": cloner.last_error,
        "locked_voices": sorted(list(cloner.prompts.keys())),
        "cpu_threads": CPU_THREADS,
        "last_infer_sec": cloner.last_infer_sec,
        "uptime_sec": int(time.time() - cloner.start_ts),
    }


@app.post("/reload_refs")
def reload_refs():
    if cloner.status != "ready" or cloner.model is None:
        raise HTTPException(status_code=503, detail="Model not ready yet")
    cnt = cloner.encode_all_refs()
    cloner.detail = f"本机同源 Qwen3-TTS-0.6B 实时克隆已就绪（{len(cloner.prompts)}/8 位音色常驻内存，离线无损接力！）"
    return {"ok": True, "reloaded": cnt, "locked_voices": sorted(list(cloner.prompts.keys()))}


@app.post("/v1/audio/speech")
@app.post("/tts")
def tts_endpoint(req: SpeechReq):
    txt = (req.input or req.text or "").strip()
    if not txt:
        raise HTTPException(status_code=400, detail="Empty text")
    sp = (req.voice or req.speaker or req.character_name or "deepseek").strip().lower()
    wav_bytes, elapsed = cloner.synthesize(txt, sp)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "X-TTS-Engine": "qwen3-tts-0.6b-local-cpu",
            "X-Voice-Character": sp,
            "X-Infer-Sec": str(elapsed),
        },
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("LOCAL_CLONER_PORT", "8766"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
