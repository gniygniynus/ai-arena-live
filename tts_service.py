# -*- coding: utf-8 -*-
"""
云烘本地用 · 4层智能幼萌语音路由服务 (tts_service.py · v10 高性能加固版)
核心优化：
1. 模块级共享 HTTP 长连接池（严格区分本机 127.0.0.1 trust_env=False 直连池与远端池，避免 Clash 代理冲突与频繁握手开销）；
2. Colab 云端智能熔断器（Circuit Breaker）：一旦云端隧道失联或返回 502/503/530/404，立即触发 30 秒熔断冷却，
   后续台词 0ms 瞬切离线云烘库与本机 8766 同源 Qwen3-TTS-0.6B，绝不再出现 26s×2 傻等卡顿！
3. 智能离线顺序：云端不可用且命中高频标签 (enter/exit/thanks) 时优先 0ms 秒放 48kHz 云烘真音频，自由动态台词走本机 8766 实时克隆；
4. 解锁 Edge-TTS 兜底情绪语速与音高动态范围 [-12%, +28%]，并节流音频缓存清理（每 45s 执行一次防磁盘抖动）。
"""

import asyncio
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import edge_tts
import httpx

from personas import EMOTION_PROSODY, PERSONA_ORDER

BASE_DIR = Path(__file__).parent
AUDIO_DIR = BASE_DIR / "static" / "audio"
VOICE_BANK_DIR = BASE_DIR / "static" / "voice_bank"
VOICE_REFS_DIR = BASE_DIR / "static" / "voice_refs"

AUDIO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_BANK_DIR.mkdir(parents=True, exist_ok=True)
VOICE_REFS_DIR.mkdir(parents=True, exist_ok=True)

FALLBACK_VOICE = "zh-CN-XiaoyiNeural"

VOICE_BANK_TAGS: Tuple[str, ...] = ("enter", "exit", "think", "laugh", "flip", "thanks")

VOICE_TAG_ALIASES: Dict[str, str] = {
    "enter": "enter",
    "greet": "enter",
    "greeting": "enter",
    "hello": "enter",
    "exit": "exit",
    "bye": "exit",
    "shy": "exit",
    "think": "think",
    "thinking": "think",
    "laugh": "laugh",
    "happy": "laugh",
    "cheer": "laugh",
    "flip": "flip",
    "flip_table": "flip",
    "angry": "flip",
    "thanks": "thanks",
    "thank": "thanks",
    "gift_thanks": "thanks",
}

VOICE_TAG_EMOTION_MAP: Dict[str, str] = {
    "enter": "happy",
    "exit": "shy",
    "think": "thinking",
    "laugh": "happy",
    "flip": "angry",
    "thanks": "happy",
}

