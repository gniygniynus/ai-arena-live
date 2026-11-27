# -*- coding: utf-8 -*-
"""
阳光小院 2D 活体骨骼 AI 娘直播间 · 核心导演与状态机引擎 (arena_engine.py · v14 旗舰互动版)
核心职责：
1. 零额度默认待机：启动默认 stopped（0 次 LLM 消耗），点击开播才驱动对话循环；
2. 8 位角色专属外号互称（龙宝/鲸宝/葵葵/星喵/瓜瓜/月月/小桂花/班长）+ SQLite 独立角色记忆库；
3. 严格 P0~P5 优先级调度队列：
   - P0: 真人开麦接管让麦 / 导播紧急插麦口播
   - P1: 1 个小心心强制替换角色上场写记忆 & 礼物专属道谢
   - P2: 2 人红蓝 PK 辩论（固定 2 人、冻结精力消耗、屏蔽非投票互动、实时统计 1/2 弹幕票数、低互动量自动触发）
   - P3: 观众出题 3 人从左到右依次吐槽后第 3 人自动退场 & 老熟人进场/弹幕从左到右依次互动
   - P4: 常规点名/独播弹幕互动 & 小屋休息 FIFO 顺序轮换出场
   - P5: 日常双缓冲预热闲聊
4. 严格 FIFO 小屋休息轮换队列：下场排队尾、上场取队首，与前端「下一位」标识 100% 一致；
5. 单轨流式字音同步（1.15x 自然人类语速对齐）+ 离场先说话后退场防气泡残留。
"""

import asyncio
import itertools
import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

import httpx
from openai import AsyncOpenAI

from memory_store import CharacterMemoryStore
from mic_guard import async_get_mic_mute, async_set_mic_mute
from personas import (
    DEBATE_TOPICS,
    GARDEN_SPOTS,
    NICKNAME_MAP,
    PERSONA_ORDER,
    PERSONAS,
    PK_DEBATE_TOPICS,
    STATIC_OBSTACLES,
    STOOLS,
)
from safety import sanitize_ai_speech, sanitize_barrage
from tts_service import (
    get_tts_runtime_status,
    get_voice_bank_counts,
    get_voice_bank_phrase,
    is_local_url,
    list_public_refs,
    reset_colab_circuit,
    synthesize_speech_detailed,
)

BASE_DIR = Path(__file__).parent
STATIC_SCENE_DIR = BASE_DIR / "static" / "scene"
VOICE_PLAYBACK_RATE: float = 0.88             # 默认 0.88x（精准定位于慢糯版与 1.0x 原速正中间，保音调不降调！）
STAMINA_DRAIN_PER_SEC: float = 100.0 / 300.0  # 精确 5 分钟（300 秒）耗尽轮换一次
LOW_INTERACTION_PK_TRIGGER_SEC: float = 55.0  # 连续 55 秒低互动自动触发红蓝 PK 辩论
PK_COOLDOWN_SEC: float = 90.0                 # 两场自动 PK 之间至少间隔 90 秒

VALID_SPEECH_EMOTIONS: Tuple[str, ...] = (
    "happy", "excited", "smug", "angry", "pout", "sad", "shy", "love",
    "shocked", "surprised", "thinking", "talking", "wink", "determined",
    "worried", "confused", "listening", "sleepy", "dizzy",
)

_RE_AUDIENCE_TOPIC = re.compile(r"^(?:出题|题目|我要出题|考考你们)\s*[：:\s]\s*(.+)$")


