# -*- coding: utf-8 -*-
"""
阳光小院 AI 直播间 · 推广视频全流程制作与可调倍速混音引擎 (promo_studio_service.py)
100% 原创自研模块：
1. 支持 0.70x ~ 1.65x 无级调节讲解语速（原声干声走 FFmpeg 保音调 atempo，TTS 走原生韵律率 + EQ 统一响度）；
2. 原创 C 大调 104 BPM 田园八音盒 + 暖音电钢 + 木吉他无版权立体声 BGM 合成器；
3. O(N) 前缀和自动人声闪避 (Ducking) 母带混音；
4. 双出片引擎：
   - 极速混流重制模式 (fast_remix)：复用已有 1080P 舞台录屏缓存帧，15 秒内按新语速/新声线/新文案输出新版 1080P MP4；
   - 全流程舞台实拍模式 (full_record)：无头 Edge + CDP 逐帧捕获 58 部件弹簧骨骼实时互动画面并封装成片与爆款封面；
5. 支持 4 种声线导入管理（上传参考干声、同步 8 位直播角色、连接 Colab/外部 TTS、自定义 Edge-TTS 调音预设）。
"""

import asyncio
import base64
import json
import math
import shutil
import subprocess
import time
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import edge_tts
import httpx
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from personas import PERSONAS, PERSONA_ORDER

BASE_DIR = Path(__file__).resolve().parent
PROMO_DIR = BASE_DIR / "static" / "promo"
VOICE_REFS_DIR = BASE_DIR / "static" / "voice_refs"
VOICE_BANK_DIR = BASE_DIR / "static" / "voice_bank"
WORK_DIR = PROMO_DIR / "_build_cache"
CUSTOM_VOICES_PATH = PROMO_DIR / "custom_voices.json"

PROMO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_REFS_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"

DEFAULT_SCENES: List[Dict[str, Any]] = [
    {
        "id": "s1_hook",
        "tag": "★ 01 · 阳光小院 2D活体AI剧场",
        "voice_id": "gpt",
        "mode": "opening_3p",
        "emotion": "happy",
        "text": "推开小木门啦！首先今天阳光超舒服，其次戴好金皇冠的龙宝来陪大家聊天咯！",
        "use_bank_tag": "enter",
        "bubble_speaker": "gpt",
        "bubble_text": "推开小木门啦！会眨眼会斗嘴的 58 部件活体 AI 团长龙宝登场~",
        "bubble_color": "#7c8cf8",
    },
    {
        "id": "s2_rig",
        "tag": "★ 02 · 58部件真骨骼 8位AI轮流值班",
        "voice_id": "gpt",
        "mode": "cast_3p",
        "emotion": "happy",
        "text": "这里可不是呆板的静态贴图哦！小院里住着 8 位性格各异的 58 部件活体宝宝，会自己呼吸眨眼、害羞脸红，还会轮流上台斗嘴聊天呢！",
        "bubble_speaker": "deepseek",
        "bubble_text": "没错没错！我鲸宝负责干饭，龙宝负责讲道理，大家轮流值班超热闹~",
        "bubble_color": "#38bdf8",
    },
    {
        "id": "s3_pk",
        "tag": "★ 03 · 弹幕实时互动 红蓝辩论PK投票",
        "voice_id": "gpt",
        "mode": "pk_2p",
        "emotion": "happy",
        "text": "更上头的是全自动弹幕玩法！公屏发 1 和 2 就能实时投票决定红蓝辩论 PK 胜负，发送「出题」还能让三位宝宝排着队接力吐槽！",
        "bubble_speaker": "gpt",
        "bubble_text": "首先甜粽子软糯香甜才是正义！快发 1 给龙宝投票撑腰呀！",
        "bubble_color": "#f43f5e",
    },
    {
        "id": "s4_memory",
        "tag": "★ 04 · 独立长期记忆库 95GB真声克隆",
        "voice_id": "gpt",
        "mode": "memory_2p",
        "emotion": "happy",
        "text": "而且每位小伙伴都有独立长期记忆库！送一颗小心心请小屋伙伴上台，她就会永久记住你的名字，下次进房主动打招呼哦！",
        "bubble_speaker": "claude",
        "bubble_text": "呀！谢谢你的小心心，葵葵已经把你的名字悄悄记进小本本啦~",
        "bubble_color": "#fb923c",
        "familiar_user": "B站小伙伴",
    },
    {
        "id": "s5_tech",
        "tag": "★ 05 · 全栈自研 抖音直连与硬件闭麦",
        "voice_id": "gpt",
        "mode": "solo_outro",
        "emotion": "happy",
        "text": "免装第三方软件就能直连实时弹幕与礼物，支持开播自动硬件闭麦、真人随时接管，还有独立桌面总控面板持续进化中！",
        "bubble_speaker": "gpt",
        "bubble_text": "三大独立原生桌面面板互联互通，平时办公看剧也能轻松挂播~",
        "bubble_color": "#7c8cf8",
    },
    {
        "id": "s6_cta",
        "tag": "★ 06 · 喜欢就一键三连 评论区点名上台",
        "voice_id": "gpt",
        "mode": "outro_card",
        "emotion": "happy",
        "text": "如果觉得我们这群小家伙可爱又有趣，记得点赞投币收藏一键三连，评论区告诉龙宝你想看谁上台呀，拜拜！",
        "bubble_speaker": "gpt",
        "bubble_text": "记得一键三连支持龙宝和小院的小伙伴们哦，拜拜~",
        "bubble_color": "#f59e0b",
    },
]