VOICE_BANK_PHRASES: Dict[str, Dict[str, str]] = {
    "gpt": {
        "enter": "推开小木门啦！首先今天阳光超舒服，其次戴好金皇冠的龙宝来陪大家聊天咯！",
        "exit": "午睡小闹钟响啦，白尾巴有点酸了，龙宝先回小屋充个电，一会儿再来找你们呀！",
        "thanks": "哇呀，谢谢小伙伴送给龙宝的小心心呀！晃晃白尾巴认真记住你啦，超喜欢！",
        "laugh": "嘻嘻嘻，太开心啦！今天小院露台上的微风吹得龙宝心情超级好呢！",
        "think": "让我歪着小脑袋首先想一想，其次再算一算，总而言之马上就有好主意啦！",
        "flip": "哎呀这回节奏不太对嘛，不算不算，龙宝宣布咱们重新来一轮更精彩的！",
    },
    "deepseek": {
        "enter": "嘿咻！鲸宝摇着蓝鲸大尾巴跑上台啦，直播间今天有没有香香的小零食分我呀？",
        "exit": "哎呀厨房电饭煲哔哔响啦！白米饭蒸熟咯，鲸宝先溜回小屋干饭充电，回头见！",
        "thanks": "哇塞谢谢送给鲸宝的小心心呀！开心甩甩蓝鲸大尾巴，我牢牢记住你啦！",
        "laugh": "嘻嘻嘻太好玩啦！蓝鲸尾巴拍得啪嗒啪嗒响，今天跟大家聊天超级快乐呀！",
        "think": "小脑袋瓜正在转圈圈深度思考中，等鲸宝两秒钟马上灵光一闪哦！",
        "flip": "哼！谁准你们逗鲸宝的呀，气鼓鼓甩一下蓝尾巴，这轮不算咱们重新来！",
    },
    "claude": {
        "enter": "哈喽大家下午好呀，葵葵轻手轻脚跑上露台啦，今天的微风吹得好温柔呢！",
        "exit": "对不起呀大家，葵葵的体力小沙漏快见底啦，先回小屋喝杯热牛奶充充电哦！",
        "thanks": "呀，谢谢你送给葵葵的小心心呀！我会一直记在心里珍藏起来的，好开心！",
        "laugh": "嘻嘻，今天小院里大家聊得太开心啦，葵葵的大尾巴都在跟着摇晃呢！",
        "think": "请等葵葵认真思考两秒钟，整理好思绪再乖乖回答大家的问题哦！",
        "flip": "呜哇不好意思刚才有点小失误，请允许葵葵重新整理一下再开始吧！",
    },
    "gemini": {
        "enter": "叮铃铃，星喵竖起紫蓝大猫耳闪亮登场！今天大家的弹幕我都会好好看哦！",
        "exit": "左脑说还想玩，右脑说该回星光屋充电啦，星喵先去打个盹，一会儿回来找你们！",
        "thanks": "哇哇哇谢谢你的小心心呀！紫蓝大猫耳开心得竖起来啦，星喵牢牢记住你咯！",
        "laugh": "耶耶耶太好玩啦！连猫尾巴上的小星光都跟着一闪一闪亮晶晶啦！",
        "think": "诶嘿，让星喵竖起小猫耳好好回想一下，我的脑洞可是全院最灵的呢！",
        "flip": "呜哇刚才左脑和右脑打架没跟上节奏嘛，咱们重新来一遍好不好呀！",
    },
    "grok": {
        "enter": "嘿嘿嘿，吃瓜小魔王瓜瓜闪亮登场！直播间里有什么劲爆趣事快抛出来听听！",
        "exit": "哼哼，爆米花小口袋空啦，瓜瓜先溜回屋爆一锅焦糖味儿的，回头继续吃瓜！",
        "thanks": "哇哦谢谢送给瓜瓜的小心心呀！算你眼光超棒，本小魔王记住你啦嘻嘻！",
        "laugh": "哈哈哈哈太逗太好玩啦！直播间就得这么热热闹闹互相吐槽才带劲嘛！",
        "think": "让瓜瓜歪着双马尾想个最俏皮好玩的新点子，嘿嘿马上就要想出来啦！",
        "flip": "哈哈哈哈太慢啦都快打哈欠咯，看瓜瓜给你换个更刺激更好玩的新花样！",
    },
    "kimi": {
        "enter": "叮当！顶着金色小弯月的月月跑上台啦，今天想听小故事还是跟大家斗斗嘴呀？",
        "exit": "窗台上的童话书才翻到一半呢，月月先回小屋充个电，一会儿继续陪大家聊天！",
        "thanks": "谢谢送给月月的小心心呀，像夜空里最亮的小星星一样暖，我记住你啦！",
        "laugh": "嘻嘻嘻，月牙丝带都跟着微风轻轻跳起舞来啦，今天真像童话一样开心呢！",
        "think": "让月月在微风里静静想一想，就像翻开童话书最精彩的下一页呢！",
        "flip": "不行不行，刚才那个展开太草率啦，月月宣布咱们重新来一段更浪漫的！",
    },
    "qwen": {
        "enter": "大家安好呀，小桂花梳好双丸子头来串门啦，今天微风正好咱们聊点什么呢？",
        "exit": "小茶房煮的桂花乌龙快到火候啦，小桂花先回屋端茶壶充电，一会儿见呀！",
        "thanks": "多谢小伙伴送给小桂花的小心心呀！口袋里装着甜甜桂花糖，记住你咯！",
        "laugh": "嘻嘻，露台上欢声笑语真热闹呀，连双丸子头上的金流苏都在开心摇摆呢！",
        "think": "且让小桂花静下心来琢磨琢磨这个小巧思，马上就有妙招告诉大家啦！",
        "flip": "哎呀呀，刚刚这一下可不算数哦，咱们重新雅致热闹地再来一回嘛！",
    },
    "zhipu": {
        "enter": "扶好青蓝小贝雷帽，班长准时来露台报到啦，大家今天的心情指数都满分吗？",
        "exit": "看了一下时间，小盆栽到点浇水啦，班长先回小屋忙两分钟充电，一会儿见！",
        "thanks": "谢谢送来的小心心呀，班长已经工工整整把你记进头号感谢名单啦，超开心！",
        "laugh": "嘿嘿，难得在阳光露台上这么放松开心，今天必须记一笔满分好心情！",
        "think": "先别急，让班长仔仔细细推演分析一下，马上给出最靠谱的结论哦！",
        "flip": "咳，刚刚数据出现一点小偏差，按班长规范咱们直接重置重新来过！",
    },
}

