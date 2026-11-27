# -*- coding: utf-8 -*-
"""
阳光小院 AI 直播间 · 极简本机物理闭麦系统小托盘 (mic_tray.py)
定位：
- 主控后端已部署在云端服务器，本机无需常驻任何大型 Python 引擎；
- 本托盘常驻 Windows 右下角系统托盘（仅占 ~20MB 内存、0 黑框、0% CPU）：
  1. 通过 WebSocket 实时订阅云端直播与「真人开麦接管」状态；
  2. 云端开播且未接管 -> 自动硬件静音本机物理麦克风（托盘变红 🔇），彻底屏蔽杂音；
  3. 手机或电脑导播台点「真人开麦接管」 -> 瞬间解除本机物理麦静音（托盘变绿 🎙️）；
  4. 下播或退出小托盘 -> 强制恢复物理麦克风为未静音，绝不影响平时语音通话；
  5. 右键菜单支持直接切换真人开麦接管、应急静音、打开云端导播台、启动 OBS 与直播伴侣。
"""

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw
import pystray
import websockets

import mic_guard

ROOT = Path(__file__).resolve().parent
CLOUD_CFG_PATH = ROOT / "cloud_client.json"
OBS_EXE = Path(r"d:\gemini\OBS-Studio\bin\64bit\obs64.exe")
DY_EXE = Path(r"d:\gemini\DouyinLiveCompanion\直播伴侣.exe")


def _acquire_single_instance_mutex():
    """Windows 单实例互斥锁，防止托盘图标重复多开。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "Local\\AIGardenMicTraySingleton_v1")
    last_err = kernel32.GetLastError()
    if last_err == 183:  # ERROR_ALREADY_EXISTS
        return None
    return mutex


def load_cloud_config() -> Dict[str, Any]:
    default = {
        "base_url": "http://127.0.0.1:8765",
        "token": "",
        "auto_mute_on_live": True,
    }
    if CLOUD_CFG_PATH.exists():
        try:
            data = json.loads(CLOUD_CFG_PATH.read_text(encoding="utf-8"))
            default.update(data)
        except Exception:
            pass
    default["base_url"] = str(default["base_url"]).rstrip("/")
    return default


def make_tray_icon(state: str) -> Image.Image:
    """绘制清晰 64x64 矢量感托盘图标：muted(红闭麦) / live_mic(绿开麦) / offline(黄连线中)。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if state == "muted":
        bg, ring = (220, 38, 38, 255), (254, 202, 202, 255)
    elif state == "open":
        bg, ring = (22, 163, 74, 255), (187, 247, 208, 255)
    else:
        bg, ring = (217, 119, 6, 255), (254, 243, 199, 255)

    d.ellipse((2, 2, 62, 62), fill=bg, outline=ring, width=4)
    # 麦克风主体
    d.rounded_rectangle((24, 14, 40, 36), radius=8, fill=(255, 255, 255, 255))
    # 麦克风 U 型支架
    d.arc((18, 20, 46, 44), start=0, end=180, fill=(255, 255, 255, 255), width=4)
    d.line((32, 44, 32, 51), fill=(255, 255, 255, 255), width=4)
    d.line((23, 51, 41, 51), fill=(255, 255, 255, 255), width=4)

    if state == "muted":
        # 粗斜杠表示硬件闭麦
        d.line((15, 15, 49, 49), fill=(255, 255, 255, 255), width=6)
        d.line((15, 15, 49, 49), fill=(153, 27, 27, 255), width=2)
    return img