def run_ffmpeg(args: List[str]) -> None:
    p = subprocess.run(
        [FFMPEG, "-y"] + args,
        capture_output=True,
        text=True,
        creationflags=0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if p.returncode != 0:
        raise RuntimeError(f"FFmpeg 执行失败:\n{p.stderr[-1000:]}")


def probe_wav_duration(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def build_atempo_chain(speed: float) -> str:
    """构建 FFmpeg 保音调变速滤镜链（支持 0.5x ~ 2.0x 任意区间无失真变速）。"""
    s = max(0.55, min(2.0, float(speed)))
    if abs(s - 1.0) < 0.015:
        return "anull"
    return f"atempo={s:.3f}"


def synthesize_original_bgm(out_wav: Path, duration_sec: float = 75.0, sr: int = 48000) -> None:
    """原创无版权治愈系田园八音盒 + 暖音电钢 + 木吉他分解拨弦立体声 BGM (C Major, 104 BPM)。"""
    n_samples = int(duration_sec * sr)
    left = np.zeros(n_samples, dtype=np.float64)
    right = np.zeros(n_samples, dtype=np.float64)

    bpm = 104.0
    beat = 60.0 / bpm
    bar = beat * 4.0

    def m2f(midi: float) -> float:
        return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))

    def add_tone(start_s: float, dur_s: float, freq: float, amp: float, pan: float = 0.0, kind: str = "bell"):
        i0 = int(start_s * sr)
        length = int(dur_s * sr)
        if i0 >= n_samples or length <= 0:
            return
        i1 = min(n_samples, i0 + length)
        t = np.arange(i1 - i0, dtype=np.float64) / sr
        if kind == "bell":
            env = (1.0 - np.exp(-t * 140.0)) * np.exp(-t * 3.1)
            sig = (
                0.70 * np.sin(2 * np.pi * freq * t)
                + 0.22 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t * 4.5)
                + 0.08 * np.sin(2 * np.pi * freq * 3.0 * t) * np.exp(-t * 7.5)
            ) * env
        elif kind == "pad":
            attack = np.minimum(1.0, t / 0.14)
            release = np.exp(-np.maximum(0.0, t - dur_s * 0.55) * 3.2)
            sig = (
                0.78 * np.sin(2 * np.pi * freq * t)
                + 0.16 * np.sin(2 * np.pi * freq * 2.0 * t)
                + 0.06 * np.sin(2 * np.pi * freq * 0.5 * t)
            ) * (attack * release)
        else:
            env = (1.0 - np.exp(-t * 180.0)) * np.exp(-t * 4.6)
            sig = (
                0.65 * np.sin(2 * np.pi * freq * t)
                + 0.25 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t * 3.0)
                + 0.10 * np.sin(2 * np.pi * freq * 3.0 * t) * np.exp(-t * 6.0)
            ) * env
        g_l = amp * math.cos((pan + 1.0) * math.pi / 4.0)
        g_r = amp * math.sin((pan + 1.0) * math.pi / 4.0)
        left[i0:i1] += sig * g_l
        right[i0:i1] += sig * g_r

    chords = [
        (48, [60, 64, 67, 72]),
        (43, [59, 62, 67, 71]),
        (45, [60, 64, 69, 72]),
        (40, [59, 64, 67, 71]),
        (41, [60, 65, 69, 72]),
        (48, [60, 64, 67, 72]),
        (50, [60, 65, 69, 74]),
        (43, [59, 62, 65, 67]),
    ]
    melody_bars = [
        [76, None, 79, 81, 79, None, 76, 72],
        [74, None, 79, None, 76, 74, 71, None],
        [72, None, 76, 79, 81, None, 79, 76],
        [74, 76, 79, None, 76, None, 74, None],
        [77, None, 81, 84, 81, None, 79, 77],
        [76, None, 79, None, 72, 74, 76, None],
        [74, 76, 77, None, 81, None, 79, 77],
        [79, None, 83, 81, 79, None, 74, None],
    ]

    total_bars = int(duration_sec / bar) + 1
    for b in range(total_bars):
        t_bar = b * bar
        bass, notes = chords[b % len(chords)]
        add_tone(t_bar, bar * 1.05, m2f(bass), amp=0.14, pan=0.0, kind="pad")
        for idx, n in enumerate(notes[:3]):
            add_tone(t_bar, bar * 0.98, m2f(n), amp=0.065, pan=-0.25 + idx * 0.25, kind="pad")
        arp_seq = [notes[0], notes[1], notes[2], notes[3], notes[2], notes[1], notes[3], notes[2]]
        for step, n in enumerate(arp_seq):
            t_step = t_bar + step * (beat * 0.5)
            add_tone(t_step, beat * 1.35, m2f(n), amp=0.085, pan=-0.35 if step % 2 == 0 else 0.35, kind="pluck")
        mel = melody_bars[b % len(melody_bars)]
        for step, m_note in enumerate(mel):
            if m_note is not None:
                t_step = t_bar + step * (beat * 0.5)
                add_tone(t_step, beat * 1.45, m2f(m_note), amp=0.115, pan=0.18 if step % 2 == 0 else -0.18, kind="bell")
                if step in (0, 4):
                    add_tone(t_step + 0.02, beat * 1.1, m2f(m_note + 12), amp=0.028, pan=-0.3, kind="bell")

    d1, d2 = int(0.11 * sr), int(0.23 * sr)
    left[d1:] += right[:-d1] * 0.16
    right[d1:] += left[:-d1] * 0.16
    left[d2:] += left[:-d2] * 0.09
    right[d2:] += right[:-d2] * 0.09

    fi, fo = int(1.2 * sr), int(2.2 * sr)
    left[:fi] *= np.linspace(0.0, 1.0, fi)
    right[:fi] *= np.linspace(0.0, 1.0, fi)
    left[-fo:] *= np.linspace(1.0, 0.0, fo)
    right[-fo:] *= np.linspace(1.0, 0.0, fo)

    peak = max(1e-6, float(np.max(np.abs(left))), float(np.max(np.abs(right))))
    scale = 0.68 / peak
    stereo = np.stack([left * scale, right * scale], axis=1)
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def wrap_text_cn(text: str, max_chars: int = 27) -> List[str]:
    lines: List[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars and ch in "，。！？、；：~！":
            lines.append(cur)
            cur = ""
        elif len(cur) >= max_chars + 4:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines[:2]


def make_subtitle_overlay_png(
    sc: Dict[str, Any],
    idx: int,
    total: int,
    out_png: Path,
    subtitle_style: str = "pastoral_cream",
    outro_cta: bool = True,
) -> None:
    """绘制零乱码方框的 1920x1080 透明字幕与章节徽章图层。"""
    W, H = 1920, 1080
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    f_tag = ImageFont.truetype(FONT_BOLD, 30)
    f_sub = ImageFont.truetype(FONT_BOLD, 44)
    f_small = ImageFont.truetype(FONT_REG, 22)

    # 清洗常见彩色 Emoji 防止 Windows 雅黑字库渲染出 □ 方框
    clean_map = {"👑": "★", "✨": "◆", "⚔️": "★", "💖": "◆", "🛠️": "◆", "🌟": "★", "🌻": "★", "👍": "★", "🪙": "·", "⭐": "·", "▶": "◆", "♥": "◆"}
    tag_txt = str(sc.get("tag") or f"★ 0{idx+1} · 阳光小院")
    sub_txt = str(sc.get("text") or sc.get("subtitle") or "")
    for k, v in clean_map.items():
        tag_txt = tag_txt.replace(k, v)
        sub_txt = sub_txt.replace(k, v)

    tb = d.textbbox((0, 0), tag_txt, font=f_tag)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    px, py = 46, 126
    d.rounded_rectangle(
        (px, py, px + tw + 44, py + th + 26),
        radius=28,
        fill=(255, 251, 235, 238),
        outline=(245, 158, 11, 255),
        width=4,
    )
    d.text((px + 22, py + 11), tag_txt, font=f_tag, fill=(124, 45, 18, 255))

    lines = wrap_text_cn(sub_txt, max_chars=27)
    line_h = 58
    box_h = len(lines) * line_h + 36
    y_box = H - box_h - 46

    if subtitle_style == "bili_yellow":
        box_fill = (15, 23, 42, 210)
        txt_fill = (253, 224, 71, 255)
    elif subtitle_style == "cinema_clean":
        box_fill = (0, 0, 0, 165)
        txt_fill = (255, 255, 255, 255)
    else:
        box_fill = (22, 16, 12, 198)
        txt_fill = (255, 252, 240, 255)

    d.rounded_rectangle(
        (140, y_box, W - 140, y_box + box_h),
        radius=26,
        fill=box_fill,
        outline=(251, 191, 36, 220),
        width=3,
    )
    for li, line in enumerate(lines):
        lb = d.textbbox((0, 0), line, font=f_sub)
        lw = lb[2] - lb[0]
        tx = (W - lw) // 2
        ty = y_box + 16 + li * line_h
        d.text((tx + 2, ty + 3), line, font=f_sub, fill=(0, 0, 0, 220))
        d.text((tx, ty), line, font=f_sub, fill=txt_fill)

    if outro_cta and (sc.get("mode") == "outro_card" or idx == total - 1):
        cx, cy, cw, ch = 1230, 260, 620, 440
        d.rounded_rectangle(
            (cx, cy, cx + cw, cy + ch),
            radius=32,
            fill=(255, 255, 255, 244),
            outline=(245, 158, 11, 255),
            width=5,
        )
        d.text((cx + 42, cy + 36), "★ 阳光小院 AI 直播间", font=ImageFont.truetype(FONT_BOLD, 42), fill=(124, 45, 18, 255))
        bullets = [
            "◆ 58 部件 2D 活体骨骼 · 8 位全明星轮播",
            "◆ 实时弹幕互动 · 红蓝 PK 投票 · 观众出题",
            "◆ 小心心长期记忆库 · 95GB 奶萌真声克隆",
            "★ 点赞 · 投币 · 收藏  一键三连支持！",
        ]
        for bi, bt in enumerate(bullets):
            by = cy + 122 + bi * 70
            bg_c = (254, 243, 199, 255) if bi == 3 else (255, 247, 237, 255)
            d.rounded_rectangle((cx + 32, by, cx + cw - 32, by + 56), radius=16, fill=bg_c, outline=(251, 191, 36, 160), width=2)
            d.text((cx + 50, by + 13), bt, font=ImageFont.truetype(FONT_BOLD, 24), fill=(69, 26, 3, 255))

    bar_w = W - 280
    prog = (idx + 1) / float(max(1, total))
    d.rounded_rectangle((140, H - 24, 140 + bar_w, H - 14), radius=5, fill=(255, 255, 255, 90))
    d.rounded_rectangle((140, H - 24, 140 + int(bar_w * prog), H - 14), radius=5, fill=(251, 191, 36, 245))
    d.text((W - 255, 28), f"龙宝推广特辑 · {idx+1}/{total}", font=f_small, fill=(255, 255, 255, 215))
    img.save(str(out_png))


def make_bilibili_cover(
    stage_frame_png: Path,
    out_jpg: Path,
    title: str = "会自己聊天的 2D 活体 AI 娘！",
    subtitle: str = "58部件真骨骼 · 弹幕红蓝PK · 小心心记忆库 · 龙宝亲声带逛！",
    badge: str = "★ 龙宝原声解说",
) -> None:
    """制作无遮挡、零方框的 1920x1080 B 站高点击率封面图。"""
    base = Image.open(stage_frame_png).convert("RGBA").resize((1920, 1080), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    for y in range(420):
        alpha = int(170 * (1.0 - y / 420.0))
        d.line([(0, y), (1920, y)], fill=(20, 14, 10, alpha))

    f_big = ImageFont.truetype(FONT_BOLD, 88)
    f_sub = ImageFont.truetype(FONT_BOLD, 44)
    f_pill = ImageFont.truetype(FONT_BOLD, 34)

    tb = d.textbbox((0, 0), title, font=f_big)
    tx = (1920 - (tb[2] - tb[0])) // 2
    ty = 116
    for dx in (-5, 0, 5):
        for dy in (-5, 0, 5):
            if dx or dy:
                d.text((tx + dx, ty + dy), title, font=f_big, fill=(124, 45, 18, 255))
    d.text((tx, ty), title, font=f_big, fill=(255, 250, 220, 255))

    sb = d.textbbox((0, 0), subtitle, font=f_sub)
    sw = sb[2] - sb[0]
    sx = (1920 - sw) // 2
    sy = 242
    d.rounded_rectangle((sx - 34, sy - 12, sx + sw + 34, sy + 62), radius=34, fill=(245, 158, 11, 242), outline=(255, 255, 255, 255), width=4)
    d.text((sx, sy), subtitle, font=f_sub, fill=(255, 255, 255, 255))

    pb = d.textbbox((0, 0), badge, font=f_pill)
    pw = pb[2] - pb[0]
    d.rounded_rectangle((1920 - pw - 96, 1080 - 110, 1920 - 40, 1080 - 42), radius=32, fill=(225, 29, 72, 238), outline=(255, 255, 255, 248), width=4)
    d.text((1920 - pw - 68, 1080 - 98), badge, font=f_pill, fill=(255, 255, 255, 255))

    final = Image.alpha_composite(base, overlay).convert("RGB")
    final.save(str(out_jpg), quality=94)


class PromoStudioService:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.custom_voices: List[Dict[str, Any]] = self._load_custom_voices()

    def _load_custom_voices(self) -> List[Dict[str, Any]]:
        if CUSTOM_VOICES_PATH.exists():
            try:
                data = json.loads(CUSTOM_VOICES_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save_custom_voices(self) -> None:
        CUSTOM_VOICES_PATH.write_text(json.dumps(self.custom_voices, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_all_voices(self) -> List[Dict[str, Any]]:
        voices: List[Dict[str, Any]] = []
        for pid in PERSONA_ORDER:
            p = PERSONAS[pid]
            sample_url = ""
            for ext in ("mp3", "wav"):
                cand = VOICE_BANK_DIR / pid / f"enter.{ext}"
                if cand.exists():
                    sample_url = f"/static/voice_bank/{pid}/enter.{ext}"
                    break
            ref_wav = VOICE_REFS_DIR / f"{pid}_baby_ref.wav"
            bp = p.get("base_pitch", 11)
            pitch_str = bp if isinstance(bp, str) and bp.endswith("Hz") else f"{int(bp):+d}Hz"
            br = p.get("base_rate", 0)
            rate_str = br if isinstance(br, str) and br.endswith("%") else f"{int(br):+d}%"
            voices.append({
                "id": pid,
                "name": p.get("nickname") or p.get("name") or pid,
                "nickname": p.get("nickname") or p.get("name") or pid,
                "label": p.get("internal_label", pid),
                "emoji": p.get("prop_emoji") or p.get("emoji") or "★",
                "prop_emoji": p.get("prop_emoji") or "★",
                "color": p.get("color", "#60a5fa"),
                "avatar_url": f"/static/avatars/{pid}.png",
                "edge_voice": p.get("voice", "zh-CN-XiaoxiaoNeural"),
                "pitch": pitch_str,
                "base_pitch": bp,
                "base_rate": rate_str,
                "sample_url": sample_url,
                "preview_url": sample_url,
                "ref_url": f"/static/voice_refs/{ref_wav.name}" if ref_wav.exists() else "",
                "ref_wav": f"/static/voice_refs/{ref_wav.name}" if ref_wav.exists() else "",
                "source": "project_persona",
            })
        for cv in self.custom_voices:
            voices.append(cv)
        return voices

    def list_videos_and_bgm(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        videos: List[Dict[str, Any]] = []
        bgms: List[Dict[str, Any]] = []
        for f in sorted(PROMO_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_dir():
                continue
            suf = f.suffix.lower()
            if suf == ".mp4":
                cover = f.with_suffix(".jpg")
                videos.append({
                    "filename": f.name,
                    "url": f"/static/promo/{f.name}",
                    "cover_url": f"/static/promo/{cover.name}" if cover.exists() else "",
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                })
            elif suf in (".wav", ".mp3") and not f.name.startswith("preview_"):
                bgms.append({
                    "filename": f.name,
                    "url": f"/static/promo/{f.name}",
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                })
        return videos, bgms

    def get_bootstrap_data(self) -> Dict[str, Any]:
        videos, bgms = self.list_videos_and_bgm()
        return {
            "ok": True,
            "backend_ready": True,
            "default_voice_speed": 1.18,
            "voices": self.list_all_voices(),
            "videos": videos,
            "bgm_tracks": bgms,
            "default_scenes": DEFAULT_SCENES,
        }

    def add_edge_preset(self, name: str, edge_voice: str, pitch: str = "+8Hz", rate: str = "+10%") -> Dict[str, Any]:
        vid = f"custom_edge_{int(time.time())}"
        item = {
            "id": vid,
            "name": name.strip() or "自定义声线",
            "emoji": "🎙️",
            "avatar_url": "/static/avatars/gpt.png",
            "edge_voice": edge_voice.strip() or "zh-CN-XiaoxiaoNeural",
            "pitch": pitch.strip() or "+8Hz",
            "base_rate": rate.strip() or "+0%",
            "sample_url": "",
            "ref_url": "",
            "source": "edge_preset",
        }
        self.custom_voices.append(item)
        self._save_custom_voices()
        return {"ok": True, "id": vid, "voice": item}

    def save_uploaded_voice_ref(self, name: str, ref_text: str, filename: str, raw: bytes) -> Dict[str, Any]:
        vid = f"ref_{int(time.time())}"
        ext = Path(filename or "ref.wav").suffix.lower()
        if ext not in (".wav", ".mp3"):
            ext = ".wav"
        raw_p = VOICE_REFS_DIR / f"{vid}_raw{ext}"
        wav_p = VOICE_REFS_DIR / f"{vid}_baby_ref.wav"
        raw_p.write_bytes(raw)
        run_ffmpeg(["-i", str(raw_p), "-ar", "24000", "-ac", "1", str(wav_p)])
        raw_p.unlink(missing_ok=True)

        meta_p = VOICE_REFS_DIR / "refs_meta.json"
        meta = {}
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta[vid] = {"name": name.strip() or vid, "ref_text": ref_text.strip()}
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        item = {
            "id": vid,
            "name": name.strip() or "克隆干声",
            "emoji": "🔊",
            "avatar_url": "/static/avatars/gpt.png",
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "pitch": "+10Hz",
            "base_rate": "+0%",
            "sample_url": f"/static/voice_refs/{wav_p.name}",
            "ref_url": f"/static/voice_refs/{wav_p.name}",
            "source": "uploaded_ref",
        }
        self.custom_voices.append(item)
        self._save_custom_voices()
        return {"ok": True, "voice_id": vid, "voice": item}

    def _resolve_voice_cfg(self, voice_id: str) -> Dict[str, Any]:
        for v in self.list_all_voices():
            if v["id"] == voice_id:
                return v
        return self.list_all_voices()[0]

    async def synthesize_single_line(
        self,
        text: str,
        voice_id: str,
        voice_speed: float = 1.18,
        out_wav: Optional[Path] = None,
        use_bank_mp3: Optional[Path] = None,
        tts_engine: str = "auto",
    ) -> Path:
        """按指定角色声线与可调节倍速 (0.70x ~ 1.65x) 生成 48kHz 标准响度 WAV。"""
        speed = max(0.70, min(1.65, float(voice_speed)))
        if out_wav is None:
            out_wav = PROMO_DIR / f"preview_{voice_id}_{int(speed*100)}.wav"

        # 1. 若匹配原声烘焙库且文本完全一致，直接对 95GB 原声做保音调变速
        if use_bank_mp3 and use_bank_mp3.exists() and tts_engine != "edge_preset":
            atempo = build_atempo_chain(speed)
            run_ffmpeg([
                "-i", str(use_bank_mp3),
                "-af", f"{atempo},loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "48000", "-ac", "1", str(out_wav),
            ])
            return out_wav

        # 2. 若开启且连接了 Colab 95GB 云端真声克隆，优先调云端再叠保音调倍速
        if tts_engine in ("auto", "colab_clone") and getattr(self.engine, "colab_url", ""):
            try:
                headers = {"Content-Type": "application/json"}
                if getattr(self.engine, "colab_token", ""):
                    headers["Authorization"] = f"Bearer {self.engine.colab_token}"
                async with httpx.AsyncClient(timeout=20.0, trust_env=False) as cli:
                    r = await cli.post(
                        f"{self.engine.colab_url.rstrip('/')}/v1/audio/speech",
                        headers=headers,
                        json={
                            "model": getattr(self.engine, "colab_tts_model", "qwen3-tts-1.7b"),
                            "input": text,
                            "text": text,
                            "voice": voice_id if voice_id in PERSONAS else "gpt",
                            "emotion": "happy",
                            "response_format": "wav",
                        },
                    )
                if r.status_code == 200 and len(r.content) > 2000:
                    tmp_cloud = WORK_DIR / f"tmp_cloud_{uuid.uuid4().hex[:8]}.wav"
                    tmp_cloud.write_bytes(r.content)
                    atempo = build_atempo_chain(speed)
                    run_ffmpeg([
                        "-i", str(tmp_cloud),
                        "-af", f"{atempo},loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-ar", "48000", "-ac", "1", str(out_wav),
                    ])
                    tmp_cloud.unlink(missing_ok=True)
                    return out_wav
            except Exception:
                pass

        # 3. 高表现力神经语音合成（把倍速直接换算为原生韵律百分比，吐字自然不发紧！）
        vcfg = self._resolve_voice_cfg(voice_id)
        edge_voice = vcfg.get("edge_voice") or "zh-CN-XiaoxiaoNeural"
        pitch = vcfg.get("pitch") or "+11Hz"
        pct = int(round((speed - 1.0) * 100))
        pct_clamped = max(-35, min(65, pct))
        rate_str = f"{pct_clamped:+d}%"

        tmp_mp3 = WORK_DIR / f"tmp_tts_{uuid.uuid4().hex[:8]}.mp3"
        comm = edge_tts.Communicate(text, voice=edge_voice, rate=rate_str, pitch=pitch)
        await comm.save(str(tmp_mp3))
        run_ffmpeg([
            "-i", str(tmp_mp3),
            "-af", "highpass=f=90,equalizer=f=3200:t=q:w=1.2:g=2,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "48000", "-ac", "1", str(out_wav),
        ])
        tmp_mp3.unlink(missing_ok=True)
        return out_wav

    def mix_master_audio(
        self,
        vo_items: List[Tuple[Dict[str, Any], Path, float]],
        bgm_wav: Path,
        out_master_wav: Path,
        voice_speed: float = 1.18,
        bgm_volume: float = 0.42,
        bgm_ducking: bool = True,
    ) -> Tuple[List[Tuple[Dict[str, Any], float, float]], float]:
        """O(N) 前缀和自动人声闪避母带混音，句间停顿随语速自适应紧凑。"""
        sr = 48000
        speed = max(0.70, min(1.65, float(voice_speed)))
        pre_gap = max(0.28, 0.42 / speed)
        between_gap = max(0.25, 0.46 / speed)
        tail_pad = 1.35

        timeline: List[Tuple[Dict[str, Any], float, float]] = []
        vo_arrays: List[Tuple[float, np.ndarray]] = []
        cur = pre_gap
        for sc, wp, _dur in vo_items:
            with wave.open(str(wp), "rb") as wf:
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
            start_s = cur
            end_s = cur + len(pcm) / sr
            timeline.append((sc, start_s, end_s + between_gap * 0.5))
            vo_arrays.append((start_s, pcm))
            cur = end_s + between_gap

        total_dur = cur + tail_pad
        last_sc, last_st, _ = timeline[-1]
        timeline[-1] = (last_sc, last_st, total_dur)

        n_total = int(total_dur * sr)
        vo_mix = np.zeros(n_total, dtype=np.float64)
        duck_mask = np.zeros(n_total, dtype=np.float64)

        for st, pcm in vo_arrays:
            i0 = int(st * sr)
            i1 = min(n_total, i0 + len(pcm))
            vo_mix[i0:i1] += pcm[:i1 - i0]
            d0 = max(0, i0 - int(0.15 * sr))
            d1 = min(n_total, i1 + int(0.18 * sr))
            duck_mask[d0:d1] = 1.0

        vol = max(0.0, min(1.0, float(bgm_volume)))
        if bgm_ducking:
            win = int(0.24 * sr)
            cs = np.cumsum(np.insert(duck_mask, 0, 0.0))
            idx_arr = np.arange(n_total)
            left_i = np.maximum(0, idx_arr - win // 2)
            right_i = np.minimum(n_total, idx_arr + win // 2 + 1)
            smooth_mask = (cs[right_i] - cs[left_i]) / (right_i - left_i)
            bgm_gain = vol * (1.0 - 0.62 * smooth_mask)
        else:
            bgm_gain = np.full(n_total, vol * 0.45, dtype=np.float64)

        with wave.open(str(bgm_wav), "rb") as wf:
            ch = wf.getnchannels()
            raw_sr = wf.getframerate()
            bgm_pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
        if raw_sr != sr or ch != 2:
            norm_bgm = WORK_DIR / "bgm_norm_48k.wav"
            run_ffmpeg(["-i", str(bgm_wav), "-ar", "48000", "-ac", "2", str(norm_bgm)])
            with wave.open(str(norm_bgm), "rb") as wf:
                bgm_pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0

        bgm_stereo = bgm_pcm.reshape(-1, 2)
        if len(bgm_stereo) < n_total:
            reps = (n_total // len(bgm_stereo)) + 1
            bgm_stereo = np.tile(bgm_stereo, (reps, 1))
        bgm_stereo = bgm_stereo[:n_total].copy()

        fo = int(1.3 * sr)
        bgm_stereo[-fo:, 0] *= np.linspace(1.0, 0.0, fo)
        bgm_stereo[-fo:, 1] *= np.linspace(1.0, 0.0, fo)

        mix_l = np.clip(vo_mix * 0.96 + bgm_stereo[:, 0] * bgm_gain, -0.98, 0.98)
        mix_r = np.clip(vo_mix * 0.96 + bgm_stereo[:, 1] * bgm_gain, -0.98, 0.98)
        master = np.stack([mix_l, mix_r], axis=1)
        pcm_out = np.clip(master * 32767.0, -32768, 32767).astype(np.int16)

        with wave.open(str(out_master_wav), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm_out.tobytes())

        return timeline, total_dur

    async def _record_stage_frames(
        self,
        timeline: List[Tuple[Dict[str, Any], float, float]],
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Path:
        """无头 Edge + CDP 实拍录制 6 镜 58 部件活体舞台画面到 WORK_DIR/frames_{idx}。"""
        from playwright.async_api import async_playwright

        base = "http://127.0.0.1:8765"

        def _post(path: str, body: dict):
            req = urllib.request.Request(
                f"{base}{path}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=6) as r:
                return json.loads(r.read().decode("utf-8"))

        _post("/api/live_status", {"status": "stopped"})
        _post("/api/guide_card", {"show": False})
        cover_src = WORK_DIR / "cover_raw.png"

        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="msedge", headless=True)
            ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
            page = await ctx.new_page()
            cdp = await ctx.new_cdp_session(page)
            await page.goto(f"{base}/stage?layout=landscape&obs=1&hide_barrage=1&mute_audio=1&guide=0", wait_until="networkidle")
            await page.wait_for_timeout(1400)

            for idx, (sc, st, ed) in enumerate(timeline):
                if progress_cb:
                    progress_cb(30 + int(45 * idx / max(1, len(timeline))), f"正在实拍第 {idx+1}/{len(timeline)} 镜 58 部件活体骨骼舞台...")
                dur = max(3.0, ed - st)
                frames_dir = WORK_DIR / f"frames_{idx}"
                if frames_dir.exists():
                    shutil.rmtree(frames_dir, ignore_errors=True)
                frames_dir.mkdir(exist_ok=True)

                mode = sc.get("mode", "opening_3p")
                if mode == "opening_3p":
                    _post("/api/set_stage_count", {"count": 3, "roster": ["gpt", "claude", "deepseek"]})
                elif mode == "cast_3p":
                    _post("/api/set_stage_count", {"count": 3, "roster": ["gemini", "deepseek", "grok"]})
                elif mode == "pk_2p":
                    _post("/api/set_stage_count", {"count": 2, "roster": ["gpt", "claude"]})
                elif mode == "memory_2p":
                    _post("/api/set_stage_count", {"count": 2, "roster": ["claude", "kimi"]})
                else:
                    _post("/api/set_stage_count", {"count": 1, "roster": [sc.get("voice_id", "gpt") if sc.get("voice_id") in PERSONAS else "gpt"]})

                await page.evaluate("""() => {
                  const pk = document.getElementById('pk-banner');
                  if (pk) pk.classList.remove('show');
                  window.__stageHandleMsg({ type: 'live_stopped' });
                }""")
                await page.wait_for_timeout(1100)

                if idx == 0:
                    await page.screenshot(path=str(cover_src))

                if mode == "pk_2p":
                    await page.evaluate("""() => {
                      const pk = document.getElementById('pk-banner');
                      if (pk) {
                        pk.classList.add('show');
                        document.getElementById('pk-title').textContent = '⚔️ 辩论 PK：甜粽子 vs 咸粽子 谁才是天下第一？';
                        document.getElementById('pk-red-label').textContent = '🔴 龙宝：甜粽子软糯香甜';
                        document.getElementById('pk-blue-label').textContent = '咸粽子鲜香流油：葵葵 🔵';
                        document.getElementById('pk-bar-red').style.width = '64%';
                        document.getElementById('pk-bar-red').textContent = '64% (32票)';
                        document.getElementById('pk-bar-blue').textContent = '(18票) 36%';
                      }
                    }""")
                elif mode == "memory_2p":
                    await page.evaluate("""() => {
                      window.__stageHandleMsg({ type: 'gift_boost', target: 'claude', user: 'B站小伙伴', gift_name: '小心心' });
                    }""")

                spk = sc.get("bubble_speaker") or (sc.get("voice_id") if sc.get("voice_id") in PERSONAS else "gpt")
                btxt = sc.get("bubble_text") or str(sc.get("text", ""))[:36]
                bcol = sc.get("bubble_color") or "#7c8cf8"
                await page.evaluate(
                    """([spk, btxt, bcol, durSec, fam]) => {
                      window.__stageHandleMsg({
                        type: 'speak',
                        speech_id: 9000 + Math.floor(Math.random()*900),
                        speaker: spk,
                        text: btxt,
                        emotion: 'happy',
                        duration: Math.max(3.5, durSec - 0.8),
                        color: bcol,
                        bubble_border: bcol,
                        bubble_bg: 'rgba(255,255,255,0.97)',
                        familiar_user: fam || ''
                      });
                    }""",
                    [spk, btxt, bcol, dur, sc.get("familiar_user", "")],
                )

                frame_paths: List[Path] = []
                frame_ts: List[float] = []
                rec_start = time.perf_counter()

                def on_frame(ev):
                    sid = ev.get("sessionId")
                    try:
                        asyncio.create_task(cdp.send("Page.screencastFrameAck", {"sessionId": sid}))
                    except Exception:
                        pass
                    now_t = time.perf_counter() - rec_start
                    if now_t > dur + 0.4:
                        return
                    fp = frames_dir / f"raw_{len(frame_paths):05d}.jpg"
                    fp.write_bytes(base64.b64decode(ev["data"]))
                    frame_paths.append(fp)
                    frame_ts.append(now_t)

                cdp.on("Page.screencastFrame", on_frame)
                await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "maxWidth": 1920, "maxHeight": 1080, "everyNthFrame": 1})
                await page.wait_for_timeout(int(dur * 1000))
                await cdp.send("Page.stopScreencast")
                cdp.remove_listener("Page.screencastFrame", on_frame)

                if not frame_paths:
                    fb = frames_dir / "raw_00000.jpg"
                    await page.screenshot(path=str(fb), type="jpeg", quality=92)
                    frame_paths.append(fb)
                    frame_ts.append(0.0)

                seq_txt = frames_dir / "seq.txt"
                lines = []
                for fi, fp in enumerate(frame_paths):
                    t_cur = frame_ts[fi]
                    t_next = frame_ts[fi + 1] if fi + 1 < len(frame_ts) else dur
                    seg_d = max(0.033, min(1.0, t_next - t_cur))
                    lines.append(f"file '{fp.as_posix()}'")
                    lines.append(f"duration {seg_d:.4f}")
                lines.append(f"file '{frame_paths[-1].as_posix()}'")
                seq_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

            await browser.close()

        _post("/api/guide_card", {"show": True})
        _post("/api/set_stage_count", {"count": 2, "roster": ["gpt", "claude"]})
        return cover_src

    async def render_promo_video(
        self,
        cfg: Dict[str, Any],
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """执行完整视频合成（支持任意 voice_speed 语速调节 + 极速缓存帧混流或全新实拍）。"""
        t0 = time.time()
        voice_speed = max(0.70, min(1.65, float(cfg.get("voice_rate") or cfg.get("voice_speed") or 1.18)))
        primary_voice = str(cfg.get("primary_voice_id") or "gpt")
        tts_engine = str(cfg.get("tts_engine") or "auto")
        subtitle_style = str(cfg.get("subtitle_style") or "pastoral_cream")
        outro_cta = bool(cfg.get("outro_cta", True))
        bgm_volume = float(cfg.get("bgm_volume", 0.42))
        bgm_ducking = bool(cfg.get("bgm_ducking", True))
        fast_remix = bool(cfg.get("fast_remix", True))
        fps = int(cfg.get("fps") or 30)
        res = str(cfg.get("resolution") or "1920x1080")
        out_name = str(cfg.get("output_filename") or "promo_longbao_v1.mp4").strip()
        if not out_name.endswith(".mp4"):
            out_name += ".mp4"

        scenes_in = cfg.get("scenes") or DEFAULT_SCENES
        scenes: List[Dict[str, Any]] = []
        for idx, raw_sc in enumerate(scenes_in):
            base_sc = dict(DEFAULT_SCENES[idx % len(DEFAULT_SCENES)])
            base_sc.update(raw_sc)
            if not raw_sc.get("voice_id"):
                base_sc["voice_id"] = primary_voice
            scenes.append(base_sc)

        if progress_cb:
            progress_cb(8, f"正在按 {voice_speed:.2f}x 倍速合成 {len(scenes)} 段角色配音...")

        vo_items: List[Tuple[Dict[str, Any], Path, float]] = []
        for idx, sc in enumerate(scenes):
            vid = str(sc.get("voice_id") or primary_voice)
            txt = str(sc.get("text") or sc.get("subtitle") or "")
            bank_mp3: Optional[Path] = None
            if idx == 0 and vid == "gpt" and "推开小木门" in txt:
                cand = VOICE_BANK_DIR / "gpt" / "enter.mp3"
                if cand.exists():
                    bank_mp3 = cand
            vo_wav = WORK_DIR / f"vo_{idx}_{int(voice_speed*100)}.wav"
            await self.synthesize_single_line(
                txt, vid, voice_speed=voice_speed, out_wav=vo_wav, use_bank_mp3=bank_mp3, tts_engine=tts_engine
            )
            vo_items.append((sc, vo_wav, probe_wav_duration(vo_wav)))

        if progress_cb:
            progress_cb(22, "正在准备原创无版权 BGM 并执行自动人声闪避母带混音...")

        bgm_rel = str(cfg.get("bgm_url") or "/static/promo/bgm_sunny_garden_musicbox.wav")
        bgm_path = PROMO_DIR / Path(bgm_rel).name
        if not bgm_path.exists():
            bgm_path = PROMO_DIR / "bgm_sunny_garden_musicbox.wav"
            if not bgm_path.exists():
                synthesize_original_bgm(bgm_path, duration_sec=85.0)

        master_wav = WORK_DIR / "master_audio.wav"
        timeline, total_dur = self.mix_master_audio(
            vo_items, bgm_path, master_wav, voice_speed=voice_speed, bgm_volume=bgm_volume, bgm_ducking=bgm_ducking
        )

        # 检查是否所有分镜均已有原画缓存帧；若缺失或用户指定全新实拍，则启动无头 Edge 实拍
        cache_ready = all((WORK_DIR / f"frames_{i}" / "seq.txt").exists() for i in range(len(timeline)))
        cover_raw = WORK_DIR / "cover_raw.png"
        if (not fast_remix) or (not cache_ready):
            cover_raw = await self._record_stage_frames(timeline, progress_cb=progress_cb)

        if progress_cb:
            progress_cb(78, "正在渲染零乱码高清字幕图层与 B 站爆款封面...")

        # 计算每段视频精确时长以严丝合缝对齐母带总时长
        seg_durs: List[float] = []
        for idx, (_sc, st, ed) in enumerate(timeline):
            if idx == 0:
                seg_durs.append(ed)
            elif idx < len(timeline) - 1:
                seg_durs.append(ed - timeline[idx - 1][2])
            else:
                seg_durs.append(max(1.5, total_dur - sum(seg_durs)))

        scale_expr = "1080:1920" if "1080x1920" in res else "1920:1080"
        clips: List[Path] = []
        for idx, (sc, _st, _ed) in enumerate(timeline):
            dur = seg_durs[idx]
            sub_png = WORK_DIR / f"sub_{idx}.png"
            make_subtitle_overlay_png(sc, idx, len(timeline), sub_png, subtitle_style=subtitle_style, outro_cta=outro_cta)

            seq_txt = WORK_DIR / f"frames_{idx}" / "seq.txt"
            # 若原缓存帧比新语速短或长，stream_loop 循环原画骨骼动作，绝不黑屏或卡住！
            clip_mp4 = WORK_DIR / f"clip_out_{idx}.mp4"
            fade_out_st = max(0.25, dur - 0.28)
            if progress_cb:
                progress_cb(80 + int(15 * (idx + 1) / len(timeline)), f"正在硬件级封装第 {idx+1}/{len(timeline)} 镜高清画面 ({dur:.1f}s)...")
            run_ffmpeg([
                "-stream_loop", "-1",
                "-f", "concat", "-safe", "0", "-i", str(seq_txt),
                "-i", str(sub_png),
                "-t", f"{dur:.3f}",
                "-filter_complex",
                f"[0:v]fps={fps},scale={scale_expr},format=yuv420p,fade=t=in:st=0:d=0.22,fade=t=out:st={fade_out_st:.3f}:d=0.22[bg];"
                f"[bg][1:v]overlay=0:0:eof_action=repeat[v]",
                "-map", "[v]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
                str(clip_mp4),
            ])
            clips.append(clip_mp4)

        concat_txt = WORK_DIR / "concat_out.txt"
        concat_txt.write_text("\n".join(f"file '{c.as_posix()}'" for c in clips), encoding="utf-8")
        out_mp4 = PROMO_DIR / out_name
        out_jpg = out_mp4.with_suffix(".jpg")

        if cover_raw.exists():
            v_label = self._resolve_voice_cfg(primary_voice).get("name", "龙宝")
            make_bilibili_cover(
                cover_raw,
                out_jpg,
                badge=f"★ {v_label} {voice_speed:.2f}x 解说",
            )

        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_txt),
            "-i", str(master_wav),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_mp4),
        ])

        if progress_cb:
            progress_cb(100, f"成片渲染完成！时长 {total_dur:.1f}s（倍速 {voice_speed:.2f}x）")

        return {
            "ok": True,
            "filename": out_mp4.name,
            "output_url": f"/static/promo/{out_mp4.name}",
            "cover_url": f"/static/promo/{out_jpg.name}" if out_jpg.exists() else "",
            "duration_sec": round(total_dur, 2),
            "size_mb": round(out_mp4.stat().st_size / (1024 * 1024), 2),
            "voice_speed": voice_speed,
            "elapsed_sec": round(time.time() - t0, 1),
        }

    def start_render_task(self, cfg: Dict[str, Any]) -> str:
        task_id = f"render_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        self.tasks[task_id] = {
            "ok": True,
            "task_id": task_id,
            "status": "running",
            "progress": 2,
            "stage_text": "已加入渲染队列，准备合成配音...",
            "output_url": "",
            "cover_url": "",
            "error": "",
        }

        def _on_prog(pct: int, msg: str):
            self.tasks[task_id]["progress"] = pct
            self.tasks[task_id]["stage_text"] = msg

        async def _runner():
            try:
                res = await self.render_promo_video(cfg, progress_cb=_on_prog)
                self.tasks[task_id].update({
                    "status": "done",
                    "progress": 100,
                    "stage_text": f"✅ 渲染成功！总时长 {res['duration_sec']}s ({res['size_mb']} MB)",
                    "output_url": res["output_url"],
                    "cover_url": res["cover_url"],
                    "result": res,
                })
            except Exception as exc:
                self.tasks[task_id].update({
                    "status": "failed",
                    "stage_text": f"❌ 渲染失败: {str(exc)[:180]}",
                    "error": str(exc)[:300],
                })

        asyncio.create_task(_runner())
        return task_id

    async def generate_script_via_llm(self, topic: str, voice_id: str = "gpt", scene_count: int = 6) -> List[Dict[str, Any]]:
        """调用项目已配置的 LLM 生成 6 段式 B 站推广分镜脚本（未填 Key 时智能返回定制模板）。"""
        api_key = getattr(self.engine, "llm_api_key", "")
        base_url = getattr(self.engine, "llm_base_url", "https://bbying.me/v1").rstrip("/")
        model = getattr(self.engine, "llm_model", "deepseek-chat")
        v_name = self._resolve_voice_cfg(voice_id).get("name", "龙宝")

        if api_key:
            prompt = (
                f"请以阳光小院 2D 活体 AI 直播间角色「{v_name}」的第一人称口吻，围绕主题「{topic}」写 {scene_count} 段紧凑活泼的 B 站短视频旁白。"
                "只返回纯 JSON 数组，每项包含 tag(如 '★ 01 · 标题') 和 text(35~55字口播文案)。"
            )
            try:
                async with httpx.AsyncClient(timeout=18.0, trust_env=False) as cli:
                    r = await cli.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7},
                    )
                if r.status_code == 200:
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    s_idx, e_idx = raw.find("["), raw.rfind("]")
                    if s_idx != -1 and e_idx > s_idx:
                        arr = json.loads(raw[s_idx : e_idx + 1])
                        out = []
                        modes = ["opening_3p", "cast_3p", "pk_2p", "memory_2p", "solo_outro", "outro_card"]
                        for i, item in enumerate(arr[:scene_count]):
                            out.append({
                                "id": f"s{i+1}",
                                "tag": str(item.get("tag") or f"★ 0{i+1} · 精彩看点"),
                                "voice_id": voice_id,
                                "mode": modes[i % len(modes)],
                                "emotion": "happy",
                                "text": str(item.get("text") or ""),
                            })
                        if out:
                            return out
            except Exception:
                pass

        return [dict(sc, voice_id=voice_id) for sc in DEFAULT_SCENES[:scene_count]]