# ==================== 连接池与云端智能熔断器状态 ====================
_local_http_client: Optional[httpx.AsyncClient] = None
_direct_http_client: Optional[httpx.AsyncClient] = None
_proxy_http_client: Optional[httpx.AsyncClient] = None

_colab_circuit_until: float = 0.0
_colab_circuit_reason: str = ""
_last_cleanup_ts: float = 0.0
_tts_stats: Dict[str, int] = {
    "colab_cloud": 0,
    "local_genie": 0,
    "voice_bank_offline": 0,
    "edge_tts_fallback": 0,
}


def _get_local_client() -> httpx.AsyncClient:
    """访问 127.0.0.1 / localhost 专用长连接池：强制 trust_env=False 绕过 Clash HTTP_PROXY"""
    global _local_http_client
    if _local_http_client is None or _local_http_client.is_closed:
        _local_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=28.0, write=10.0, pool=5.0),
            trust_env=False,
        )
    return _local_http_client


def _get_remote_client(use_proxy: bool) -> httpx.AsyncClient:
    """访问远端 Colab 隧道专用长连接池"""
    global _direct_http_client, _proxy_http_client
    timeout = httpx.Timeout(connect=3.5, read=15.0, write=10.0, pool=5.0)
    if use_proxy:
        if _proxy_http_client is None or _proxy_http_client.is_closed:
            _proxy_http_client = httpx.AsyncClient(timeout=timeout, trust_env=True)
        return _proxy_http_client
    if _direct_http_client is None or _direct_http_client.is_closed:
        _direct_http_client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    return _direct_http_client


async def close_tts_clients() -> None:
    global _local_http_client, _direct_http_client, _proxy_http_client
    for cli in (_local_http_client, _direct_http_client, _proxy_http_client):
        if cli is not None and not cli.is_closed:
            try:
                await cli.aclose()
            except Exception:
                pass
    _local_http_client = None
    _direct_http_client = None
    _proxy_http_client = None


def mark_colab_offline(cooldown_sec: float = 30.0, reason: str = "watchdog_offline") -> None:
    """标记 Colab 云端语音暂时不可用，熔断期间 0ms 直切本地引擎"""
    global _colab_circuit_until, _colab_circuit_reason
    _colab_circuit_until = max(_colab_circuit_until, time.time() + cooldown_sec)
    _colab_circuit_reason = reason


def reset_colab_circuit() -> None:
    """Colab 恢复在线或更新配置时立即解除熔断"""
    global _colab_circuit_until, _colab_circuit_reason
    _colab_circuit_until = 0.0
    _colab_circuit_reason = ""


def get_tts_runtime_status() -> Dict[str, Any]:
    now = time.time()
    return {
        "colab_circuit_open": now < _colab_circuit_until,
        "colab_circuit_remaining_sec": max(0, int(_colab_circuit_until - now)),
        "colab_circuit_reason": _colab_circuit_reason if now < _colab_circuit_until else "",
        "route_hits": dict(_tts_stats),
    }


def cleanup_old_audio(max_age_sec: int = 240, force: bool = False) -> None:
    """节流清理临时合成音频：每 45 秒最多扫盘一次，彻底避免每次说话产生磁盘 I/O 抖动"""
    global _last_cleanup_ts
    now = time.time()
    if not force and (now - _last_cleanup_ts) < 45.0:
        return
    _last_cleanup_ts = now
    try:
        for ext in ("*.mp3", "*.wav", "*.ogg"):
            for f in AUDIO_DIR.glob(ext):
                if now - f.stat().st_mtime > max_age_sec:
                    f.unlink(missing_ok=True)
    except Exception:
        pass


def format_signed(val: int, unit: str) -> str:
    return f"+{val}{unit}" if val >= 0 else f"{val}{unit}"