class MicTrayController:
    def __init__(self) -> None:
        self.cfg = load_cloud_config()
        self.stop_event = threading.Event()
        self.connected = False
        self.live_status = "stopped"
        self.human_mic_active = False
        self.auto_mute_mic = True
        self.mic_muted = False
        self._last_desired: Optional[bool] = None
        self._ws_send_queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._img_muted = make_tray_icon("muted")
        self._img_open = make_tray_icon("open")
        self._img_offline = make_tray_icon("offline")

        # 启动时读一次真实物理麦状态
        st = mic_guard.get_default_mic_mute()
        if st.get("ok"):
            self.mic_muted = bool(st.get("muted", False))

        self.icon = pystray.Icon(
            "ai_garden_mic_tray",
            self._choose_icon(),
            self._build_title(),
            menu=self._build_menu(),
        )

    def _choose_icon(self) -> Image.Image:
        if not self.connected:
            return self._img_offline
        return self._img_muted if self.mic_muted else self._img_open

    def _build_title(self) -> str:
        conn_txt = "云端已连" if self.connected else "连接云端中..."
        live_map = {"live": "直播中", "paused": "已暂停", "stopped": "已下播"}
        live_txt = live_map.get(self.live_status, self.live_status)
        mic_txt = "物理麦已闭麦" if self.mic_muted else "物理麦已开麦"
        return f"阳光小院小托盘 | {conn_txt} | {live_txt} | {mic_txt}"

    def _refresh_tray_ui(self) -> None:
        try:
            self.icon.icon = self._choose_icon()
            self.icon.title = self._build_title()
            self.icon.update_menu()
        except Exception:
            pass

    def _status_line_1(self, _item) -> str:
        conn = "🟢 云端后端已连" if self.connected else "🟡 正在重连云端..."
        live_map = {"live": "直播中", "paused": "已暂停", "stopped": "已下播"}
        return f"{conn} · {live_map.get(self.live_status, '未知')}"

    def _status_line_2(self, _item) -> str:
        if self.mic_muted:
            return "🔇 本机物理麦：已硬件静音（外界 0 收音）"
        return "🎙️ 本机物理麦：已开麦（说话会进直播间）"

    def _takeover_label(self, _item) -> str:
        return "🔇 结束真人接管（重新闭麦让 AI 聊）" if self.human_mic_active else "🎙️ 开启真人开麦接管（开麦插话）"

    def _manual_mic_label(self, _item) -> str:
        return "🔊 手动解除物理麦静音" if self.mic_muted else "🔇 手动立即静音物理麦"

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._status_line_1, None, enabled=False),
            pystray.MenuItem(self._status_line_2, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._takeover_label, self.on_toggle_takeover),
            pystray.MenuItem(self._manual_mic_label, self.on_toggle_manual_mic),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🌐 打开云端导播台（免密码）", self.on_open_director),
            pystray.MenuItem("📺 一键启动 OBS + 抖音直播伴侣", self.on_launch_obs_dy),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ 退出小托盘（恢复物理麦）", self.on_quit),
        )

    def _apply_local_mic(self, muted: bool, force: bool = False) -> None:
        if (not force) and (self._last_desired == muted) and (self.mic_muted == muted):
            return
        res = mic_guard.set_default_mic_mute(muted)
        if res.get("ok"):
            self.mic_muted = bool(res.get("muted", muted))
            self._last_desired = muted
        self._refresh_tray_ui()
        self._queue_ws_report(self.mic_muted)

    def _queue_ws_report(self, muted: bool) -> None:
        if self._loop and self._ws_send_queue:
            try:
                self._loop.call_soon_threadsafe(
                    self._ws_send_queue.put_nowait,
                    json.dumps({"type": "tray_mic_report", "muted": bool(muted)}),
                )
            except Exception:
                pass

    def _post_cloud(self, path: str, body: Dict[str, Any]) -> None:
        def _worker():
            self.cfg = load_cloud_config()
            url = f"{self.cfg['base_url']}{path}"
            headers = {"Content-Type": "application/json"}
            if self.cfg.get("token"):
                headers["X-Live-Token"] = self.cfg["token"]
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=8) as r:
                    _ = r.read()
            except Exception:
                pass
        threading.Thread(target=_worker, daemon=True).start()

    def on_toggle_takeover(self, _icon, _item) -> None:
        next_active = not self.human_mic_active
        self.human_mic_active = next_active
        if next_active:
            self._apply_local_mic(False, force=True)
        elif self.live_status in ("live", "paused") and self.auto_mute_mic:
            self._apply_local_mic(True, force=True)
        self._post_cloud("/api/mic_takeover", {"active": next_active})

    def on_toggle_manual_mic(self, _icon, _item) -> None:
        self._apply_local_mic(not self.mic_muted, force=True)

    def on_open_director(self, _icon, _item) -> None:
        self.cfg = load_cloud_config()
        tok = self.cfg.get("token", "")
        q = f"?token={tok}" if tok else ""
        webbrowser.open(f"{self.cfg['base_url']}/director{q}")

    def on_launch_obs_dy(self, _icon, _item) -> None:
        def _worker():
            if OBS_EXE.exists():
                try:
                    subprocess.Popen([str(OBS_EXE), "--startvirtualcam"], cwd=str(OBS_EXE.parent))
                except Exception:
                    pass
            if DY_EXE.exists():
                try:
                    ctypes.windll.shell32.ShellExecuteW(None, "runas", str(DY_EXE), None, str(DY_EXE.parent), 1)
                except Exception:
                    pass
        threading.Thread(target=_worker, daemon=True).start()

    def on_quit(self, _icon, _item) -> None:
        self.stop_event.set()
        try:
            mic_guard.set_default_mic_mute(False)
        except Exception:
            pass
        self.icon.stop()

    def _ws_url(self) -> str:
        self.cfg = load_cloud_config()
        base = self.cfg["base_url"]
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        else:
            ws_base = "ws://" + base
        tok = self.cfg.get("token", "")
        return f"{ws_base}/ws?token={tok}" if tok else f"{ws_base}/ws"

    async def _ws_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ws_send_queue = asyncio.Queue(maxsize=32)

        while not self.stop_event.is_set():
            url = self._ws_url()
            try:
                async with websockets.connect(
                    url,
                    ping_interval=8,
                    ping_timeout=8,
                    open_timeout=10,
                ) as ws:
                    self.connected = True
                    self._refresh_tray_ui()

                    async def _sender():
                        while not self.stop_event.is_set():
                            msg = await self._ws_send_queue.get()
                            await ws.send(msg)

                    send_task = asyncio.create_task(_sender())
                    try:
                        async for raw in ws:
                            if self.stop_event.is_set():
                                break
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if msg.get("type") == "state_sync":
                                self.live_status = str(msg.get("live_status", "stopped"))
                                self.human_mic_active = bool(msg.get("human_mic_active", False))
                                self.auto_mute_mic = bool(msg.get("auto_mute_mic", True))
                                if "desired_mic_muted" in msg:
                                    desired = bool(msg["desired_mic_muted"])
                                else:
                                    desired = self.auto_mute_mic and (not self.human_mic_active)
                                await asyncio.to_thread(self._apply_local_mic, desired)
                    finally:
                        send_task.cancel()
            except Exception:
                pass

            self.connected = False
            self._refresh_tray_ui()
            for _ in range(30):
                if self.stop_event.is_set():
                    break
                await asyncio.sleep(0.1)

    def _start_ws_thread(self) -> None:
        def _runner():
            asyncio.run(self._ws_loop())
        threading.Thread(target=_runner, name="cloud-mic-ws", daemon=True).start()

    def run(self) -> None:
        self._start_ws_thread()
        self.icon.run()


def main() -> int:
    mutex = _acquire_single_instance_mutex()
    if os.name == "nt" and mutex is None:
        return 0
    ctrl = MicTrayController()
    try:
        ctrl.run()
    finally:
        try:
            mic_guard.set_default_mic_mute(False)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
