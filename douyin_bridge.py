# -*- coding: utf-8 -*-
"""
抖音直播间弹幕、进场与礼物协议严隔桥接器（Douyin Strict Protocol Bridge · v15 双模直连升级版）
【双模自动识别】：
- 填入抖音房间号或直播间网址 (如 80017709309 或 https://live.douyin.com/xxx 或 https://v.douyin.com/xxx)
  ==> 自动启动内置原生 DouyinDirectClient 直连抖音官方弹幕+礼物推送流（0 第三方软件、0 登录、0 代理证书！）
- 填入 ws://... / wss://... 地址
  ==> 兼容旧版第三方抓包器 WebSocket 桥接
【核心事件通道】：
1. Type == 1 / WebcastChatMessage   ==> 真实文字弹幕（喊外号聊天、PK 1/2 投票、出题吐槽）
2. Type == 3 / WebcastMemberMessage ==> 观众进场（老熟人进场，在场认识他的 AI 从左到右依次欢迎）
3. Type == 2 / WebcastLikeMessage   ==> 免费点赞（轻量回血）
4. Type == 5 / WebcastGiftMessage   ==> 真实礼物（1 个小心心强换人+写入独立记忆库，其余礼物专属感谢/充能）
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import websockets

from douyin_direct import DouyinDirectClient, extract_live_id

if TYPE_CHECKING:
    from arena_engine import ArenaEngine

BASE_DIR = Path(__file__).resolve().parent


class DouyinBridge:
    def __init__(self, engine: "ArenaEngine", ws_url: str = "") -> None:
        self.engine = engine
        env_room = os.getenv("DOUYIN_ROOM_ID", "").strip()
        default_target = env_room if env_room else "auto"
        self.ws_url = default_target
        self.mode: str = "direct"
        self.live_id: str = ""
        self.room_id: str = ""
        self.anchor_name: str = ""
        self.connected = False
        self.enabled = False
        self._task: Optional[asyncio.Task[Any]] = None
        self._direct_client: Optional[DouyinDirectClient] = None
        self.last_error: str = ""
        self._combo_tracker: Dict[str, tuple[int, float]] = {}
        self.stats = {"chat_count": 0, "enter_count": 0, "gift_count": 0, "blocked_invalid": 0}
        self.recent_events: List[str] = []

    def _push_log(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.recent_events.append(f"[{ts}] {line}")
        if len(self.recent_events) > 8:
            self.recent_events = self.recent_events[-8:]

    def _persist_target_to_env(self, target: str) -> None:
        env_path = BASE_DIR / ".env"
        if not env_path.exists():
            return
        try:
            lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            kv = {}
            for l in lines:
                s = l.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    kv[k.strip()] = v.strip()
            if target.startswith(("ws://", "wss://")):
                kv["DOUYIN_WS_URL"] = target
            else:
                kv["DOUYIN_ROOM_ID"] = target
            tmp = BASE_DIR / ".env.tmp"
            tmp.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n", encoding="utf-8")
            os.replace(tmp, env_path)
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "mode": self.mode,
            "ws_url": self.ws_url,
            "live_id": self.live_id,
            "room_id": self.room_id,
            "anchor_name": self.anchor_name,
            "last_error": self.last_error,
            "stats": dict(self.stats),
            "recent_events": list(self.recent_events[-5:]),
        }

    def start(self, ws_url: Optional[str] = None) -> None:
        self.stop()
        if ws_url is not None and ws_url.strip():
            self.ws_url = ws_url.strip()
            self._persist_target_to_env(self.ws_url)
        if not self.ws_url:
            self.ws_url = "auto"

        self.enabled = True
        self.last_error = ""

        if self.ws_url.lower().startswith(("ws://", "wss://")):
            self.mode = "ws_bridge"
            self._task = asyncio.create_task(self._listen_loop())
        else:
            self.mode = "direct"
            self.live_id = "" if self.ws_url.lower() in ("auto", "companion") else extract_live_id(self.ws_url)
            loop = asyncio.get_running_loop()
            self._direct_client = DouyinDirectClient(
                target=self.ws_url,
                loop=loop,
                on_packet=self._process_packet,
                on_status=self._on_direct_status,
            )
            self._direct_client.start()

    def stop(self) -> None:
        self.enabled = False
        self.connected = False
        if self._direct_client is not None:
            try:
                self._direct_client.stop()
            except Exception:
                pass
            self._direct_client = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _on_direct_status(self, connected: bool, err: str, meta: Dict[str, Any]) -> None:
        self.connected = bool(connected)
        self.last_error = err or ""
        if meta.get("live_id"):
            self.live_id = str(meta["live_id"])
        if meta.get("room_id"):
            self.room_id = str(meta["room_id"])
        if meta.get("anchor_name"):
            self.anchor_name = str(meta["anchor_name"])
        await self.engine.broadcast({"type": "douyin_status", **self.status()})

    async def _listen_loop(self) -> None:
        while self.enabled:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    self.last_error = ""
                    await self.engine.broadcast({"type": "douyin_status", **self.status()})
                    async for raw in ws:
                        if not self.enabled:
                            break
                        await self._process_packet(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)[:120]
                await self.engine.broadcast({"type": "douyin_status", **self.status()})
                await asyncio.sleep(4.0)

        self.connected = False
        await self.engine.broadcast({"type": "douyin_status", **self.status()})

    def _cleanup_combos(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in self._combo_tracker.items() if now - ts > 60]
        for k in expired:
            del self._combo_tracker[k]

    def _route_specific_gift_target(self, gift_name: str) -> str:
        """非小心心的专属代表礼物 -> 对应角色 ID（空字符串代表走默认 FIFO 队列召唤）"""
        g = gift_name.lower()
        mapping = {
            "玫瑰": "claude",
            "大啤酒": "grok",
            "棒棒糖": "gemini",
            "荧光棒": "zhipu",
            "人气票": "gpt",
            "粉丝团灯牌": "deepseek",
            "千纸鹤": "kimi",
            "桂花": "qwen",
        }
        for key, pid in mapping.items():
            if key in g:
                return pid
        return ""

    async def _process_packet(self, raw: Any) -> None:
        try:
            pkt = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception:
            return
        if not isinstance(pkt, dict):
            return

        msg_type = pkt.get("Type") if "Type" in pkt else pkt.get("type")
        method = str(pkt.get("method") or pkt.get("Method") or "")

        data_raw = pkt.get("Data") if "Data" in pkt else pkt.get("data", pkt)
        if isinstance(data_raw, str):
            try:
                data = json.loads(data_raw)
            except Exception:
                self.stats["blocked_invalid"] += 1
                return
        elif isinstance(data_raw, dict):
            data = data_raw
        else:
            return

        user_info = data.get("User") or data.get("user") or {}
        nickname = str(user_info.get("Nickname") or user_info.get("nickname") or data.get("nickname") or "抖友").strip()
        uid = str(user_info.get("Id") or user_info.get("id_str") or nickname)

        # ==================== 1. 纯文字弹幕通道 ====================
        is_chat = (msg_type == 1) or (method == "WebcastChatMessage") or (str(msg_type).lower() == "chat")
        if is_chat:
            content = str(data.get("Content") or data.get("content") or "").strip()
            if content:
                self.stats["chat_count"] += 1
                self._push_log(f"💬 {nickname}: {content[:26]}")
                await self.engine.broadcast({"type": "douyin_status", **self.status()})
                await self.engine.handle_audience_barrage(nickname, content, source="抖音弹幕")
            return

        # ==================== 2. 观众进场通道（认识他的在场 AI 从左到右主动打招呼！） ====================
        is_enter = (msg_type == 3) or (method == "WebcastMemberMessage") or (str(msg_type).lower() in ("member", "enter", "welcome"))
        if is_enter:
            self.stats["enter_count"] += 1
            await self.engine.handle_audience_enter(nickname)
            return

        # ==================== 3. 免费点赞通道 ====================
        is_like = (msg_type == 2) or (method == "WebcastLikeMessage") or (str(msg_type).lower() == "like")
        if is_like:
            if self.engine.slots and not self.engine.pk_state.get("active"):
                target = self.engine.slots[int(time.time()) % len(self.engine.slots)]
                await self.engine.boost_character(
                    target, power=2, user=nickname, gift_name="小红心点赞", silent=True, is_real_gift=False
                )
            return

        # ==================== 4. 严格真实礼物通道 ====================
        is_gift = (msg_type == 5) or (method == "WebcastGiftMessage") or (str(msg_type).lower() == "gift")
        if not is_gift:
            return

        try:
            gift_obj = data.get("Gift") or data.get("gift") or {}
            gift_id = int(data.get("GiftId") or data.get("gift_id") or gift_obj.get("id") or 0)
            gift_name = str(data.get("GiftName") or data.get("gift_name") or gift_obj.get("name") or "").strip()
            diamond = int(data.get("DiamondCount") or data.get("diamond_count") or gift_obj.get("diamond_count") or 0)
            repeat_count = int(data.get("GiftCount") or data.get("repeat_count") or data.get("combo_count") or 1)
            group_id = str(data.get("GroupId") or data.get("group_id") or int(time.time() // 5))
        except (TypeError, ValueError):
            self.stats["blocked_invalid"] += 1
            return

        if not gift_name or (gift_id <= 0 and diamond <= 0) or repeat_count <= 0:
            self.stats["blocked_invalid"] += 1
            return

        self._cleanup_combos()
        combo_key = f"{uid}:{gift_id or gift_name}:{group_id}"
        prev_cnt, _ = self._combo_tracker.get(combo_key, (0, 0.0))
        delta_count = max(0, repeat_count - prev_cnt) if repeat_count > prev_cnt else (repeat_count if prev_cnt == 0 else 0)
        if delta_count <= 0:
            return
        self._combo_tracker[combo_key] = (max(prev_cnt, repeat_count), time.time())
        self.stats["gift_count"] += delta_count
        self._push_log(f"🎁 {nickname} 送出【{gift_name}】×{delta_count}")
        await self.engine.broadcast({"type": "douyin_status", **self.status()})

        # 核心机制：1 个「小心心」强制替换 1 位角色上场并永久记入该角色的记忆库！
        if "小心心" in gift_name:
            target_pref = str(data.get("TargetPersona") or "").strip()
            await self.engine.handle_heart_gift(
                user=nickname,
                count=delta_count,
                preferred_target=target_pref,
                gift_name="小心心",
                force_replace=True,
            )
            return

        effective_diamond = max(1, diamond)
        total_power = max(15, min(350, effective_diamond * delta_count * 15))
        specific_pid = self._route_specific_gift_target(gift_name)
        if specific_pid:
            await self.engine.boost_character(
                pid=specific_pid,
                power=total_power,
                user=nickname,
                gift_name=f"{gift_name}×{delta_count}",
                silent=False,
                is_real_gift=True,
            )
        else:
            await self.engine.handle_heart_gift(
                user=nickname,
                count=delta_count,
                preferred_target="",
                gift_name=gift_name,
                force_replace=True,
            )