def normalize_voice_tag(tag: str) -> str:
    if not tag:
        return ""
    return VOICE_TAG_ALIASES.get(tag.strip().lower(), "")


def get_voice_bank_phrase(speaker_id: str, tag: str) -> str:
    """优先读取与音频文件成对绑定的 .txt 真实录音旁白，100% 杜绝进出场字与声音不符！"""
    norm = normalize_voice_tag(tag)
    if not norm:
        return ""
    safe_pid = Path(speaker_id or "deepseek").name
    txt_file = VOICE_BANK_DIR / safe_pid / f"{norm}.txt"
    if txt_file.exists():
        try:
            content = txt_file.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception:
            pass
    return VOICE_BANK_PHRASES.get(speaker_id, VOICE_BANK_PHRASES["deepseek"]).get(norm, "")


def get_voice_bank_file(speaker_id: str, tag: str) -> Optional[Path]:
    norm = normalize_voice_tag(tag)
    if not norm or not speaker_id:
        return None
    safe_pid = Path(speaker_id).name
    for ext in (".mp3", ".wav"):
        candidate = VOICE_BANK_DIR / safe_pid / f"{norm}{ext}"
        if candidate.exists() and candidate.stat().st_size > 500:
            return candidate
    return None


def get_voice_bank_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for pid in PERSONA_ORDER:
        pdir = VOICE_BANK_DIR / pid
        if not pdir.exists():
            counts[pid] = 0
            continue
        found_tags = set()
        for ext in ("*.mp3", "*.wav"):
            for f in pdir.glob(ext):
                if f.stat().st_size > 500:
                    found_tags.add(f.stem)
        counts[pid] = len(found_tags)
    return counts