class ArenaEngine:
    def __init__(self, broadcast_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        self.broadcast = broadcast_fn
        self.memory_store = CharacterMemoryStore()

        # LLM 与 4 层语音算力配置（兼容读取 COLAB_TOKEN / COLAB_API_KEY / COLAB_ADMIN_TOKEN 杜绝 401 失配！）
        self.llm_base_url: str = os.getenv("LLM_BASE_URL", "https://bbying.me/v1").strip()
        self.llm_api_key: str = os.getenv("LLM_API_KEY", "").strip()
        self.llm_model: str = os.getenv("LLM_MODEL", "deepseek-v4.1-flash").strip()
        self.colab_url: str = os.getenv("COLAB_URL", os.getenv("EXT_TTS_URL", "")).strip().rstrip("/")
        self.colab_token: str = (
            os.getenv("COLAB_TOKEN")
            or os.getenv("COLAB_API_KEY")
            or os.getenv("COLAB_ADMIN_TOKEN")
            or os.getenv("EXT_TTS_KEY")
            or ""
        ).strip()
        self.colab_tts_model: str = os.getenv("EXT_TTS_MODEL", "qwen3-tts-1.7b").strip()
        # 若已配置 COLAB_URL 且未显式关闭，默认开启云端满血克隆（离线熔断自动 0ms 瞬切本机 8766）
        raw_use_colab = os.getenv("USE_COLAB_TTS", "").strip().lower()
        self.use_colab_tts: bool = (raw_use_colab == "true") if raw_use_colab in ("true", "false") else bool(self.colab_url)
        self.local_genie_url: str = os.getenv("LOCAL_GENIE_URL", "http://127.0.0.1:8766").strip().rstrip("/")
        self.use_local_genie: bool = os.getenv("USE_LOCAL_GENIE", "true").lower() == "true"

        self._llm_http_client: Optional[httpx.AsyncClient] = None
        self._client: Optional[AsyncOpenAI] = None
        self._rebuild_llm_client()

        # 默认启动为 stopped（0 API 消耗）
        self.live_status: str = "stopped"  # "stopped" | "paused" | "live"
        self.human_mic_active: bool = False
        self.auto_mute_mic: bool = True            # 开播非接管自动硬件静音物理麦克风，防止直播伴侣/OBS 误收主播杂音
        self.physical_mic_muted: bool = False      # 当前系统物理麦克风硬件静音实时状态
        self.voice_playback_rate: float = VOICE_PLAYBACK_RATE
        self.llm_calls: int = 0
        self.allow_silence: bool = True
        self.random_activities: bool = True
        self.fixed_seating: bool = True
        self._life_chat_round: int = 0

        # 核心舞台阵容 + 严格 FIFO 小屋休息轮换队列
        self.slots: List[str] = ["gpt", "deepseek", "grok"]
        self.bench_queue: List[str] = [p for p in PERSONA_ORDER if p not in self.slots]
        self.mode: str = "life"
        self.desk_prop_mode: str = "none"  # "none" | "tea" | "mic"
        self.time_of_day: str = "day"
        self.game_locked: bool = False
        self.current_topic: str = DEBATE_TOPICS[0]
        self.tension: int = 15

        # ⚔️ 红蓝 PK 辩论实时投票状态
        self.pk_state: Dict[str, Any] = {
            "active": False,
            "topic": "",
            "red_id": "",
            "blue_id": "",
            "red_stance": "",
            "blue_stance": "",
            "red_votes": 0,
            "blue_votes": 0,
            "remaining_sec": 0,
        }
        self._pk_end_ts: float = 0.0
        self._pk_stop_flag: bool = False
        self._last_pk_end_ts: float = 0.0
        self._pk_topic_cursor: int = 0
        self._pk_voter_cd: Dict[str, float] = {}

        # 🎬 观众出题 3 人从左到右吐槽状态
        self.audience_topic_state: Dict[str, Any] = {
            "active": False,
            "user": "",
            "topic": "",
            "step": 0,
        }

        # 合规去诱导互动玩法提示牌（前期不催礼物，保留小心心强换人+记忆库彩蛋机制！）
        self.show_guide_card: bool = True
        self.guide_title: str = "✨ 小院互动指南"
        self.guide_items: List[str] = [
            "💬 弹幕点名：喊外号即可随机翻牌聊",
            "⚔️ 辩论 PK：公屏发 1 / 2 实时投票",
            "🎬 观众出题：发「出题：xxx」3人接力吐槽",
            "✨ 轮换彩蛋：小心心可请休息伙伴上台并记住你",
        ]

        # 弹幕、进场、最近点名与低互动监测状态
        now = time.time()
        self._last_barrage_reply_ts: float = 0.0
        self._last_barrage_seen_ts: float = now
        self._last_interaction_ts: float = now
        self._recent_barrage_times: List[float] = []
        self._solo_idle_since: float = now
        self._recent_barrage_dedup: Dict[str, float] = {}
        self._recent_enter_cd: Dict[str, float] = {}
        self._user_last_mentioned_bench: Dict[str, Tuple[str, float]] = {}

        # 兼容保留的工具架状态
        self.rack_tools: Dict[str, bool] = {"net": True, "watering_can": True}
        self._tool_owners: Dict[str, str] = {}
        self._activity_tasks: Dict[str, asyncio.Task[Any]] = {}

        # 静态资产缓存
        self._asset_cache_ts: float = 0.0
        self._asset_cache: Dict[str, Any] = {}

        # 8 位全明星实时状态表
        self.states: Dict[str, Dict[str, Any]] = {}
        init_spots = ["lawn_front_l", "lawn_front_c", "lawn_front_r"]
        for pid in PERSONA_ORDER:
            on_stage = pid in self.slots
            sp = GARDEN_SPOTS[init_spots[self.slots.index(pid)]] if on_stage else GARDEN_SPOTS["gate_outside"]
            self.states[pid] = {
                "id": pid,
                "stamina": 100.0,
                "love_score": 20,
                "stay_seconds": 0.0,
                "entered_at": now if on_stage else 0.0,
                "emotion": "idle",
                "pose": "idle",
                "look_target": "",
                "reaction_emoji": "",
                "activity": PERSONAS[pid]["favorite_habit"],
                "target_x": sp["x"],
                "target_y": sp["y"],
                "held_tool": "",
                "is_seated": False,
                "seat_id": "",
                "watering_active": False,
                "stay_until": now + random.uniform(22.0, 48.0),
                "enter_phase": "none",
                "walk_speed": 96.0,
                "idle_posture": "stand",
                "idle_expr": "neutral",
                "idle_motion": "",
                "role_tag": "idle",
            }
        self._sync_role_tags()

        # P0~P5 严格优先级队列 (0=P0最高优 ... 5=P5日常闲聊)
        self.dialogue_history: List[Dict[str, str]] = []
        self._prio_seq = itertools.count(1)
        self.priority_queue: asyncio.PriorityQueue[Tuple[int, int, Dict[str, Any]]] = asyncio.PriorityQueue(maxsize=64)
        self._wake_event = asyncio.Event()
        self._prefetch_task: Optional[asyncio.Task[Any]] = None
        self._prefetched_chat: Optional[Dict[str, Any]] = None
        self.barrage_feed: List[Dict[str, Any]] = []
        self.last_speaker: Optional[str] = None
        self.speaking_lock = asyncio.Lock()
        self._speech_seq: int = 0
        self._running = False
        self._tasks: List[asyncio.Task[Any]] = []

    # ==================== LLM 客户端与本机代理栈隔离 ====================
    def _rebuild_llm_client(self) -> None:
        old_http = self._llm_http_client
        self._llm_http_client = None
        if old_http is not None and not old_http.is_closed:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(old_http.aclose())
            except RuntimeError:
                pass

        if not self.llm_api_key:
            self._client = None
            return

        if is_local_url(self.llm_base_url):
            self._llm_http_client = httpx.AsyncClient(timeout=12.0, trust_env=False)
            self._client = AsyncOpenAI(
                api_key=self.llm_api_key,
                base_url=self.llm_base_url,
                timeout=12.0,
                max_retries=1,
                http_client=self._llm_http_client,
            )
        else:
            self._client = AsyncOpenAI(
                api_key=self.llm_api_key,
                base_url=self.llm_base_url,
                timeout=12.0,
                max_retries=1,
            )

    def update_config(
        self,
        base_url: str,
        api_key: str,
        model: str,
        colab_url: str = "",
        colab_token: str = "",
        colab_tts_model: str = "qwen3-tts-1.7b",
        use_colab_tts: bool = False,
        local_genie_url: Optional[str] = None,
        use_local_genie: Optional[bool] = None,
        save_env: bool = True,
    ) -> None:
        prev_colab = self.colab_url
        self.llm_base_url = base_url.strip() or "https://bbying.me/v1"
        if api_key.strip() and not api_key.strip().startswith("***"):
            self.llm_api_key = api_key.strip()
        self.llm_model = model.strip() or "deepseek-v4.1-flash"
        self.colab_url = colab_url.strip().rstrip("/")
        if colab_token.strip() and not colab_token.strip().startswith("***"):
            self.colab_token = colab_token.strip()
        self.colab_tts_model = colab_tts_model.strip() or "qwen3-tts-1.7b"
        self.use_colab_tts = bool(use_colab_tts)
        if local_genie_url is not None:
            self.local_genie_url = local_genie_url.strip().rstrip("/")
        if use_local_genie is not None:
            self.use_local_genie = bool(use_local_genie)

        if self.colab_url != prev_colab or self.use_colab_tts:
            reset_colab_circuit()
        self._rebuild_llm_client()
        self.invalidate_asset_cache()

        if save_env:
            env_path = BASE_DIR / ".env"
            existing_extra: Dict[str, str] = {}
            if env_path.exists():
                for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                    if "=" in raw_line and not raw_line.strip().startswith("#"):
                        ek, ev = raw_line.split("=", 1)
                        existing_extra[ek.strip()] = ev.strip()
            managed = {
                "LLM_BASE_URL": self.llm_base_url,
                "LLM_API_KEY": self.llm_api_key,
                "LLM_MODEL": self.llm_model,
                "COLAB_URL": self.colab_url,
                "COLAB_TOKEN": self.colab_token,
                "EXT_TTS_MODEL": self.colab_tts_model,
                "USE_COLAB_TTS": "true" if self.use_colab_tts else "false",
                "LOCAL_GENIE_URL": self.local_genie_url,
                "USE_LOCAL_GENIE": "true" if self.use_local_genie else "false",
                "DOUYIN_WS_URL": os.getenv("DOUYIN_WS_URL", "ws://127.0.0.1:8888"),
                "PORT": os.getenv("PORT", "8765"),
            }
            existing_extra.update(managed)
            tmp_path = BASE_DIR / ".env.tmp"
            tmp_path.write_text("\n".join(f"{k}={v}" for k, v in existing_extra.items()) + "\n", encoding="utf-8")
            os.replace(tmp_path, env_path)

    # ==================== 静态资产内存缓存 ====================
    def invalidate_asset_cache(self) -> None:
        self._asset_cache_ts = 0.0

    @staticmethod
    def _has_baked_scene(time_of_day: str) -> bool:
        p = STATIC_SCENE_DIR / f"courtyard_{time_of_day}.jpg"
        return p.exists() and p.stat().st_size > 1000

    def _get_cached_static_meta(self) -> Dict[str, Any]:
        now = time.time()
        if self._asset_cache and (now - self._asset_cache_ts) < 30.0:
            return self._asset_cache

        scenes_dir = BASE_DIR / "static" / "scenes"
        scenes_ready = sorted([f.stem for f in scenes_dir.glob("*.jpg") if f.stat().st_size > 8000]) if scenes_dir.exists() else []
        chibi_dir = BASE_DIR / "static" / "chibi_v7"
        chibis_ready = sorted([f.stem for f in chibi_dir.glob("*.png") if f.stat().st_size > 8000]) if chibi_dir.exists() else []
        chibi_map = {
            pid: f"/static/chibi_v7/{pid}.png" if (chibi_dir / f"{pid}.png").exists() else PERSONAS[pid]["avatar_png"]
            for pid in PERSONA_ORDER
        }
        self._asset_cache = {
            "scenes_v7_ready": scenes_ready,
            "chibi_v7_ready": chibis_ready,
            "chibi_map": chibi_map,
            "baked_scene_day": self._has_baked_scene("day"),
            "baked_scene_night": self._has_baked_scene("night"),
            "has_deepseek_vrm": (BASE_DIR / "static" / "models" / "vrm" / "deepseek.vrm").exists(),
            "voice_bank_counts": get_voice_bank_counts(),
            "public_refs": list_public_refs(),
        }
        self._asset_cache_ts = now
        return self._asset_cache

    # ==================== 兼容保留的辅助清理方法 ====================
    def _return_tool_if_held(self, pid: str) -> None:
        if pid not in self.states:
            return
        st = self.states[pid]
        tool = st.get("held_tool", "")
        if tool in ("net", "watering_can"):
            self.rack_tools[tool] = True
            if self._tool_owners.get(tool) == pid:
                self._tool_owners.pop(tool, None)
        st["held_tool"] = ""
        st["watering_active"] = False

    def _release_seat(self, pid: str) -> None:
        if pid not in self.states:
            return
        self.states[pid]["is_seated"] = False
        self.states[pid]["seat_id"] = ""

    def _is_doing_activity(self, pid: str) -> bool:
        task = self._activity_tasks.get(pid)
        return task is not None and not task.done()

    def _cancel_activity(self, pid: str) -> None:
        task = self._activity_tasks.pop(pid, None)
        if task and not task.done():
            task.cancel()
        self._return_tool_if_held(pid)

    def _cancel_all_activities(self) -> None:
        for pid in list(self._activity_tasks.keys()):
            self._cancel_activity(pid)
        for pid in PERSONA_ORDER:
            self._return_tool_if_held(pid)
        self.rack_tools["net"] = True
        self.rack_tools["watering_can"] = True
        self._tool_owners.clear()

    def trigger_garden_activity(self, pid: str, activity_kind: str) -> Dict[str, Any]:
        """兼容保留旧接口：温和触发角色思考微表情"""
        if pid not in PERSONAS or pid not in self.slots:
            return {"ok": False, "reason": "角色不在场"}
        self.states[pid]["emotion"] = "thinking" if activity_kind == "think" else "happy"
        return {"ok": True, "persona_id": pid, "activity": activity_kind}

    # ==================== 严格 FIFO 小屋休息轮换队列与状态快照 ====================
    def _sync_bench_queue(self, push_back: Optional[List[str]] = None) -> List[str]:
        """维护严格先进先出 (FIFO) 休息队列：退场角色排队尾，上场取队首！"""
        on_stage = set(self.slots)
        cleaned: List[str] = [p for p in self.bench_queue if p in PERSONAS and p not in on_stage]
        if push_back:
            for pb in push_back:
                if pb in PERSONAS and pb not in on_stage:
                    if pb in cleaned:
                        cleaned.remove(pb)
                    cleaned.append(pb)
        for p in PERSONA_ORDER:
            if p not in on_stage and p not in cleaned:
                cleaned.append(p)
        self.bench_queue = cleaned
        return list(self.bench_queue)

    def get_bench(self) -> List[str]:
        return self._sync_bench_queue()

    def _pop_next_bench(self, preferred: Optional[str] = None) -> Optional[str]:
        bench = self._sync_bench_queue()
        if not bench:
            return None
        if preferred and preferred in bench:
            self.bench_queue.remove(preferred)
            return preferred
        return self.bench_queue.pop(0)

    def get_longest_stay_slot(self) -> Tuple[int, str]:
        ranked = [
            (self.states[pid]["stamina"], -self.states[pid]["stay_seconds"], idx, pid)
            for idx, pid in enumerate(self.slots[:3])
        ]
        ranked.sort(key=lambda x: (x[0], x[1]))
        _, _, idx, pid = ranked[0]
        return idx, pid

    def _sync_role_tags(self) -> None:
        is_solo = len(self.slots) == 1
        for pid in PERSONA_ORDER:
            if pid not in self.slots:
                self.states[pid]["role_tag"] = "idle"
            elif self.pk_state.get("active"):
                self.states[pid]["role_tag"] = "pk_debater"
            elif is_solo:
                self.states[pid]["role_tag"] = "solo_host"
            else:
                self.states[pid]["role_tag"] = "idle"

    def snapshot(self) -> Dict[str, Any]:
        meta = self._get_cached_static_meta()
        chibi_map = meta["chibi_map"]
        now = time.time()
        bench_ids = self.get_bench()

        if self.pk_state.get("active"):
            self.pk_state["remaining_sec"] = max(0, int(round(self._pk_end_ts - now)))

        stage_cards = []
        for idx, pid in enumerate(self.slots):
            p = PERSONAS[pid]
            st = self.states[pid]
            stage_cards.append({
                "slot": idx,
                "persona": {
                    "id": p["id"],
                    "nickname": p.get("nickname", NICKNAME_MAP.get(pid, pid)),
                    "internal_label": p["internal_label"],
                    "appearance_alias": p["appearance_alias"],
                    "color": p["color"],
                    "bubble_bg": p["bubble_bg"],
                    "bubble_border": p["bubble_border"],
                    "avatar_png": p["avatar_png"],
                    "chibi_v7_png": chibi_map.get(pid, p["avatar_png"]),
                    "prop_emoji": p["prop_emoji"],
                },
                "state": {
                    "stamina": round(st["stamina"], 1),
                    "love_score": int(st["love_score"]),
                    "stay_seconds": int(st["stay_seconds"]),
                    "emotion": st["emotion"],
                    "pose": st["pose"],
                    "look_target": st["look_target"],
                    "reaction_emoji": st["reaction_emoji"],
                    "activity": st["activity"],
                    "target_x": st["target_x"],
                    "target_y": st["target_y"],
                    "held_tool": st["held_tool"],
                    "is_seated": bool(st["is_seated"]),
                    "seat_id": st["seat_id"],
                    "watering_active": bool(st["watering_active"]),
                    "enter_phase": st.get("enter_phase", "none"),
                    "walk_speed": float(st.get("walk_speed", 96.0)),
                    "stay_remaining": max(0, int(float(st.get("stay_until", 0.0)) - now)),
                    "idle_posture": st.get("idle_posture", "stand"),
                    "idle_expr": st.get("idle_expr", "neutral"),
                    "idle_motion": st.get("idle_motion", ""),
                    "role_tag": st.get("role_tag", "idle"),
                },
            })

        bench_cards = [
            {
                "id": pid,
                "nickname": PERSONAS[pid].get("nickname", NICKNAME_MAP.get(pid, pid)),
                "internal_label": PERSONAS[pid]["internal_label"],
                "appearance_alias": PERSONAS[pid]["appearance_alias"],
                "avatar_png": PERSONAS[pid]["avatar_png"],
                "color": PERSONAS[pid]["color"],
                "prop_emoji": PERSONAS[pid]["prop_emoji"],
                "love_score": int(self.states[pid]["love_score"]),
                "queue_order": i + 1,
            }
            for i, pid in enumerate(bench_ids)
        ]

        desired_mic_muted = self.auto_mute_mic and (not self.human_mic_active)
        return {
            "type": "state_sync",
            "live_status": self.live_status,
            "human_mic_active": self.human_mic_active,
            "auto_mute_mic": self.auto_mute_mic,
            "physical_mic_muted": self.physical_mic_muted,
            "desired_mic_muted": desired_mic_muted,
            "voice_playback_rate": round(self.voice_playback_rate, 2),
            "llm_calls": self.llm_calls,
            "allow_silence": self.allow_silence,
            "random_activities": self.random_activities,
            "fixed_seating": self.fixed_seating,
            "mode": self.mode,
            "desk_prop_mode": self.desk_prop_mode,
            "show_guide_card": self.show_guide_card,
            "guide_title": self.guide_title,
            "guide_items": list(self.guide_items),
            "pk_state": dict(self.pk_state),
            "audience_topic_state": dict(self.audience_topic_state),
            "memory_summary": self.memory_store.get_summary(PERSONA_ORDER),
            "scene_key": "scene_life",
            "scenes_v7_ready": meta["scenes_v7_ready"],
            "chibi_v7_ready": meta["chibi_v7_ready"],
            "time_of_day": self.time_of_day,
            "game_locked": bool(self.pk_state.get("active")),
            "audience_host_id": None,
            "game_players": [],
            "game_paused_for_barrage": False,
            "center_persona_id": self.slots[len(self.slots) // 2] if self.slots else "",
            "party_game": {"active": False, "kind": "", "phase": "", "summary": "", "votes": {}, "vote_arrows": []},
            "topic": self.current_topic,
            "tension": self.tension,
            "llm_ready": bool(self.llm_api_key),
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "colab_url": self.colab_url,
            "colab_tts_model": self.colab_tts_model,
            "use_colab_tts": self.use_colab_tts,
            "local_genie_url": self.local_genie_url,
            "use_local_genie": self.use_local_genie,
            "tts_runtime": get_tts_runtime_status(),
            "baked_scene_day": meta["baked_scene_day"],
            "baked_scene_night": meta["baked_scene_night"],
            "has_deepseek_vrm": meta["has_deepseek_vrm"],
            "voice_bank_counts": meta["voice_bank_counts"],
            "public_refs": meta["public_refs"],
            "rack_tools": dict(self.rack_tools),
            "stools": STOOLS,
            "obstacles": STATIC_OBSTACLES,
            "stage": stage_cards,
            "bench": bench_cards,
            "gomoku": {"size": 9, "board": [], "turn": 1, "move_count": 0, "last_move": None, "winner": 0, "flipped": False, "flipped_by": None},
            "recent_barrage": self.barrage_feed[-6:],
        }

    # ==================== 开播 / 暂停 / 彻底下播 / 真人开麦接管 / 物理麦自动静音 ====================
    async def _sync_physical_mic(self, force_unmute: bool = False) -> Dict[str, Any]:
        """防社死硬件闭麦铁律：
        - 只要开启「非接管自动闭麦」且未开启「真人开麦接管」，无论开播/暂停/下播一律保持物理麦克风硬件静音！
        - 仅在开启真人开麦接管、手动关闭自动闭麦开关、或彻底退出程序 (force_unmute=True) 时解除静音！
        """
        should_mute = (
            (not force_unmute)
            and self.auto_mute_mic
            and (not self.human_mic_active)
        )
        res = await async_set_mic_mute(should_mute)
        if res.get("ok"):
            self.physical_mic_muted = bool(res.get("muted", should_mute))
        else:
            st = await async_get_mic_mute()
            if st.get("ok"):
                self.physical_mic_muted = bool(st.get("muted", False))
        return res

    async def set_auto_mute_mic(self, enabled: bool) -> Dict[str, Any]:
        self.auto_mute_mic = bool(enabled)
        res = await self._sync_physical_mic(force_unmute=(not self.auto_mute_mic))
        await self.broadcast(self.snapshot())
        return {
            "ok": True,
            "auto_mute_mic": self.auto_mute_mic,
            "physical_mic_muted": self.physical_mic_muted,
            "mic_result": res,
        }

    def _clear_priority_queue(self) -> None:
        while not self.priority_queue.empty():
            try:
                self.priority_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def set_live_status(self, status: str) -> None:
        if status not in ("live", "paused", "stopped"):
            return
        self.live_status = status
        if status == "live":
            self._last_interaction_ts = time.time()
            self._wake_event.set()
        elif status == "paused":
            self._cancel_prefetch()
            await self.broadcast({"type": "live_stopped"})
        elif status == "stopped":
            self._pk_stop_flag = True
            self.pk_state.update({
                "active": False, "topic": "", "red_id": "", "blue_id": "",
                "red_stance": "", "blue_stance": "", "red_votes": 0, "blue_votes": 0, "remaining_sec": 0,
            })
            self.audience_topic_state.update({"active": False, "user": "", "topic": "", "step": 0})
            self.human_mic_active = False
            self._cancel_prefetch()
            self._clear_priority_queue()
            self._cancel_all_activities()
            self.time_of_day = "day"
            self.mode = "solo" if len(self.slots) == 1 else "life"
            self._sync_role_tags()
            await self.broadcast({"type": "live_stopped"})
        # 关键防社死修复：点「下播」不自动解除物理麦静音（防止抖音直播伴侣还没关导致主播杂音泄露！）
        await self._sync_physical_mic(force_unmute=False)
        await self.broadcast(self.snapshot())

    async def set_human_mic_takeover(self, active: bool) -> Dict[str, Any]:
        """P0 真人开麦接管：开启时解除物理麦静音+AI安静让麦；关闭时若在直播中自动硬件闭麦防收杂音！"""
        self.human_mic_active = bool(active)
        if self.human_mic_active:
            self._cancel_prefetch()
            for pid in self.slots:
                self.states[pid]["emotion"] = "listening"
                self.states[pid]["pose"] = "idle"
                self.states[pid]["reaction_emoji"] = "🎙️"
            await self.broadcast({"type": "live_stopped"})
        else:
            for pid in self.slots:
                self.states[pid]["emotion"] = "happy"
                self.states[pid]["reaction_emoji"] = ""
            self._last_interaction_ts = time.time()
            self._wake_event.set()
        mic_res = await self._sync_physical_mic(force_unmute=self.human_mic_active)
        await self.broadcast(self.snapshot())
        return {
            "ok": True,
            "human_mic_active": self.human_mic_active,
            "physical_mic_muted": self.physical_mic_muted,
            "mic_result": mic_res,
        }

    async def set_stage_count(
        self,
        count: int,
        lead_persona: Optional[str] = None,
        roster: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """一键调整场上人数（PK 进行中锁死 2 人禁止破坏 PK 现场）"""
        if self.pk_state.get("active"):
            return {"ok": False, "reason": "红蓝 PK 进行中，场上固定 2 位 AI，请先结束 PK"}

        target = max(1, min(3, int(count)))
        self.time_of_day = "day"
        pushed_back: List[str] = []

        if roster:
            valid = []
            for pid in roster:
                if pid in PERSONAS and pid not in valid:
                    valid.append(pid)
            if valid:
                for old in list(self.slots):
                    if old not in valid[:target]:
                        self._cancel_activity(old)
                        self._release_seat(old)
                        self.states[old]["stamina"] = 100.0
                        pushed_back.append(old)
                self.slots = valid[:target]
                for inc in self.slots:
                    self.states[inc]["stamina"] = max(65.0, float(self.states[inc].get("stamina", 100.0)))
                    self.states[inc]["emotion"] = "happy"

        if lead_persona and lead_persona in PERSONAS and not roster:
            if lead_persona in self.slots:
                self.slots.remove(lead_persona)
            self.slots.insert(0, lead_persona)

        while len(self.slots) > target:
            out = self.slots.pop()
            self._cancel_activity(out)
            self._release_seat(out)
            self.states[out]["stamina"] = 100.0
            pushed_back.append(out)

        self._sync_bench_queue(push_back=pushed_back)
        while len(self.slots) < target:
            inc = self._pop_next_bench()
            if not inc:
                break
            self.slots.append(inc)
            self.states[inc]["stamina"] = 100.0
            self.states[inc]["emotion"] = "happy"

        self._sync_bench_queue()
        self.mode = "solo" if len(self.slots) == 1 else "life"
        self._solo_idle_since = time.time()
        self._sync_role_tags()
        await self.broadcast(self.snapshot())
        return {"ok": True, "mode": self.mode, "slots": list(self.slots), "bench": self.get_bench()}

    async def return_to_life_mode(self) -> None:
        for pid in self.slots:
            self._release_seat(pid)
            self.states[pid]["emotion"] = "idle"
            self.states[pid]["pose"] = "idle"
        self.mode = "solo" if len(self.slots) == 1 else "life"
        self._sync_role_tags()
        await self.broadcast(self.snapshot())

    # ==================== 引擎生命周期与 P0~P5 严格优先级队列 ====================
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # 启动即按自动闭麦规则锁定物理麦，防止未点开播前直播伴侣误收环境杂音！
        try:
            await self._sync_physical_mic(force_unmute=False)
        except Exception:
            pass
        self._tasks = [
            asyncio.create_task(self._garden_life_ticker()),
            asyncio.create_task(self._director_main_loop()),
        ]

    async def stop(self) -> None:
        self._running = False
        self._pk_stop_flag = True
        self._cancel_prefetch()
        self._cancel_all_activities()
        # 退出服务时强制解除物理麦静音，避免影响用户平时语音通话
        try:
            await async_set_mic_mute(False)
            self.physical_mic_muted = False
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._llm_http_client is not None and not self._llm_http_client.is_closed:
            try:
                await self._llm_http_client.aclose()
            except Exception:
                pass
            self._llm_http_client = None

    async def enqueue_event(self, event: Dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        if self.live_status == "stopped" and kind != "manual_speak":
            return

        # 严格 P0 ~ P5 优先级表
        prio_map = {
            "manual_speak": 0,      # P0: 导播/真人紧急插麦口播（最高优先级）
            "heart_summon": 1,      # P1: 1个小心心强制替换角色上场并感谢
            "gift_thanks": 1,       # P1: 在场角色收到礼物专属感谢
            "pk_session": 2,        # P2: 2人红蓝 PK 辩论实时投票专场
            "audience_topic": 3,    # P3: 观众出题 3人从左到右吐槽后第3人消失
            "familiar_greet": 3,    # P3: 老熟人进场（场上有认识他的AI从左到右依次欢迎）
            "familiar_barrage": 3,  # P3: 老熟人弹幕（场上有认识他的AI从左到右依次回应）
            "audience_chat": 4,     # P4: 常规点名/独播弹幕互动
            "targeted_roast": 4,    # P4: 双人互怼
            "swap_character": 4,    # P4: 精力耗尽 FIFO 轮换出场
            "leave_only": 4,        # P4: 退场休息
            "start_game": 4,
        }
        prio = prio_map.get(kind, 4)
        if prio <= 3:
            self._cancel_prefetch()
        if self.priority_queue.full():
            try:
                _ = self.priority_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self.priority_queue.put((prio, next(self._prio_seq), event))
        self._wake_event.set()

    def _has_urgent_p0_interrupt(self) -> bool:
        """检查是否有 P0 插麦、真人开麦接管或下播信号到达，用于多轮长事件间隙礼貌让路"""
        if self.live_status != "live" or self.human_mic_active:
            return True
        # 查看队首是否是 P0 (prio == 0)
        try:
            q_internal = getattr(self.priority_queue, "_queue", None)
            if q_internal and q_internal[0][0] == 0:
                return True
        except Exception:
            pass
        return False

    def _cancel_prefetch(self) -> None:
        self._prefetched_chat = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None

    async def manual_speak(self, persona_id: str, text: str, emotion: str = "happy") -> Dict[str, Any]:
        """P0 最高优先级插麦口播（0 LLM 消耗，即使在真人开麦或暂停态也可立即开口回应！）"""
        pid = persona_id if persona_id in self.slots else (self.slots[0] if self.slots else "deepseek")
        clean = sanitize_ai_speech(text, anonymize_names=True)
        if not clean:
            return {"ok": False, "reason": "口播内容为空"}
        if self.live_status == "live" and not self.human_mic_active and not self.pk_state.get("active"):
            await self.enqueue_event({"kind": "manual_speak", "speaker": pid, "text": clean, "emotion": emotion or "happy"})
        else:
            async def _say_now() -> None:
                async with self.speaking_lock:
                    await self._speak(pid, clean, emotion=emotion or "happy", look_at="", pose="talking")
            asyncio.create_task(_say_now())
        return {"ok": True, "speaker": pid, "text": clean}

    # ==================== 红蓝 PK 辩论实时投票控制（功能 2） ====================
    async def start_pk(
        self,
        topic: str = "",
        red_stance: str = "",
        blue_stance: str = "",
        duration_sec: int = 60,
        auto_triggered: bool = False,
    ) -> Dict[str, Any]:
        if self.pk_state.get("active"):
            return {"ok": True, "already_active": True, "pk_state": dict(self.pk_state)}

        preset = PK_DEBATE_TOPICS[self._pk_topic_cursor % len(PK_DEBATE_TOPICS)]
        self._pk_topic_cursor += 1
        final_topic = topic.strip()[:42] if topic.strip() else preset["topic"]
        final_red = red_stance.strip()[:18] if red_stance.strip() else preset["red_stance"]
        final_blue = blue_stance.strip()[:18] if blue_stance.strip() else preset["blue_stance"]
        dur = max(25, min(180, int(duration_sec or 60)))

        was_not_live = (self.live_status != "live")
        if was_not_live:
            self.live_status = "live"
            self._last_interaction_ts = time.time()

        self._pk_stop_flag = False
        self._pk_voter_cd.clear()
        self._cancel_prefetch()
        await self.enqueue_event({
            "kind": "pk_session",
            "topic": final_topic,
            "red_stance": final_red,
            "blue_stance": final_blue,
            "duration_sec": dur,
            "auto_triggered": auto_triggered,
        })
        if was_not_live:
            await self.broadcast(self.snapshot())
        return {"ok": True, "queued": True, "topic": final_topic, "duration_sec": dur}

    async def stop_pk(self) -> Dict[str, Any]:
        self._pk_stop_flag = True
        if self.pk_state.get("active"):
            self.pk_state["active"] = False
            self.pk_state["remaining_sec"] = 0
            self._last_pk_end_ts = time.time()
            self.mode = "solo" if len(self.slots) == 1 else "life"
            self._sync_role_tags()
            await self.broadcast(self.snapshot())
        self._wake_event.set()
        return {"ok": True, "pk_state": dict(self.pk_state)}

    def _try_record_pk_vote(self, user: str, clean_text: str) -> Optional[str]:
        """在 PK 进行中解析弹幕 1 / 2 实时计票（对其他非投票内容严格返回 None）"""
        if not self.pk_state.get("active"):
            return None
        t = clean_text.strip().lower()
        red_id = self.pk_state.get("red_id", "")
        blue_id = self.pk_state.get("blue_id", "")
        red_nick = NICKNAME_MAP.get(red_id, "").lower()
        blue_nick = NICKNAME_MAP.get(blue_id, "").lower()

        side: Optional[str] = None
        if t in ("1", "红", "红方", "投1", "选1", "支持1", "支持红方", "11", "111"):
            side = "red"
        elif t in ("2", "蓝", "蓝方", "投2", "选2", "支持2", "支持蓝方", "22", "222"):
            side = "blue"
        elif red_nick and red_nick in t and (not blue_nick or blue_nick not in t):
            side = "red"
        elif blue_nick and blue_nick in t and (not red_nick or red_nick not in t):
            side = "blue"

        if not side:
            return None

        now = time.time()
        vkey = f"{user[:12]}:{side}"
        if now - self._pk_voter_cd.get(vkey, 0.0) < 1.2:
            return side
        self._pk_voter_cd[vkey] = now

        if side == "red":
            self.pk_state["red_votes"] = int(self.pk_state.get("red_votes", 0)) + 1
        else:
            self.pk_state["blue_votes"] = int(self.pk_state.get("blue_votes", 0)) + 1
        self._last_interaction_ts = now
        return side

    async def _run_pk_session(self, event: Dict[str, Any]) -> None:
        """执行 2 人红蓝 PK 辩论：固定 2 个 AI 在场、冻结精力时间、屏蔽无关互动、实时播报票数！"""
        if self.human_mic_active or self.live_status != "live":
            return

        # 1. 严格固定场上为 2 位 AI（3人则退第3人回休息队尾；1人则从休息队首补第2人上台）
        while len(self.slots) > 2:
            out_id = self.slots.pop()
            self._cancel_activity(out_id)
            self._release_seat(out_id)
            self.states[out_id]["stamina"] = 100.0
            self._sync_bench_queue(push_back=[out_id])
            await self.broadcast({"type": "stage_exit", "outgoing": out_id, "reason": "pk_prepare", "fast": True})
            await self.broadcast(self.snapshot())
            await asyncio.sleep(0.35)

        while len(self.slots) < 2:
            inc = self._pop_next_bench()
            if not inc:
                break
            self.slots.append(inc)
            self.states[inc].update({"stamina": 100.0, "stay_seconds": 0.0, "entered_at": time.time(), "emotion": "excited"})
            await self.broadcast({"type": "stage_enter", "incoming": inc, "color": PERSONAS[inc]["color"]})
            await self.broadcast(self.snapshot())
            await asyncio.sleep(0.55)

        if len(self.slots) < 2:
            return

        red_id, blue_id = self.slots[0], self.slots[1]
        red_nick = NICKNAME_MAP.get(red_id, red_id)
        blue_nick = NICKNAME_MAP.get(blue_id, blue_id)
        topic = event.get("topic") or PK_DEBATE_TOPICS[0]["topic"]
        red_stance = event.get("red_stance") or "正方观点"
        blue_stance = event.get("blue_stance") or "反方观点"
        dur = int(event.get("duration_sec", 60))

        now = time.time()
        self._pk_end_ts = now + dur
        self._pk_stop_flag = False
        self.mode = "pk"
        self.audience_topic_state.update({"active": False, "user": "", "topic": "", "step": 0})
        self.pk_state.update({
            "active": True,
            "topic": topic,
            "red_id": red_id,
            "blue_id": blue_id,
            "red_stance": red_stance,
            "blue_stance": blue_stance,
            "red_votes": 0,
            "blue_votes": 0,
            "remaining_sec": dur,
        })
        self._sync_role_tags()
        await self.broadcast(self.snapshot())

        try:
            turn_idx = 0
            while not self._pk_stop_flag and time.time() < self._pk_end_ts and not self._has_urgent_p0_interrupt():
                is_red = (turn_idx % 2 == 0)
                spk = red_id if is_red else blue_id
                opp = blue_id if is_red else red_id
                spk_nick = red_nick if is_red else blue_nick
                opp_nick = blue_nick if is_red else red_nick
                my_stance = red_stance if is_red else blue_stance
                opp_stance = blue_stance if is_red else red_stance
                vote_num = "1" if is_red else "2"
                rv = int(self.pk_state["red_votes"])
                bv = int(self.pk_state["blue_votes"])

                if turn_idx == 0:
                    inst = (
                        f"现在开启红蓝PK辩论《{topic}》！你是红方【{spk_nick}】，坚定支持『{my_stance}』，对手是蓝方【{opp_nick}】。"
                        f"请元气满满亮出观点并喊观众在弹幕扣 {vote_num} 支持你（严禁提礼物消费）！"
                    )
                    fb = f"红蓝PK开战啦！{spk_nick}坚定站『{my_stance}』，认同的小伙伴快在弹幕扣1支持我呀！"
                elif turn_idx == 1:
                    inst = (
                        f"红蓝PK辩论《{topic}》！你是蓝方【{spk_nick}】，坚定支持『{my_stance}』，不服红方【{opp_nick}】的『{opp_stance}』。"
                        f"请奶凶俏皮反驳她，并喊观众在弹幕扣 {vote_num} 投给你（严禁提礼物消费）！"
                    )
                    fb = f"哼，{opp_nick}说得才不对呢！明明『{my_stance}』最棒，快在弹幕扣2给{spk_nick}投票呀！"
                else:
                    score_tip = f"当前实时票数：红方{rv}票 vs 蓝方{bv}票。"
                    inst = (
                        f"PK激战中《{topic}》！{score_tip}你是【{spk_nick}】（支持『{my_stance}』），请针对【{opp_nick}】刚才的话趣味反击拉票，让观众扣{vote_num}！"
                    )
                    fb = f"现在比分{rv}比{bv}啦！站『{my_stance}』的家人们快扣{vote_num}帮{spk_nick}冲一波票数呀！"

                t = await self._generate_turn(spk, inst, fb, "excited" if is_red else "smug", False, opp)
                if self._pk_stop_flag or self.live_status != "live" or self.human_mic_active:
                    break
                await self._speak(spk, t["speech"], t["emotion"], {opp: "pout"}, look_at=opp, pose="talking")
                turn_idx += 1
                await asyncio.sleep(0.25)

            # 若正常倒计时结束且未被下播/开麦打断，播报精彩胜负结算！
            if not self._pk_stop_flag and self.live_status == "live" and not self.human_mic_active:
                rv = int(self.pk_state["red_votes"])
                bv = int(self.pk_state["blue_votes"])
                if rv > bv:
                    win_id, win_nick, lose_nick = red_id, red_nick, blue_nick
                    summary_fb = f"耶！最终{rv}比{bv}，红方{win_nick}拿下胜利啦，谢谢大家扣1支持我！"
                elif bv > rv:
                    win_id, win_nick, lose_nick = blue_id, blue_nick, red_nick
                    summary_fb = f"好耶！最终{bv}比{rv}，蓝方{win_nick}逆袭获胜咯，谢谢大家扣2投票！"
                else:
                    win_id, win_nick, lose_nick = red_id, red_nick, blue_nick
                    summary_fb = f"哇塞竟然{rv}比{bv}打成平手啦！看来我和{lose_nick}说得都很有道理嘛！"
                await self._speak(win_id, summary_fb, "happy", { (blue_id if win_id == red_id else red_id): "shy" }, look_at="", pose="happy_hop")
        finally:
            self.pk_state.update({
                "active": False, "remaining_sec": 0,
            })
            self._last_pk_end_ts = time.time()
            self._last_interaction_ts = time.time()
            self.mode = "solo" if len(self.slots) == 1 else "life"
            self._sync_role_tags()
            await self.broadcast(self.snapshot())

    # ==================== 观众出题 3 人从左到右吐槽 + 第 3 人消失（功能 3） ====================
    async def trigger_audience_topic(self, user: str, topic: str, force: bool = True) -> Dict[str, Any]:
        ok, clean_topic = sanitize_barrage(topic)
        if not ok or not clean_topic:
            return {"ok": False, "reason": "题目为空或包含不合规内容"}
        if self.pk_state.get("active"):
            return {"ok": False, "reason": "当前正在进行红蓝 PK，暂不接收出题"}

        clean_user = (user or "热心观众").strip()[:14] or "热心观众"
        if not force:
            familiar = self.memory_store.get_familiar_on_stage(clean_user, self.slots)
            if not familiar:
                return {"ok": False, "reason": "not_familiar_yet"}

        was_not_live = (self.live_status != "live")
        if was_not_live:
            self.live_status = "live"
            self._last_interaction_ts = time.time()

        await self.enqueue_event({
            "kind": "audience_topic",
            "user": clean_user,
            "topic": clean_topic[:36],
        })
        if was_not_live:
            await self.broadcast(self.snapshot())
        return {"ok": True, "user": clean_user, "topic": clean_topic[:36]}

    async def _run_audience_topic_session(self, user: str, topic: str) -> None:
        """挂起观众出题横幅 -> 凑齐 3 位 AI -> 严格从左到右 (0->1->2) 依次吐槽 -> 第 3 位 AI 自动消失退场！"""
        if self.pk_state.get("active") or self.human_mic_active or self.live_status != "live":
            return

        self.audience_topic_state = {"active": True, "user": user, "topic": topic, "step": 0}
        await self.broadcast(self.snapshot())

        try:
            # 1. 不足 3 人则按 FIFO 休息队列依次补齐至 3 人登场
            while len(self.slots) < 3:
                inc = self._pop_next_bench()
                if not inc:
                    break
                self.slots.append(inc)
                self.states[inc].update({
                    "stamina": 100.0, "stay_seconds": 0.0, "entered_at": time.time(), "emotion": "excited",
                })
                self.mode = "life"
                self._sync_role_tags()
                await self.broadcast({"type": "stage_enter", "incoming": inc, "color": PERSONAS[inc]["color"]})
                await self.broadcast(self.snapshot())
                await asyncio.sleep(0.55)

            speakers_ltr = list(self.slots[:3])

            # 2. 严格从左到右（slot 0 -> slot 1 -> slot 2）零空窗流水线依次吐槽！
            specs: List[Dict[str, Any]] = []
            for idx, sp_id in enumerate(speakers_ltr):
                sp_nick = NICKNAME_MAP.get(sp_id, sp_id)
                knows = self.memory_store.knows_user(sp_id, user)
                order_label = ("左边第一位率先开麦", "中间第二位接力", "右边第三位压轴总结")[min(idx, 2)]
                inst = (
                    f"观众【{user}】挂条幅出题：《{topic}》！现在3人从左到右依次吐槽，你是【{sp_nick}】（{order_label}）。"
                    f"请完全结合你自己的独特人设和口癖生动风趣地吐槽这个题目，一针见血超有梗！"
                )
                fb = f"哇，{user}出的《{topic}》太会整活啦，依{sp_nick}看这事儿简直笑点拉满！"
                specs.append({
                    "speaker_id": sp_id,
                    "instruction": inst,
                    "fallback_speech": fb,
                    "fallback_emotion": "smug" if idx == 1 else "happy",
                    "look_at_id": "",
                    "familiar_user": user if knows else "",
                    "event_tag": "audience_topic",
                })

            async def _on_step(step_no: int) -> None:
                self.audience_topic_state["step"] = step_no
                await self.broadcast(self.snapshot())

            await self._run_sequence_pipeline(specs, step_cb=_on_step)

            # 3. 3 人从左到右全部吐槽完毕后，第 3 个 AI（最右侧 slot 2）优雅道别并消失退回休息队列！
            if len(self.slots) >= 3 and self.live_status == "live" and not self.pk_state.get("active"):
                await self._perform_leave_only(2, reason="audience_topic_done")
        finally:
            self.audience_topic_state = {"active": False, "user": "", "topic": "", "step": 0}
            await self.broadcast(self.snapshot())

    # ==================== 小心心记忆绑定、观众进场与弹幕分发（功能 1 & 4） ====================
    @staticmethod
    def _match_instant_keyword_reaction(text: str) -> Optional[Tuple[str, str, str]]:
        rules = (
            (("比心", "爱你", "喜欢你", "贴贴", "好可爱", "萌死了", "抱抱"), ("love", "hop", "💗")),
            (("哈哈", "笑死", "233", "好好笑", "绝了", "太逗"), ("happy", "dance", "🎵")),
            (("胖鱼", "肥鱼", "小笨蛋", "哼", "气死", "坏蛋", "菜"), ("angry", "shake", "💢")),
            (("加油", "冲冲冲", "打call", "太棒", "厉害", "牛", "666"), ("excited", "wave", "✨")),
            (("晚安", "好困", "睡觉", "辛苦", "早点睡"), ("shy", "bow", "🌙")),
            (("问个问题", "为什么", "怎么", "?", "？"), ("thinking", "nod", "💭")),
        )
        for kws, action in rules:
            if any(k in text for k in kws):
                return action
        return None

    def _find_persona_in_text(self, text: str) -> List[str]:
        low = text.lower()
        return [pid for pid, p in PERSONAS.items() if any(k in low for k in p["keywords"])]

    async def handle_audience_enter(self, user: str) -> Dict[str, Any]:
        """
        观众进入直播间事件：
        严格遵循「他来的时候场上有认识他的 AI 主动互动，认识他的 AI 多的时候从左到右依次说话」！
        若场上没有认识他的 AI，绝不乱打招呼刷屏。
        """
        clean_user = (user or "").strip()[:16]
        if not clean_user:
            return {"ok": False, "triggered": False}

        if self.pk_state.get("active") or self.human_mic_active:
            return {"ok": True, "triggered": False, "reason": "pk_or_mic_locked"}

        familiar = self.memory_store.get_familiar_on_stage(clean_user, self.slots)
        if not familiar:
            return {"ok": True, "triggered": False, "reason": "no_familiar_on_stage"}

        now = time.time()
        ukey = clean_user.lower()
        if now - self._recent_enter_cd.get(ukey, 0.0) < 18.0:
            return {"ok": True, "triggered": False, "reason": "enter_cooldown"}
        self._recent_enter_cd[ukey] = now
        self._last_interaction_ts = now

        if self.live_status == "live":
            await self.enqueue_event({"kind": "familiar_greet", "user": clean_user})
        return {"ok": True, "triggered": True, "familiar_order": familiar}

    async def handle_heart_gift(
        self,
        user: str,
        count: int = 1,
        preferred_target: str = "",
        gift_name: str = "小心心",
        force_replace: bool = True,
    ) -> Dict[str, Any]:
        """
        1 个小心心强制替换 1 个角色上场 + 永久写入该角色的独立记忆库！
        （若在 PK 期间送出：后台静默写入记忆库不丢心意，但绝不打断 PK 换人或反应！）
        """
        clean_user = (user or "星光老粉").strip()[:16] or "星光老粉"
        hearts = max(1, int(count or 1))
        now = time.time()
        self._last_interaction_ts = now

        # PK 期间严格不对任何非投票互动行为做台面反应，仅静默保存记忆
        if self.pk_state.get("active"):
            quiet_pid = preferred_target if preferred_target in PERSONAS else (self.slots[0] if self.slots else "deepseek")
            self.memory_store.record_heart_gift(quiet_pid, clean_user, hearts_delta=hearts, gift_name=gift_name)
            await self.broadcast(self.snapshot())
            return {"ok": True, "action": "recorded_silently_during_pk", "persona_id": quiet_pid}

        # 若明确指定给台上某位角色补小心心且不要求强换下场
        if not force_replace and preferred_target in self.slots:
            self.memory_store.record_heart_gift(preferred_target, clean_user, hearts_delta=hearts, gift_name=gift_name)
            self.states[preferred_target]["stamina"] = min(100.0, self.states[preferred_target]["stamina"] + 35.0)
            self.states[preferred_target]["love_score"] += hearts * 25
            await self.broadcast({"type": "gift_boost", "target": preferred_target, "user": clean_user, "gift_name": gift_name})
            if self.live_status == "live":
                await self.enqueue_event({"kind": "gift_thanks", "speaker": preferred_target, "user": clean_user, "gift_name": gift_name})
            await self.broadcast(self.snapshot())
            return {"ok": True, "action": "loved_on_stage", "persona_id": preferred_target}

        # 选择要强制替换上场的休息区角色：指定休息角色 > 该观众25s内刚点名的休息角色 > 严格 FIFO 休息队首（下一位）！
        bench = self.get_bench()
        incoming_pid = ""
        if preferred_target and preferred_target in bench:
            incoming_pid = preferred_target
        else:
            last_m = self._user_last_mentioned_bench.get(clean_user.lower())
            if last_m and (now - last_m[1] <= 25.0) and last_m[0] in bench:
                incoming_pid = last_m[0]
            elif bench:
                incoming_pid = bench[0]
            elif self.slots:
                incoming_pid = self.slots[0]

        if not incoming_pid:
            return {"ok": False, "reason": "no_available_persona"}

        # 立即写入被召唤角色的专属记忆库！
        self.memory_store.record_heart_gift(incoming_pid, clean_user, hearts_delta=hearts, gift_name=gift_name)
        self.states[incoming_pid]["love_score"] += hearts * 25

        if self.live_status != "live":
            self.live_status = "live"
            self._last_interaction_ts = time.time()

        if incoming_pid in self.slots:
            await self.enqueue_event({"kind": "gift_thanks", "speaker": incoming_pid, "user": clean_user, "gift_name": gift_name})
            await self.broadcast({"type": "gift_boost", "target": incoming_pid, "user": clean_user, "gift_name": gift_name})
            await self.broadcast(self.snapshot())
            return {"ok": True, "action": "loved_on_stage", "persona_id": incoming_pid}

        await self.enqueue_event({
            "kind": "heart_summon",
            "incoming": incoming_pid,
            "user": clean_user,
            "gift_name": gift_name,
            "hearts": hearts,
        })
        await self.broadcast(self.snapshot())
        return {"ok": True, "action": "heart_force_swap_queued", "incoming": incoming_pid, "user": clean_user}

    async def handle_audience_barrage(self, user: str, text: str, source: str = "弹幕") -> Dict[str, Any]:
        ok, clean_text = sanitize_barrage(text)
        if not ok:
            return {"accepted": False, "reason": "blocked_by_safety"}

        now = time.time()
        clean_user = (user or "抖友").strip()[:14] or "抖友"

        # ==================== 1. PK 期间最高优拦截：只记 1 / 2 投票，不对任何其他互动做反应！ ====================
        if self.pk_state.get("active"):
            voted_side = self._try_record_pk_vote(clean_user, clean_text)
            if voted_side:
                item = {"user": clean_user, "text": clean_text, "ts": int(now)}
                self.barrage_feed.append(item)
                self.barrage_feed = self.barrage_feed[-25:]
                await self.broadcast({"type": "barrage", "user": clean_user, "text": f"{clean_text}（已计票）", "source": source})
                await self.broadcast(self.snapshot())
                return {
                    "accepted": True,
                    "pk_vote": voted_side,
                    "red_votes": self.pk_state["red_votes"],
                    "blue_votes": self.pk_state["blue_votes"],
                }
            return {"accepted": True, "picked_for_reply": False, "pk_ignored": True}

        # 短时间重复弹幕去重
        if len(self._recent_barrage_dedup) > 60:
            self._recent_barrage_dedup = {k: ts for k, ts in self._recent_barrage_dedup.items() if now - ts < 10.0}
        dedup_key = f"{clean_user}:{clean_text}"
        if now - self._recent_barrage_dedup.get(dedup_key, 0.0) < 2.5:
            return {"accepted": True, "picked_for_reply": False, "deduplicated": True}
        self._recent_barrage_dedup[dedup_key] = now

        self._recent_barrage_times = [ts for ts in self._recent_barrage_times if now - ts <= 10.0]
        self._recent_barrage_times.append(now)
        self._last_barrage_seen_ts = now
        self._last_interaction_ts = now
        self._solo_idle_since = now

        item = {"user": clean_user, "text": clean_text, "ts": int(now)}
        self.barrage_feed.append(item)
        if len(self.barrage_feed) > 25:
            self.barrage_feed = self.barrage_feed[-25:]
        await self.broadcast({"type": "barrage", "user": clean_user, "text": clean_text, "source": source})

        # 若真人开麦接管中，仅上屏弹幕，AI 静音倾听绝不抢话
        if self.human_mic_active:
            return {"accepted": True, "picked_for_reply": False, "human_mic_active": True}

        mentioned = self._find_persona_in_text(clean_text)
        bench_now = set(self.get_bench())
        for m in mentioned:
            if m in bench_now:
                self._user_last_mentioned_bench[clean_user.lower()] = (m, now)
                break

        # 查询场上从左到右有哪些 AI 认识这位老熟人观众！
        familiar_ltr = self.memory_store.get_familiar_on_stage(clean_user, self.slots)

        # ==================== 2. 弹幕「出题：xxx」识别（老熟人专属触发 3 人从左到右吐槽！） ====================
        m_topic = _RE_AUDIENCE_TOPIC.match(clean_text)
        if m_topic:
            topic_body = m_topic.group(1).strip()
            if topic_body and familiar_ltr and self.live_status == "live":
                await self.trigger_audience_topic(clean_user, topic_body, force=True)
                return {"accepted": True, "picked_for_reply": True, "audience_topic_triggered": True}
            elif topic_body and not familiar_ltr:
                return {"accepted": True, "picked_for_reply": False, "reason": "need_familiar_ai_for_topic"}

        # ==================== 3. 老熟人弹幕：场上有认识他的 AI -> 按从左到右顺序依次热情回应！ ====================
        kw_hit = self._match_instant_keyword_reaction(clean_text)
        if familiar_ltr:
            for fpid in familiar_ltr:
                self.memory_store.update_last_note(fpid, clean_user, clean_text)
                await self.broadcast({
                    "type": "barrage_ack",
                    "target": fpid,
                    "user": clean_user,
                    "emotion": kw_hit[0] if kw_hit else "love",
                    "motion": kw_hit[1] if kw_hit else "wave",
                    "badge": "💖",
                })
            if self.live_status == "live" and (now - self._last_barrage_reply_ts) >= 1.0:
                self._last_barrage_reply_ts = now
                await self.enqueue_event({
                    "kind": "familiar_barrage",
                    "user": clean_user,
                    "text": clean_text,
                    "mentioned": mentioned,
                })
                return {"accepted": True, "picked_for_reply": True, "familiar_order": familiar_ltr}
            return {"accepted": True, "picked_for_reply": False, "familiar_order": familiar_ltr}

        # ==================== 4. 普通未送小心心观众：仅在 1 人独播或点名时适度抽读，中间角色回应点名 ====================
        center_pid = self.slots[len(self.slots) // 2] if self.slots else "deepseek"
        target_pid = self.slots[0] if len(self.slots) == 1 else (center_pid if mentioned else (self.slots[0] if self.slots else "deepseek"))
        if kw_hit or mentioned:
            emo_k, mot_k, badge_k = kw_hit if kw_hit else ("listening", "nod", "💬")
            await self.broadcast({
                "type": "barrage_ack",
                "target": target_pid,
                "user": clean_user,
                "emotion": emo_k,
                "motion": mot_k,
                "badge": badge_k,
            })

        picked = False
        if self.live_status == "live" and (len(self.slots) == 1 or mentioned):
            cooldown_ok = (now - self._last_barrage_reply_ts) >= 3.2
            prob = 0.80 if len(self.slots) == 1 else 0.75
            if cooldown_ok and random.random() <= prob:
                picked = True
                self._last_barrage_reply_ts = now
                await self.enqueue_event({
                    "kind": "audience_chat",
                    "user": clean_user,
                    "text": clean_text,
                    "mentioned": mentioned,
                })
        return {"accepted": True, "picked_for_reply": picked}

    async def boost_character(
        self,
        pid: str,
        power: int,
        user: str = "星光老粉",
        gift_name: str = "小心心",
        silent: bool = False,
        is_real_gift: bool = True,
    ) -> Dict[str, Any]:
        """兼容中控台与抖音桥接礼物入口：若为小心心等真实礼物，自动写入该角色专属记忆库！"""
        if pid not in PERSONAS:
            return {"ok": False}
        if is_real_gift and "小心心" in gift_name:
            # 若目标在休息区则强制替换上场；若目标已在台上则写记忆并感谢
            return await self.handle_heart_gift(
                user=user,
                count=max(1, power // 25),
                preferred_target=pid,
                gift_name=gift_name,
                force_replace=(pid not in self.slots),
            )

        if self.pk_state.get("active"):
            if is_real_gift:
                self.memory_store.record_heart_gift(pid, user, hearts_delta=1, gift_name=gift_name)
                await self.broadcast(self.snapshot())
            return {"ok": True, "action": "pk_locked"}

        st = self.states[pid]
        st["love_score"] += power
        if is_real_gift:
            self.memory_store.record_heart_gift(pid, user, hearts_delta=1, gift_name=gift_name)
            self._last_interaction_ts = time.time()

        if pid in self.slots:
            st["stamina"] = min(100.0, st["stamina"] + min(45.0, power * 0.9))
            if not silent:
                await self.broadcast({"type": "gift_boost", "target": pid, "user": user, "gift_name": gift_name})
                if is_real_gift and self.live_status == "live" and power >= 10:
                    await self.enqueue_event({"kind": "gift_thanks", "speaker": pid, "user": user, "gift_name": gift_name})
            await self.broadcast(self.snapshot())
            return {"ok": True, "action": "loved_on_stage"}

        if is_real_gift and power >= 10:
            if self.live_status != "live":
                await self.set_live_status("live")
            await self.enqueue_event({
                "kind": "heart_summon",
                "incoming": pid,
                "user": user,
                "gift_name": gift_name,
                "hearts": 1,
            })
            await self.broadcast(self.snapshot())
            return {"ok": True, "action": "invite_queued"}

        await self.broadcast(self.snapshot())
        return {"ok": True, "action": "bench_cheered"}

    # ==================== 大模型自然口语生成（严格互称外号） ====================
    @staticmethod
    def _extract_json_dict(raw: str) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = re.sub(r",\s*([}\]])", r"\1", raw[start : end + 1])
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    async def _generate_turn(
        self,
        speaker_id: str,
        instruction: str,
        fallback_speech: str,
        fallback_emotion: str = "talking",
        allow_self_action: bool = False,
        look_at_id: str = "",
    ) -> Dict[str, Any]:
        others = [pid for pid in self.slots if pid != speaker_id]
        default_rx = {pid: random.choice(["happy", "shy", "shocked", "thinking", "smug", "wink", "love"]) for pid in others}
        res = {
            "speech": fallback_speech,
            "emotion": fallback_emotion,
            "pose": "talking",
            "self_action": "none",
            "look_at": look_at_id or (others[0] if others else ""),
            "reactions": default_rx,
        }

        if not self._client:
            res["speech"] = sanitize_ai_speech(res["speech"], anonymize_names=True)
            return res

        p = PERSONAS[speaker_id]
        my_nick = p.get("nickname", NICKNAME_MAP.get(speaker_id, speaker_id))
        companions_desc = "；".join(
            f"ID={pid}（外号：【{NICKNAME_MAP.get(pid, pid)}】，特征：{PERSONAS[pid]['appearance_alias']}）"
            for pid in others
        ) or "（此刻只有你一个人在镜头前独播陪观众聊天！）"
        recent = "\n".join(f"[{d['name']}]：{d['text']}" for d in self.dialogue_history[-6:]) or "（露台上刚安静了一小会儿）"

        system_prompt = (
            f"{p['style_prompt']}\n"
            f"你的专属外号是【{my_nick}】。此刻同台伙伴：{companions_desc}。\n"
            "【核心表演与防封铁律】：\n"
            "1. 说自然流畅、富有角色情感色彩的中文短句，字数严格控制在 14~36 字之间；根据当下情绪自然带出软萌语气词（如开心的『呀~』『嘻嘻』、害羞思考的『唔…』『呢~』、奶凶傲娇的『哼！』『嘛！』），让语音情绪饱满生动。\n"
            "2. 严禁任何括号动作描写，严禁结巴省略号，严禁出现任何英文模型品牌名，彼此一律直呼外号（龙宝/鲸宝/葵葵/星喵/瓜瓜/月月/小桂花/班长）！\n"
            "3. 情绪选最贴切的一种（happy|excited|smug|angry|pout|shy|love|shocked|thinking|wink|sad），严禁催促打赏消费。\n"
            "4. 只输出单行纯JSON（无代码块）：\n"
            '{"speech":"自然顺口的台词","emotion":"happy","look_at":"伙伴ID或空","reactions":{"伙伴ID":"happy"}}'
        )

        try:
            self.llm_calls += 1
            resp = await self._client.chat.completions.create(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"近期对话：\n{recent}\n\n当前情境：{instruction}"},
                ],
                temperature=0.82,
                max_tokens=110,
            )
            raw = (resp.choices[0].message.content or "").strip()
            parsed = self._extract_json_dict(raw)
            if parsed:
                sp = str(parsed.get("speech", "")).strip()
                if len(sp) >= 3:
                    res["speech"] = sp
                emo = str(parsed.get("emotion", fallback_emotion)).strip().lower()
                if emo in VALID_SPEECH_EMOTIONS:
                    res["emotion"] = emo
                la = str(parsed.get("look_at", "")).strip().lower()
                if la in others:
                    res["look_at"] = la
                rx = parsed.get("reactions")
                if isinstance(rx, dict):
                    res["reactions"] = {
                        pid: (
                            str(rx.get(pid, default_rx[pid])).lower()
                            if str(rx.get(pid, "")).lower() in VALID_SPEECH_EMOTIONS
                            else default_rx[pid]
                        )
                        for pid in others
                    }
            elif len(raw) >= 4 and "{" not in raw:
                res["speech"] = raw
        except Exception as exc:
            print(f"[LLM] fallback due to: {exc}")

        res["speech"] = sanitize_ai_speech(res["speech"], anonymize_names=True)
        return res

    async def _prepare_speech_bundle(
        self,
        speaker_id: str,
        instruction: str,
        fallback_speech: str,
        fallback_emotion: str = "talking",
        look_at_id: str = "",
        familiar_user: str = "",
        event_tag: str = "",
    ) -> Dict[str, Any]:
        """并行预生成单句 LLM 台词 + TTS 音频包，使多人从左到右依次发言实现 0.15s 零空窗无缝接力！"""
        t = await self._generate_turn(speaker_id, instruction, fallback_speech, fallback_emotion, False, look_at_id)
        p = PERSONAS[speaker_id]
        clean = sanitize_ai_speech(t["speech"], anonymize_names=True)
        ext_url = f"{self.colab_url}/v1/audio/speech" if (self.use_colab_tts and self.colab_url) else ""
        audio_url, raw_dur, tts_eng = await synthesize_speech_detailed(
            text=clean,
            voice=p["voice"],
            base_rate=p["base_rate"],
            base_pitch=p["base_pitch"],
            emotion=t["emotion"],
            speaker_id=speaker_id,
            external_url=ext_url,
            external_key=self.colab_token,
            external_model=self.colab_tts_model,
            local_genie_url=self.local_genie_url,
            use_local_genie=self.use_local_genie,
            voice_tag="",
        )
        return {
            "speaker": speaker_id,
            "text": clean,
            "emotion": t["emotion"],
            "reactions": t["reactions"],
            "look_at": t["look_at"],
            "audio_url": audio_url,
            "raw_duration": raw_dur,
            "tts_engine": tts_eng,
            "familiar_user": familiar_user,
            "event_tag": event_tag,
        }

    async def _play_prepared_bundle(self, bundle: Dict[str, Any], gap_sec: float = 0.20) -> None:
        """播放已预合成好的语音包（严格单轨等待播完）"""
        speaker_id = bundle["speaker"]
        if speaker_id not in PERSONAS or speaker_id not in self.slots:
            return
        p = PERSONAS[speaker_id]
        clean = bundle["text"]
        emotion = bundle["emotion"]
        rx_map = bundle.get("reactions") or {}
        look_at = bundle.get("look_at") or ""
        familiar_user = bundle.get("familiar_user") or ""
        rate_now = max(0.70, min(1.20, float(self.voice_playback_rate or VOICE_PLAYBACK_RATE)))
        duration = round(max(1.10, float(bundle["raw_duration"]) / rate_now), 2)

        emoji_map = {
            "angry": "💢", "pout": "💢", "shocked": "❗", "surprised": "❗",
            "shy": "💗", "love": "💖", "sad": "💧", "happy": "🎵", "excited": "✨",
            "smug": "✨", "wink": "😉", "thinking": "💭",
        }
        for pid in self.slots:
            if pid == speaker_id:
                self.states[pid]["emotion"] = emotion
                self.states[pid]["pose"] = "talking"
                self.states[pid]["look_target"] = look_at
                self.states[pid]["reaction_emoji"] = emoji_map.get(emotion, "")
            else:
                r_emo = rx_map.get(pid, "idle")
                self.states[pid]["emotion"] = r_emo
                self.states[pid]["pose"] = "look_at"
                self.states[pid]["look_target"] = speaker_id
                self.states[pid]["reaction_emoji"] = emoji_map.get(r_emo, "")

        self.dialogue_history.append({"name": NICKNAME_MAP.get(speaker_id, speaker_id), "id": speaker_id, "text": clean})
        if len(self.dialogue_history) > 16:
            self.dialogue_history = self.dialogue_history[-16:]
        self.last_speaker = speaker_id
        self._speech_seq += 1

        msg_payload: Dict[str, Any] = {
            "type": "speak",
            "speech_id": self._speech_seq,
            "speaker": speaker_id,
            "color": p["color"],
            "bubble_bg": p["bubble_bg"],
            "bubble_border": p["bubble_border"],
            "text": clean,
            "char_timings": self._build_char_timings(clean),
            "emotion": emotion,
            "look_at": look_at,
            "reactions": rx_map,
            "sticker_url": "",
            "audio_url": bundle["audio_url"],
            "duration": duration,
            "playback_rate": rate_now,
            "tts_engine": bundle["tts_engine"],
            "camera_focus": {"target": speaker_id, "zoom": 1.04},
        }
        if familiar_user:
            msg_payload["familiar_user"] = familiar_user
        if bundle.get("event_tag"):
            msg_payload["event_tag"] = bundle["event_tag"]

        await self.broadcast(msg_payload)
        await self.broadcast(self.snapshot())
        await asyncio.sleep(float(duration) + gap_sec)

        for pid in self.slots:
            self.states[pid]["reaction_emoji"] = ""
            if self.states[pid]["emotion"] in ("talking", "happy", "smug", "excited"):
                self.states[pid]["emotion"] = "idle"
            if self.states[pid]["pose"] in ("talking", "look_at"):
                self.states[pid]["pose"] = "idle"

    async def _run_sequence_pipeline(self, specs: List[Dict[str, Any]], step_cb: Optional[Callable[[int], Coroutine[Any, Any, None]]] = None) -> None:
        """
        核心零空窗接力引擎：在第 i 位角色开口播放的同时，后台并行预合成第 i+1 位角色的 LLM+TTS，
        确保从左到右多位角色依次发言既 100% 严格不重叠，又 0 空等秒接话！
        """
        if not specs:
            return
        next_task: Optional[asyncio.Task[Dict[str, Any]]] = asyncio.create_task(self._prepare_speech_bundle(**specs[0]))
        try:
            for idx in range(len(specs)):
                if self._has_urgent_p0_interrupt() or self.pk_state.get("active"):
                    if next_task and not next_task.done():
                        next_task.cancel()
                    return
                if step_cb is not None:
                    await step_cb(idx + 1)
                cur_bundle = await next_task  # type: ignore[misc]
                if idx + 1 < len(specs):
                    next_task = asyncio.create_task(self._prepare_speech_bundle(**specs[idx + 1]))
                else:
                    next_task = None
                if self._has_urgent_p0_interrupt() or self.pk_state.get("active"):
                    if next_task and not next_task.done():
                        next_task.cancel()
                    return
                await self._play_prepared_bundle(cur_bundle, gap_sec=0.18)
        finally:
            if next_task and not next_task.done():
                next_task.cancel()
    @staticmethod
    def _build_char_timings(text: str) -> List[float]:
        if not text:
            return []
        weights: List[float] = []
        for ch in text:
            if ch in "。！？!?~～":
                weights.append(2.5)
            elif ch in "，、；：,:":
                weights.append(1.75)
            elif ch in "…·—- ":
                weights.append(1.2)
            else:
                weights.append(1.0)
        total = sum(weights) or 1.0
        acc = 0.0
        out: List[float] = []
        for w in weights:
            acc += w
            out.append(round(acc / total, 4))
        return out

    async def _speak(
        self,
        speaker_id: str,
        speech: str,
        emotion: str = "talking",
        reactions: Optional[Dict[str, str]] = None,
        look_at: str = "",
        pose: str = "talking",
        voice_tag: str = "",
        camera_zoom: Optional[float] = None,
        familiar_user: str = "",
    ) -> None:
        if speaker_id not in PERSONAS or speaker_id not in self.slots:
            return
        p = PERSONAS[speaker_id]
        clean = sanitize_ai_speech(speech, anonymize_names=True)

        ext_url = f"{self.colab_url}/v1/audio/speech" if (self.use_colab_tts and self.colab_url) else ""
        audio_url, raw_duration, tts_engine = await synthesize_speech_detailed(
            text=clean,
            voice=p["voice"],
            base_rate=p["base_rate"],
            base_pitch=p["base_pitch"],
            emotion=emotion,
            speaker_id=speaker_id,
            external_url=ext_url,
            external_key=self.colab_token,
            external_model=self.colab_tts_model,
            local_genie_url=self.local_genie_url,
            use_local_genie=self.use_local_genie,
            voice_tag=voice_tag,
        )
        if tts_engine == "voice_bank_offline" and voice_tag:
            baked_phrase = get_voice_bank_phrase(speaker_id, voice_tag)
            if baked_phrase:
                clean = sanitize_ai_speech(baked_phrase, anonymize_names=True)

        # 若在合成音频期间该角色已退场或主播按下下播/开麦接管（且非导播插麦），放弃播报绝不产生悬空气泡！
        if speaker_id not in self.slots:
            return

        rate_now = max(0.70, min(1.20, float(self.voice_playback_rate or VOICE_PLAYBACK_RATE)))
        duration = round(max(1.10, float(raw_duration) / rate_now), 2)
        emoji_map = {
            "angry": "💢", "pout": "💢", "shocked": "❗", "surprised": "❗",
            "shy": "💗", "love": "💖", "sad": "💧", "worried": "💧",
            "happy": "🎵", "excited": "✨", "smug": "✨", "wink": "😉",
            "thinking": "💭", "confused": "❓", "dizzy": "💫",
        }
        rx_map = reactions or {}
        for pid in self.slots:
            if pid == speaker_id:
                self.states[pid]["emotion"] = emotion
                self.states[pid]["pose"] = pose
                self.states[pid]["look_target"] = look_at
                self.states[pid]["reaction_emoji"] = emoji_map.get(emotion, "")
            else:
                r_emo = rx_map.get(pid, "idle")
                self.states[pid]["emotion"] = r_emo
                self.states[pid]["pose"] = "look_at"
                self.states[pid]["look_target"] = speaker_id
                self.states[pid]["reaction_emoji"] = emoji_map.get(r_emo, "")

        self.dialogue_history.append({"name": NICKNAME_MAP.get(speaker_id, speaker_id), "id": speaker_id, "text": clean})
        if len(self.dialogue_history) > 16:
            self.dialogue_history = self.dialogue_history[-16:]
        self.last_speaker = speaker_id
        self._speech_seq += 1

        msg_payload: Dict[str, Any] = {
            "type": "speak",
            "speech_id": self._speech_seq,
            "speaker": speaker_id,
            "color": p["color"],
            "bubble_bg": p["bubble_bg"],
            "bubble_border": p["bubble_border"],
            "text": clean,
            "char_timings": self._build_char_timings(clean),
            "emotion": emotion,
            "look_at": look_at,
            "reactions": rx_map,
            "sticker_url": "",
            "audio_url": audio_url,
            "duration": duration,
            "playback_rate": rate_now,
            "tts_engine": tts_engine,
            "camera_focus": {"target": speaker_id, "zoom": float(camera_zoom or 1.04)},
        }
        if familiar_user:
            msg_payload["familiar_user"] = familiar_user

        await self.broadcast(msg_payload)
        await self.broadcast(self.snapshot())

        wait_total = float(duration) + 0.30
        if (
            self.live_status == "live"
            and not self.human_mic_active
            and not self.pk_state.get("active")
            and not self.audience_topic_state.get("active")
            and self.priority_queue.empty()
            and len(self.slots) >= 2
            and not voice_tag
        ):
            self._schedule_next_chat_prefetch()
        await asyncio.sleep(wait_total)

        for pid in self.slots:
            self.states[pid]["reaction_emoji"] = ""
            if self.states[pid]["emotion"] in ("talking", "happy", "smug", "excited"):
                self.states[pid]["emotion"] = "idle"
            if self.states[pid]["pose"] in ("talking", "look_at"):
                self.states[pid]["pose"] = "idle"

    def _schedule_next_chat_prefetch(self) -> None:
        if self._prefetched_chat is not None or (self._prefetch_task and not self._prefetch_task.done()):
            return

        async def _worker() -> None:
            try:
                if (
                    len(self.slots) < 2
                    or self.live_status != "live"
                    or self.human_mic_active
                    or self.pk_state.get("active")
                    or not self.priority_queue.empty()
                ):
                    return
                candidates = [pid for pid in self.slots if pid != self.last_speaker] or list(self.slots)
                speaker = random.choice(candidates)
                rivals = [pid for pid in self.slots if pid != speaker]
                if not rivals:
                    return
                rival = random.choice(rivals)
                rival_nick = NICKNAME_MAP.get(rival, rival)
                next_round = self._life_chat_round + 1
                hook = " 顺便朝镜头抛个轻松小问题邀观众聊天（严禁提打赏消费）。" if next_round % 3 == 0 else ""
                fb = PERSONAS[speaker]["roast_targets"].get(rival, f"诶，{rival_nick}今天头顶的新配饰真好看呀~")
                inst = f"当前闲聊话题：{self.current_topic}。请看着【{rival_nick}】自然顺口接话或俏皮斗嘴，直接喊她外号！{hook}"
                t = await self._generate_turn(speaker, inst, fb, "talking", False, rival)
                if (
                    not self.priority_queue.empty()
                    or speaker not in self.slots
                    or self.live_status != "live"
                    or self.human_mic_active
                    or self.pk_state.get("active")
                ):
                    return
                p = PERSONAS[speaker]
                clean = sanitize_ai_speech(t["speech"], anonymize_names=True)
                ext_url = f"{self.colab_url}/v1/audio/speech" if (self.use_colab_tts and self.colab_url) else ""
                audio_url, raw_dur, tts_eng = await synthesize_speech_detailed(
                    text=clean, voice=p["voice"], base_rate=p["base_rate"], base_pitch=p["base_pitch"],
                    emotion=t["emotion"], speaker_id=speaker, external_url=ext_url, external_key=self.colab_token,
                    external_model=self.colab_tts_model, local_genie_url=self.local_genie_url,
                    use_local_genie=self.use_local_genie, voice_tag="",
                )
                if self.priority_queue.empty() and speaker in self.slots and self.live_status == "live" and not self.pk_state.get("active"):
                    self._prefetched_chat = {
                        "speaker": speaker, "text": clean, "emotion": t["emotion"], "reactions": t["reactions"],
                        "look_at": t["look_at"], "audio_url": audio_url, "raw_duration": raw_dur, "tts_engine": tts_eng,
                    }
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        self._prefetch_task = asyncio.create_task(_worker())

    async def _speak_prefetched(self, item: Dict[str, Any]) -> None:
        speaker_id = item["speaker"]
        if speaker_id not in self.slots or speaker_id not in PERSONAS:
            return
        p = PERSONAS[speaker_id]
        clean = item["text"]
        emotion = item["emotion"]
        rx_map = item.get("reactions") or {}
        look_at = item.get("look_at") or ""
        rate_now = max(0.70, min(1.20, float(self.voice_playback_rate or VOICE_PLAYBACK_RATE)))
        duration = round(max(1.10, float(item["raw_duration"]) / rate_now), 2)

        for pid in self.slots:
            if pid == speaker_id:
                self.states[pid]["emotion"] = emotion
                self.states[pid]["look_target"] = look_at
            else:
                self.states[pid]["emotion"] = rx_map.get(pid, "idle")
                self.states[pid]["look_target"] = speaker_id

        self.dialogue_history.append({"name": NICKNAME_MAP.get(speaker_id, speaker_id), "id": speaker_id, "text": clean})
        if len(self.dialogue_history) > 16:
            self.dialogue_history = self.dialogue_history[-16:]
        self.last_speaker = speaker_id
        self._speech_seq += 1

        await self.broadcast({
            "type": "speak",
            "speech_id": self._speech_seq,
            "speaker": speaker_id,
            "color": p["color"],
            "bubble_bg": p["bubble_bg"],
            "bubble_border": p["bubble_border"],
            "text": clean,
            "char_timings": self._build_char_timings(clean),
            "emotion": emotion,
            "look_at": look_at,
            "reactions": rx_map,
            "sticker_url": "",
            "audio_url": item["audio_url"],
            "duration": duration,
            "playback_rate": rate_now,
            "tts_engine": item["tts_engine"],
            "camera_focus": {"target": speaker_id, "zoom": 1.04},
        })
        await self.broadcast(self.snapshot())
        if self.live_status == "live" and not self.human_mic_active and self.priority_queue.empty() and len(self.slots) >= 2:
            self._schedule_next_chat_prefetch()
        await asyncio.sleep(float(duration) + 0.30)

    # ==================== 丝滑进出场、FIFO 轮换与小心心强制换人 ====================
    async def _perform_leave_only(self, slot_idx: int, reason: str = "rest") -> None:
        if slot_idx >= len(self.slots) or len(self.slots) <= 1:
            return
        self._cancel_prefetch()
        outgoing_id = self.slots[slot_idx]
        self._cancel_activity(outgoing_id)
        self._release_seat(outgoing_id)

        short_bye = get_voice_bank_phrase(outgoing_id, "exit") or f"{NICKNAME_MAP.get(outgoing_id, '我')}先回小屋充个电，一会儿见呀~"
        await self._speak(outgoing_id, short_bye, emotion="shy", pose="shy_sway", voice_tag="exit")
        await self.broadcast({"type": "stage_exit", "outgoing": outgoing_id, "reason": reason, "fast": True})
        await asyncio.sleep(0.38)

        if outgoing_id in self.slots:
            self.slots.remove(outgoing_id)
        self._sync_bench_queue(push_back=[outgoing_id])
        self.states[outgoing_id].update({
            "stamina": 100.0, "stay_seconds": 0.0, "emotion": "idle", "pose": "idle",
            "held_tool": "", "is_seated": False, "seat_id": "", "enter_phase": "none",
        })
        self.mode = "solo" if len(self.slots) == 1 else "life"
        self._solo_idle_since = time.time()
        self._sync_role_tags()
        await self.broadcast(self.snapshot())

    async def _invite_companion_to_stage(self, incoming_id: Optional[str] = None, announce_by: Optional[str] = None) -> Optional[str]:
        if len(self.slots) >= 3:
            return None
        self._cancel_prefetch()
        inc = self._pop_next_bench(preferred=incoming_id)
        if not inc:
            return None
        in_p = PERSONAS[inc]
        inc_nick = NICKNAME_MAP.get(inc, inc)
        if announce_by and announce_by in self.slots:
            await self._speak(
                announce_by,
                f"一个人待了一会儿好想找姐妹热闹一下，我把{inc_nick}喊上台一起聊吧！",
                emotion="happy",
                pose="happy_hop",
            )
        self.slots.append(inc)
        self._sync_bench_queue()
        self.states[inc].update({
            "stamina": 100.0, "stay_seconds": 0.0, "entered_at": time.time(),
            "emotion": "happy", "pose": "happy_hop", "enter_phase": "none",
        })
        self.mode = "life" if len(self.slots) >= 2 else "solo"
        self._sync_role_tags()
        await self.broadcast({"type": "stage_enter", "incoming": inc, "color": in_p["color"]})
        await self.broadcast(self.snapshot())
        await asyncio.sleep(0.55)
        enter_line = get_voice_bank_phrase(inc, "enter") or random.choice(in_p["enter_lines"])
        await self._speak(inc, enter_line, emotion="happy", pose="happy_hop", voice_tag="enter")
        return inc

    async def _perform_swap(
        self,
        incoming_id: Optional[str] = None,
        slot_idx: Optional[int] = None,
        reason: str = "rest",
        user: str = "",
        is_heart_summon: bool = False,
    ) -> None:
        if self.pk_state.get("active"):
            return
        self._cancel_prefetch()
        bench = self.get_bench()
        inc = incoming_id if (incoming_id and incoming_id in bench) else (bench[0] if bench else None)
        if not inc or inc in self.slots:
            return

        if slot_idx is None or slot_idx >= len(self.slots):
            slot_idx, outgoing_id = self.get_longest_stay_slot()
        else:
            outgoing_id = self.slots[slot_idx]

        in_p = PERSONAS[inc]
        inc_nick = NICKNAME_MAP.get(inc, inc)
        self._cancel_activity(outgoing_id)
        self._release_seat(outgoing_id)

        # 1. 被替换角色先说完道别再退场（道别期间休息队列顺序保持稳定，绝不被快照重排！）
        short_bye = get_voice_bank_phrase(outgoing_id, "exit") or "先回小屋休息充电啦，一会儿见~"
        await self._speak(outgoing_id, short_bye, emotion="shy", pose="shy_sway", voice_tag="exit")
        await self.broadcast({"type": "stage_exit", "outgoing": outgoing_id, "reason": reason, "fast": True})
        await asyncio.sleep(0.38)

        # 2. 原子替换槽位：新伙伴从休息队列移除上台，原下场角色追加到 FIFO 休息队尾
        if inc in self.bench_queue:
            self.bench_queue.remove(inc)
        self.slots[slot_idx] = inc
        self._sync_bench_queue(push_back=[outgoing_id])
        self.states[outgoing_id].update({
            "stamina": 100.0, "stay_seconds": 0.0, "emotion": "idle",
            "held_tool": "", "is_seated": False, "seat_id": "", "watering_active": False, "enter_phase": "none",
        })
        self.states[inc].update({
            "stamina": 100.0, "stay_seconds": 0.0, "entered_at": time.time(),
            "emotion": "love" if is_heart_summon else "happy", "pose": "happy_hop", "enter_phase": "none",
            "held_tool": "", "is_seated": False, "seat_id": "", "watering_active": False,
        })
        self._sync_role_tags()
        await self.broadcast({"type": "stage_enter", "incoming": inc, "color": in_p["color"]})
        if is_heart_summon and user:
            await self.broadcast({"type": "gift_boost", "target": inc, "user": user, "gift_name": "小心心"})
        await self.broadcast(self.snapshot())
        await asyncio.sleep(0.55)

        if is_heart_summon and user:
            rec = self.memory_store.get_fan_record(inc, user)
            total_h = int(rec["hearts"]) if rec else 1
            inst = f"【{user}】刚送出小心心把你（{inc_nick}）请上舞台啦（累计送你{total_h}颗小心心）！请超级开心地向【{user}】甜甜道谢并告诉他你牢牢记住他啦！"
            fb = f"哇塞谢谢{user}送给{inc_nick}的小心心把我喊上台！我已经把你牢牢记进心里啦~"
            t = await self._generate_turn(inc, inst, fb, "love", False, "")
            await self._speak(inc, t["speech"], "love", t["reactions"], look_at="", pose="happy_hop", familiar_user=user)
        else:
            enter_line = get_voice_bank_phrase(inc, "enter") or random.choice(in_p["enter_lines"])
            await self._speak(inc, enter_line, emotion="happy", pose="happy_hop", voice_tag="enter")

    # ==================== 老熟人从左到右依次说话（零空窗并行预合成） & 普通弹幕回应 ====================
    async def _run_familiar_greet_ltr(self, user: str) -> None:
        """老熟人进场：场上认识他的所有 AI 严格按照舞台从左到右 (0 -> 1 -> 2) 零空窗依次主动打招呼！"""
        familiar_ltr = self.memory_store.get_familiar_on_stage(user, self.slots)
        if not familiar_ltr:
            return
        specs: List[Dict[str, Any]] = []
        for order_i, pid in enumerate(familiar_ltr):
            nick = NICKNAME_MAP.get(pid, pid)
            rec = self.memory_store.get_fan_record(pid, user) or {}
            hearts = int(rec.get("hearts", 1))
            note = rec.get("last_note", "")
            note_hint = f"（你记得上次他还聊过：{note}）" if note else ""
            seq_hint = "你是左边第一个认出他的！" if order_i == 0 else "排在你左边的姐妹刚招呼完他，轮到你接力热情欢迎他！"
            inst = (
                f"曾送给你{hearts}颗小心心的老熟人【{user}】刚进入直播间啦！{note_hint}{seq_hint}"
                f"请用【{nick}】的性格热情惊喜地喊出【{user}】的名字欢迎他回来！"
            )
            fb = f"哇！是送过{nick}小心心的{user}回来啦！一眼就认出你咯，欢迎回家呀~"
            specs.append({
                "speaker_id": pid,
                "instruction": inst,
                "fallback_speech": fb,
                "fallback_emotion": "love",
                "look_at_id": "",
                "familiar_user": user,
            })
        await self._run_sequence_pipeline(specs)

    async def _run_familiar_barrage_ltr(self, user: str, text: str, mentioned: List[str]) -> None:
        """老熟人发弹幕：场上认识他的所有 AI 严格从左到右零空窗依次说话回应他！"""
        familiar_ltr = self.memory_store.get_familiar_on_stage(user, self.slots)
        if not familiar_ltr:
            return
        specs: List[Dict[str, Any]] = []
        for order_i, pid in enumerate(familiar_ltr):
            nick = NICKNAME_MAP.get(pid, pid)
            rec = self.memory_store.get_fan_record(pid, user) or {}
            hearts = int(rec.get("hearts", 1))
            seq_hint = "你是左边第一位回应他的！" if order_i == 0 else "排在你左边的姐妹刚回应完，请你从自己的性格角度接力俏皮回应他！"
            inst = (
                f"你的老熟人恩人【{user}】（送过你{hearts}颗小心心）发弹幕说：『{text}』。{seq_hint}"
                f"请带着老熟人的亲昵感直接喊【{user}】生动回应他！"
            )
            fb = f"嘿嘿{user}！{nick}一直记得你的小心心呢，你说得太有趣啦~"
            specs.append({
                "speaker_id": pid,
                "instruction": inst,
                "fallback_speech": fb,
                "fallback_emotion": "love" if order_i == 0 else "happy",
                "look_at_id": "",
                "familiar_user": user,
            })
        await self._run_sequence_pipeline(specs)

    async def _reply_to_barrage(self, user: str, text: str, mentioned: List[str]) -> None:
        if not self.slots or self.pk_state.get("active") or self.human_mic_active:
            return
        center_pid = self.slots[len(self.slots) // 2]
        if len(self.slots) == 1:
            responder = self.slots[0]
        elif mentioned:
            responder = center_pid
        else:
            cands = [p for p in self.slots if p != self.last_speaker] or self.slots
            responder = random.choice(cands)

        self._last_barrage_reply_ts = time.time()
        angry_words = ("笨", "丑", "胖", "肥鱼", "菜", "傻", "讨厌", "弱", "慢", "迷糊")
        happy_words = ("可爱", "喜欢", "棒", "厉害", "好看", "爱", "加油", "甜", "萌", "哈哈", "赞", "漂亮")
        if any(w in text for w in angry_words):
            default_emo, emo_hint = "angry", "观众在小小逗你，请奶凶气鼓鼓地可爱反驳！"
        elif any(w in text for w in happy_words):
            default_emo, emo_hint = "happy", "观众在夸你，请超开心地甜甜回应！"
        else:
            default_emo, emo_hint = "happy", "请自然俏皮地回应观众！"

        mention_hint = ""
        if mentioned:
            m_nicks = "、".join(NICKNAME_MAP.get(m, m) for m in mentioned if m in PERSONAS)
            not_here = [NICKNAME_MAP.get(m, m) for m in mentioned if m not in self.slots and m in PERSONAS]
            if not_here:
                mention_hint = f"（弹幕提到了正在小屋休息的【{'、'.join(not_here)}】，请你作为中间代表聊聊她！）"
            else:
                mention_hint = f"（弹幕点名了【{m_nicks}】，请你作为代表用外号自然回应！）"

        inst = f"观众『{user}』发弹幕说：『{text}』{mention_hint}。{emo_hint}"
        fb = f"诶？{user}发弹幕说得好有意思呀，{NICKNAME_MAP.get(responder, '我')}盯着屏幕一眼就看见啦！"
        t = await self._generate_turn(responder, inst, fb, default_emo, False)
        await self._speak(responder, t["speech"], t["emotion"], t["reactions"], look_at="", pose="talking")

    # ==================== 后台心跳（PK冻结精力 + FIFO轮换 + 低互动自动触发PK） ====================
    async def _garden_life_ticker(self) -> None:
        tick = 0
        while self._running:
            await asyncio.sleep(1.0)
            tick += 1
            now = time.time()

            if self.live_status == "live":
                # 1. 红蓝 PK 期间：精力时间完全停止（冻结），仅倒计时剩余秒数！
                if self.pk_state.get("active"):
                    self.pk_state["remaining_sec"] = max(0, int(round(self._pk_end_ts - now)))
                elif not self.audience_topic_state.get("active") and not self.human_mic_active:
                    # 2. 常规直播态：按 5 分钟（300 秒）扣减精力，耗尽时严格按 FIFO 休息队列队首轮流出场！
                    tired_slot: Optional[int] = None
                    for idx, pid in enumerate(list(self.slots)):
                        st = self.states[pid]
                        st["stay_seconds"] += 1.0
                        st["stamina"] = max(0.0, round(st["stamina"] - STAMINA_DRAIN_PER_SEC, 2))
                        if st["stamina"] <= 0.0 and tired_slot is None:
                            tired_slot = idx

                    if tired_slot is not None and not self.speaking_lock.locked() and self.priority_queue.empty():
                        bench = self.get_bench()
                        if bench:
                            await self.enqueue_event({
                                "kind": "swap_character",
                                "incoming": bench[0],  # 严格取休息队列第 1 顺位（下一位）！
                                "slot": tired_slot,
                                "reason": "rest",
                            })

                    # 3. 低互动量监测：连续 >= 55s 无有效观众互动且冷却完毕 -> 自动触发 2 人红蓝 PK 辩论！
                    idle_inter = now - self._last_interaction_ts
                    since_last_pk = now - self._last_pk_end_ts
                    if (
                        idle_inter >= LOW_INTERACTION_PK_TRIGGER_SEC
                        and since_last_pk >= PK_COOLDOWN_SEC
                        and self.priority_queue.empty()
                        and not self.speaking_lock.locked()
                    ):
                        self._last_interaction_ts = now
                        await self.start_pk(duration_sec=60, auto_triggered=True)

            if tick % 2 == 0 or self.pk_state.get("active"):
                await self.broadcast(self.snapshot())

    async def _director_main_loop(self) -> None:
        await asyncio.sleep(1.0)
        while self._running:
            try:
                if self.live_status != "live":
                    await asyncio.sleep(0.4)
                    continue

                event: Optional[Dict[str, Any]] = None
                try:
                    _, _, event = self.priority_queue.get_nowait()
                except asyncio.QueueEmpty:
                    event = None

                if event is not None:
                    async with self.speaking_lock:
                        await self._handle_event(event)
                    continue

                # 真人开麦接管中：AI 保持安静倾听，不主动发起日常闲聊抢麦
                if self.human_mic_active:
                    await asyncio.sleep(0.4)
                    continue

                async with self.speaking_lock:
                    if len(self.slots) == 1:
                        await self._step_solo_mode()
                    else:
                        await self._step_life_chat()

                silence_sec = random.uniform(1.5, 3.0) if self.allow_silence else 0.30
                self._wake_event.clear()
                if self.priority_queue.empty() and self.live_status == "live" and not self.human_mic_active:
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=silence_sec)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[ArenaEngine] main loop err: {exc}")
                await asyncio.sleep(1.2)

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "manual_speak":
            sp = event.get("speaker", "")
            if sp in self.slots:
                await self._speak(sp, event.get("text", ""), emotion=event.get("emotion", "happy"), look_at="", pose="talking")
        elif kind == "heart_summon":
            await self._perform_swap(
                incoming_id=event.get("incoming"),
                slot_idx=event.get("slot"),
                reason="heart_gift",
                user=event.get("user", ""),
                is_heart_summon=True,
            )
        elif kind == "gift_thanks":
            sp = event.get("speaker", "")
            if sp in self.slots and not self.pk_state.get("active"):
                user = event.get("user", "小伙伴")
                gift = event.get("gift_name", "小心心")
                nick = NICKNAME_MAP.get(sp, sp)
                fam = user if self.memory_store.knows_user(sp, user) else ""
                t = await self._generate_turn(
                    sp,
                    f"直播间的【{user}】送给你【{gift}】！请用{nick}的口吻超级开心地道谢并告诉他你记住他啦！",
                    f"哇塞谢谢{user}送给{nick}的{gift}呀，已经牢牢记住你咯，超开心！",
                    "love",
                    False,
                )
                await self._speak(sp, t["speech"], "love", t["reactions"], pose="happy_hop", familiar_user=fam)
        elif kind == "pk_session":
            await self._run_pk_session(event)
        elif kind == "audience_topic":
            await self._run_audience_topic_session(event.get("user", "热心观众"), event.get("topic", ""))
        elif kind == "familiar_greet":
            await self._run_familiar_greet_ltr(event.get("user", ""))
        elif kind == "familiar_barrage":
            await self._run_familiar_barrage_ltr(
                event.get("user", ""),
                event.get("text", ""),
                event.get("mentioned") or [],
            )
        elif kind == "swap_character":
            await self._perform_swap(
                incoming_id=event.get("incoming"),
                slot_idx=event.get("slot"),
                reason=event.get("reason", "invite"),
                user=event.get("user", ""),
                is_heart_summon=False,
            )
        elif kind == "leave_only":
            await self._perform_leave_only(int(event.get("slot", 0)), event.get("reason", "rest"))
        elif kind == "start_game":
            if event.get("game") == "solo":
                await self.set_stage_count(1)
            else:
                await self.return_to_life_mode()
        elif kind == "targeted_roast":
            a, b = event.get("attacker", ""), event.get("target", "")
            if a in self.slots and b in self.slots and a != b and not self.pk_state.get("active"):
                bn, an = NICKNAME_MAP.get(b, b), NICKNAME_MAP.get(a, a)
                fb = PERSONAS[a]["roast_targets"].get(b, f"嘿嘿，{bn}今天怎么这么乖巧呀！")
                t1 = await self._generate_turn(a, f"看着【{bn}】喊她外号俏皮逗逗她！", fb, "smug", False, b)
                await self._speak(a, t1["speech"], t1["emotion"], t1["reactions"], b, "talking")
                await asyncio.sleep(0.3)
                fb2 = PERSONAS[b]["roast_targets"].get(a, f"哼！{an}别老拿我寻开心啦！")
                t2 = await self._generate_turn(b, f"【{an}】刚逗你：{t1['speech']}。软萌奶凶地怼回去！", fb2, "angry", False, a)
                await self._speak(b, t2["speech"], t2["emotion"], t2["reactions"], a, "angry_puff")
        elif kind == "audience_chat":
            await self._reply_to_barrage(
                event.get("user", "观众"),
                event.get("text", ""),
                event.get("mentioned") or [],
            )

    async def _step_solo_mode(self) -> None:
        if not self.slots or self.pk_state.get("active") or self.human_mic_active:
            return
        self.mode = "solo"
        solo_id = self.slots[0]
        solo_nick = NICKNAME_MAP.get(solo_id, solo_id)
        now = time.time()
        idle_sec = now - self._solo_idle_since

        if self.random_activities and idle_sec >= 42.0 and self.get_bench():
            self._solo_idle_since = now
            await self._invite_companion_to_stage(announce_by=solo_id)
            return

        if random.random() < 0.45:
            inst = f"现在只有你【{solo_nick}】一个人在正对观众独播，弹幕有点安静。请对着镜头说一句自然可爱的话跟大家互动（绝不提打赏）！"
            fb = f"怎么大家都在悄悄潜水呀？快在弹幕喊一声{solo_nick}陪我聊聊天嘛~"
            t = await self._generate_turn(solo_id, inst, fb, "wink", False)
            await self._speak(solo_id, t["speech"], t["emotion"], {}, look_at="", pose="talking")

    async def _step_life_chat(self) -> None:
        if len(self.slots) <= 1:
            await self._step_solo_mode()
            return
        if self.pk_state.get("active") or self.human_mic_active:
            return
        if self._prefetched_chat is not None and self.priority_queue.empty():
            item = self._prefetched_chat
            self._prefetched_chat = None
            if item.get("speaker") in self.slots:
                self._life_chat_round += 1
                await self._speak_prefetched(item)
                return

        candidates = [pid for pid in self.slots if pid != self.last_speaker] or self.slots
        speaker = random.choice(candidates)
        rival = random.choice([pid for pid in self.slots if pid != speaker])
        rival_nick = NICKNAME_MAP.get(rival, rival)

        self._life_chat_round += 1
        audience_hook = (
            " 顺便朝镜头抛个轻松小问题邀观众在弹幕互动（绝不催消费）。"
            if self._life_chat_round % 3 == 0
            else ""
        )
        fb = PERSONAS[speaker]["roast_targets"].get(rival, f"诶，{rival_nick}今天头顶的配饰在阳光下特别好看呢~")
        inst = f"当前小院闲聊话题：{self.current_topic}。请看着【{rival_nick}】直接叫她外号自然接话或俏皮斗嘴！{audience_hook}"
        t = await self._generate_turn(speaker, inst, fb, "talking", False, rival)
        await self._speak(speaker, t["speech"], t["emotion"], t["reactions"], t["look_at"], "talking")