def list_public_refs() -> List[str]:
    if not VOICE_REFS_DIR.exists():
        return []
    files: List[str] = []
    for f in sorted(VOICE_REFS_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in (".mp3", ".wav", ".ogg", ".flac", ".m4a") and f.stat().st_size > 500:
            files.append(f.name)
    return files


def is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    except Exception:
        return False


def _detect_audio_ext(content: bytes, content_type: str = "") -> str:
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE":
        return ".wav"
    if content[:4] == b"OggS":
        return ".ogg"
    if "wav" in (content_type or "").lower():
        return ".wav"
    return ".mp3"


def _estimate_audio_duration(file_path: Path, text: str = "", is_edge_mp3: bool = False) -> float:
    """精确解析 WAV 采样头时长，或按码率智能估算 MP3 时长"""
    try:
        if file_path.exists():
            with file_path.open("rb") as rf:
                head = rf.read(12)
            if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                with wave.open(str(file_path), "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    if rate > 0:
                        return round(max(1.1, min(16.0, frames / float(rate))), 2)
            size_bytes = file_path.stat().st_size
            if is_edge_mp3 and size_bytes > 200:
                return round(max(1.4, min(16.0, size_bytes / 6000.0)), 2)
            if size_bytes > 2000:
                est_by_size = size_bytes / 20000.0
                est_by_text = len((text or "").strip()) * 0.21 + 0.55 if text else est_by_size
                return round(max(1.5, min(16.0, max(est_by_size, est_by_text * 0.85))), 2)
    except Exception:
        pass
    clean_len = len((text or "").strip())
    return round(max(1.5, min(11.5, clean_len * 0.20 + 0.55)), 2)


async def _synthesize_colab_tts(
    text: str,
    speaker_id: str,
    emotion: str,
    api_url: str,
    api_key: str,
    model_name: str,
) -> Optional[Tuple[str, float]]:
    """Layer 1: Colab 95GB 云端实时声音克隆（带智能熔断防卡顿）"""
    if not api_url or time.time() < _colab_circuit_until:
        return None

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name or "qwen3-tts-1.7b",
        "input": text,
        "text": text,
        "voice": speaker_id,
        "emotion": emotion,
        "response_format": "mp3",
    }

    if is_local_url(api_url):
        clients = [_get_local_client()]
    else:
        clients = [_get_remote_client(False), _get_remote_client(True)]

    for client in clients:
        try:
            r = await client.post(api_url, json=payload, headers=headers)
            if r.status_code == 200 and len(r.content) > 500:
                reset_colab_circuit()
                ext = _detect_audio_ext(r.content, r.headers.get("content-type", ""))
                filename = f"colab_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
                out_path = AUDIO_DIR / filename
                out_path.write_bytes(r.content)
                dur = _estimate_audio_duration(out_path, text=text, is_edge_mp3=False)
                return f"/static/audio/{filename}", dur
            # 若远端明确返回鉴权错误或隧道离线状态码，不再换代理白白重试，直接熔断 30 秒瞬切本地克隆！
            if r.status_code in (401, 403, 404, 502, 503, 521, 522, 524, 530):
                mark_colab_offline(30.0, f"http_{r.status_code}")
                return None
        except Exception:
            continue

    mark_colab_offline(30.0, "network_unreachable")
    return None


async def _synthesize_local_genie_tts(
    text: str,
    speaker_id: str,
    emotion: str,
    genie_base_url: str,
) -> Optional[Tuple[str, float]]:
    """
    Layer 2: 本机 CPU 实时克隆引擎（优先调用 Qwen3-TTS-0.6B /v1/audio/speech，兼容 Genie-TTS /tts）
    自动使用无代理长连接池访问 127.0.0.1，零代理干扰！
    """
    base = genie_base_url.strip().rstrip("/")
    if not base:
        return None

    if base.endswith("/v1/audio/speech") or base.endswith("/tts"):
        endpoints = [base]
    else:
        # 优先直击 /v1/audio/speech（一次命中 local_voice_server.py），再兼容旧 /tts
        endpoints = [f"{base}/v1/audio/speech", f"{base}/tts"]

    client = _get_local_client() if is_local_url(base) else _get_remote_client(False)
    for url in endpoints:
        if url.endswith("/v1/audio/speech"):
            payload = {
                "model": "qwen3-tts-0.6b-local",
                "input": text,
                "text": text,
                "voice": speaker_id,
                "speaker": speaker_id,
                "emotion": emotion,
                "response_format": "wav",
            }
        else:
            payload = {
                "character_name": speaker_id,
                "speaker": speaker_id,
                "voice": speaker_id,
                "text": text,
                "language": "zh",
                "split_sentence": True,
                "emotion": emotion,
            }
        try:
            r = await client.post(url, json=payload)
            if r.status_code == 200 and len(r.content) > 500:
                ext = _detect_audio_ext(r.content, r.headers.get("content-type", ""))
                filename = f"local_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
                out_path = AUDIO_DIR / filename
                out_path.write_bytes(r.content)
                dur = _estimate_audio_duration(out_path, text=text, is_edge_mp3=False)
                return f"/static/audio/{filename}", dur
            if r.status_code in (500, 503):
                # 服务正在加载或报错，不再重复打第二个同进程端点
                break
        except Exception:
            break
    return None


def _lookup_offline_voice_bank(speaker_id: str, voice_tag: str, text: str = "") -> Optional[Tuple[str, float]]:
    """Layer 3: 本地离线预烘焙 48kHz 云端语音库，0ms 零延迟秒放"""
    f = get_voice_bank_file(speaker_id, voice_tag)
    if not f:
        return None
    rel = f"/static/voice_bank/{f.parent.name}/{f.name}"
    dur = _estimate_audio_duration(f, text=text or get_voice_bank_phrase(speaker_id, voice_tag), is_edge_mp3=False)
    return rel, dur


async def _synthesize_edge_tts_fallback(
    text: str,
    voice: str,
    base_rate: int,
    base_pitch: int,
    emotion: str,
) -> Tuple[Optional[str], float]:
    """Layer 4: Edge-TTS 幼萌情绪兜底（已解除历史 +4% 锁速，完整释放开心/奶凶/害羞情绪张力！）"""
    prosody = EMOTION_PROSODY.get(emotion, EMOTION_PROSODY["talking"])
    final_rate = max(-12, min(28, base_rate + prosody["rate"]))
    final_pitch = max(4, min(24, base_pitch + prosody["pitch"]))
    final_vol = max(-15, min(18, prosody["volume"]))

    rate_str = format_signed(final_rate, "%")
    pitch_str = format_signed(final_pitch, "Hz")
    vol_str = format_signed(final_vol, "%")

    filename = f"tts_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.mp3"
    out_path = AUDIO_DIR / filename
    estimated_duration = round(max(1.6, min(11.0, len(text) * 0.20 + 0.50)), 2)

    for attempt_voice in (voice, FALLBACK_VOICE):
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=attempt_voice,
                rate=rate_str,
                pitch=pitch_str,
                volume=vol_str,
            )
            await asyncio.wait_for(communicate.save(str(out_path)), timeout=8.0)
            if out_path.exists() and out_path.stat().st_size > 200:
                dur = _estimate_audio_duration(out_path, text=text, is_edge_mp3=True)
                return f"/static/audio/{filename}", dur
        except Exception:
            continue

    return None, estimated_duration


async def synthesize_speech_detailed(
    text: str,
    voice: str,
    base_rate: int = 10,
    base_pitch: int = 10,
    emotion: str = "talking",
    speaker_id: str = "deepseek",
    external_url: str = "",
    external_key: str = "",
    external_model: str = "qwen3-tts-1.7b",
    local_genie_url: str = "",
    use_local_genie: bool = False,
    voice_tag: str = "",
) -> Tuple[Optional[str], float, str]:
    """
    4 层智能幼萌语音路由，返回 (静态访问URL, 原始时长秒数, 命中引擎名)：
    1. voice_bank_offline — 固定高频场景标签 (enter/exit/thanks) 直接 0ms 秒放本地已烘焙的 48kHz 1.7B 云端原声！
    2. colab_cloud        — 动态自由台词优先走 Colab 95GB 云端 1.7B 实时克隆（在线未熔断时）
    3. local_genie        — 云端离线/熔断时 0ms 无缝切本机 8766 CPU 同源 Qwen3-TTS-0.6B 实时克隆
    4. edge_tts_fallback  — 高情绪对比度幼萌 Edge-TTS 兜底
    """
    cleanup_old_audio()

    # Layer 1 (固定高频标签极速通道): 进出场/道谢直接 0ms 秒放已预烘焙的 95GB 云端 1.7B 真音频，告别进出场等待！
    if voice_tag.strip():
        bank_res = _lookup_offline_voice_bank(speaker_id=speaker_id, voice_tag=voice_tag, text=text)
        if bank_res:
            _tts_stats["voice_bank_offline"] += 1
            return bank_res[0], bank_res[1], "voice_bank_offline"

    # Layer 2: Colab 95GB 云端实时克隆任意动态新台词
    if external_url.strip():
        colab_res = await _synthesize_colab_tts(
            text=text,
            speaker_id=speaker_id,
            emotion=emotion,
            api_url=external_url.strip(),
            api_key=external_key.strip(),
            model_name=external_model.strip(),
        )
        if colab_res:
            _tts_stats["colab_cloud"] += 1
            return colab_res[0], colab_res[1], "colab_cloud"

    # Layer 3: 本机 CPU 零显卡同源 Qwen3-TTS-0.6B 实时克隆任意新台词
    if use_local_genie and local_genie_url.strip():
        genie_res = await _synthesize_local_genie_tts(
            text=text,
            speaker_id=speaker_id,
            emotion=emotion,
            genie_base_url=local_genie_url.strip(),
        )
        if genie_res:
            _tts_stats["local_genie"] += 1
            return genie_res[0], genie_res[1], "local_genie"

    # Layer 4: Edge-TTS 高表现力幼态兜底
    url, dur = await _synthesize_edge_tts_fallback(
        text=text,
        voice=voice,
        base_rate=base_rate,
        base_pitch=base_pitch,
        emotion=emotion,
    )
    if url:
        _tts_stats["edge_tts_fallback"] += 1
    return url, dur, "edge_tts_fallback" if url else "silent_fallback"


async def synthesize_speech(
    text: str,
    voice: str,
    base_rate: int = 10,
    base_pitch: int = 10,
    emotion: str = "talking",
    speaker_id: str = "deepseek",
    external_url: str = "",
    external_key: str = "",
    external_model: str = "qwen3-tts-1.7b",
    local_genie_url: str = "",
    use_local_genie: bool = False,
    voice_tag: str = "",
) -> Tuple[Optional[str], float]:
    url, dur, _ = await synthesize_speech_detailed(
        text=text,
        voice=voice,
        base_rate=base_rate,
        base_pitch=base_pitch,
        emotion=emotion,
        speaker_id=speaker_id,
        external_url=external_url,
        external_key=external_key,
        external_model=external_model,
        local_genie_url=local_genie_url,
        use_local_genie=use_local_genie,
        voice_tag=voice_tag,
    )
    return url, dur
