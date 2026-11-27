# -*- coding: utf-8 -*-
"""
抖音直播间原生直连弹幕 + 真实礼物 + 进场采集器 (douyin_direct.py)
特点：
1. 0 第三方抓包软件、0 代理证书、0 登录要求：只需输入抖音直播间房间号、网页链接 (https://live.douyin.com/xxx) 或抖音分享短链 (https://v.douyin.com/xxx)；
2. 强制直连国内网络 (trust_env=False / 无代理)，绝不被本机海外代理触发抖音风控；
3. 完整解码抖音原生 Protobuf 二进制推送流：
   - WebcastChatMessage   -> Type=1 真实文字弹幕（支持点名、1/2 PK投票、出题）
   - WebcastGiftMessage   -> Type=5 真实礼物（精准提取礼物ID、名称、钻石价值、连击数与组ID，完美触发小心心换人+记忆库）
   - WebcastMemberMessage -> Type=3 观众进场（老熟人进场自动打招呼）
   - WebcastLikeMessage   -> Type=2 免费点赞（轻量回血）
4. 支持未开播自动轮询等待开播 + 断线 4 秒自动重连 + 优雅退出停心跳。
"""

import asyncio
import gzip
import hashlib
import json
import os
import random
import re
import string
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import requests
import websocket
from py_mini_racer import MiniRacer

from douyin_proto.douyin import (
    ChatMessage,
    ControlMessage,
    GiftMessage,
    LikeMessage,
    MemberMessage,
    PushFrame,
    Response,
)

PROTO_DIR = Path(__file__).resolve().parent / "douyin_proto"
SIGN_JS_PATH = PROTO_DIR / "sign.js"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)

_SIGN_SCRIPT_CACHE: Optional[str] = None
_SIGN_LOCK = threading.Lock()


def _gen_ms_token(length: int = 182) -> str:
    chars = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(chars) for _ in range(length))


def _generate_signature(wss_url: str) -> str:
    global _SIGN_SCRIPT_CACHE
    keys = (
        "live_id,aid,version_code,webcast_sdk_version,"
        "room_id,sub_room_id,sub_channel_id,did_rule,"
        "user_unique_id,device_platform,device_type,ac,identity"
    ).split(",")
    q_parts = urllib.parse.urlparse(wss_url).query.split("&")
    q_map = {kv.split("=")[0]: kv.split("=")[-1] for kv in q_parts if "=" in kv}
    stub = ",".join(f"{k}={q_map.get(k, '')}" for k in keys)
    md5_hex = hashlib.md5(stub.encode("utf-8")).hexdigest()

    with _SIGN_LOCK:
        if _SIGN_SCRIPT_CACHE is None:
            _SIGN_SCRIPT_CACHE = SIGN_JS_PATH.read_text(encoding="utf-8")
        ctx = MiniRacer()
        ctx.eval(_SIGN_SCRIPT_CACHE)
        return str(ctx.call("get_sign", md5_hex))


def extract_live_id(raw_input: str) -> str:
    """支持纯房间号、https://live.douyin.com/123456、抖音分享整段文案/短链提取出 live_id。"""
    s = (raw_input or "").strip()
    if not s:
        return ""
    # 1. 直接就是数字房间号 (web_rid)
    if re.fullmatch(r"\d{5,20}", s):
        return s
    # 2. 包含 live.douyin.com/<数字>
    m = re.search(r"live\.douyin\.com/(\d{5,20})", s)
    if m:
        return m.group(1)
    # 3. 包含 v.douyin.com 短链 -> 直连跟随重定向提取
    m_short = re.search(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?", s)
    if m_short:
        try:
            sess = requests.Session()
            sess.trust_env = False
            r = sess.get(m_short.group(0), headers={"User-Agent": UA}, allow_redirects=True, timeout=10)
            for candidate in (r.url, r.text[:4000]):
                m2 = re.search(r"live\.douyin\.com/(\d{5,20})", candidate)
                if m2:
                    return m2.group(1)
                m3 = re.search(r"web_rid[=:\\\"]+(\d{5,20})", candidate)
                if m3:
                    return m3.group(1)
                m4 = re.search(r"/reflow/(\d{10,22})", candidate)
                if m4:
                    return "room:" + m4.group(1)
        except Exception:
            pass
    # 4. 兜底提取任意长数字串
    m_num = re.search(r"\b(\d{6,20})\b", s)
    return m_num.group(1) if m_num else ""


def detect_companion_live_room() -> Optional[Dict[str, str]]:
    """自动读取本机官方《抖音直播伴侣》roomStore.json，免手动填房间号直接拿到当前开播精确 room_id！"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    store_path = Path(appdata) / "webcast_mate" / "WBStore" / "roomStore.json"
    if not store_path.exists():
        return None
    try:
        data = json.loads(store_path.read_text(encoding="utf-8", errors="ignore"))
        rs = data.get("roomStore") if isinstance(data, dict) else None
        if not isinstance(rs, dict):
            return None
        cur = (rs.get("liveStatusData") or {}).get("currentRoomData") or {}
        sdk = rs.get("sdkStartStreamParams") or {}
        settings = rs.get("settings") or {}
        room_id = str(sdk.get("roomId") or cur.get("id_str") or settings.get("id_str") or "").strip()
        if not room_id.isdigit():
            return None
        status_val = rs.get("status")
        # 直播伴侣里 roomStore.status == 2 表示正在直播中
        is_live = (str(status_val) == "2")
        owner = cur.get("owner") or settings.get("owner") or {}
        nick = str(owner.get("nickname") or "").strip()
        web_rid = str(owner.get("web_rid") or "").strip()
        return {
            "room_id": room_id,
            "live_id": web_rid or room_id,
            "anchor_name": nick,
            "status_str": "2" if is_live else "4",
        }
    except Exception:
        return None


def resolve_room_info(live_id_or_url: str) -> Tuple[str, str, str, str, str]:
    """
    直连抖音网页版解析 (live_id, room_id, ttwid, anchor_name, status_str)
    支持 "auto" 自动读取本机《抖音直播伴侣》当前正在开播的房间！
    status_str: '2' 表示直播中，'4' 表示未开播/已下播
    """
    raw_in = (live_id_or_url or "").strip()
    sess = requests.Session()
    sess.trust_env = False  # 强制直连国内抖音，不走代理！
    r0 = sess.get("https://live.douyin.com/", headers={"User-Agent": UA}, timeout=12)
    ttwid = r0.cookies.get("ttwid") or sess.cookies.get("ttwid") or ""
    if not ttwid:
        raise RuntimeError("获取抖音 ttwid 失败，请检查本机国内网络连接")

    # 1. 自动识别本机《抖音直播伴侣》当前开播房间（免填房间号、换场自动跟进！）
    if raw_in.lower() in ("", "auto", "companion"):
        comp = detect_companion_live_room()
        if not comp:
            raise RuntimeError("未检测到本机《抖音直播伴侣》开播记录，请先在直播伴侣点开始直播，或手动粘贴直播间链接")
        return comp["live_id"], comp["room_id"], ttwid, comp["anchor_name"], comp["status_str"]

    # 2. 若直接传入了 19 位精确 room_id (如 7712410230612593450)，免网页解析直连！
    if re.fullmatch(r"\d{18,22}", raw_in):
        return raw_in, raw_in, ttwid, "", "2"

    live_id = extract_live_id(raw_in)
    if not live_id:
        raise ValueError("无法识别抖音房间号，可填 auto 自动读取直播伴侣，或粘贴 live.douyin.com/房间号")

    # 如果短链直接拿到了 room_id
    if live_id.startswith("room:"):
        room_id = live_id[5:]
        return room_id, room_id, ttwid, "", "2"

    page_url = f"https://live.douyin.com/{live_id}"
    headers = {
        "User-Agent": UA,
        "Referer": "https://live.douyin.com/",
        "cookie": f"ttwid={ttwid}; msToken={_gen_ms_token()}; __ac_nonce=0123407cc00a9e438deb4",
    }
    resp = sess.get(page_url, headers=headers, timeout=12)
    resp.raise_for_status()
    html = resp.text

    m_rid = re.search(r'roomId\\":\\"(\d+)\\"', html)
    if not m_rid:
        m_rid = re.search(r'"roomId":"(\d+)"', html)
    if not m_rid:
        raise RuntimeError(f"未能从房间 {live_id} 页面提取 roomId（可能房间号有误或尚未开播）")
    room_id = m_rid.group(1)

    m_st = re.search(r'\\"status\\":(\d)', html) or re.search(r'"status":(\d)', html)
    status_str = m_st.group(1) if m_st else "2"

    m_nick = re.search(r'\\"owner\\":\{[^}]{0,400}?\\"nickname\\":\\"([^\\"$]{1,36})\\"', html)
    if not m_nick:
        m_nick = re.search(r'\\"nickname\\":\\"([^\\"$]{1,36})\\"', html)
    anchor_name = m_nick.group(1) if m_nick else ""
    if anchor_name.lower() in ("undefined", "null"):
        anchor_name = ""
    return live_id, room_id, ttwid, anchor_name, status_str


class DouyinDirectClient:
    """后台独立线程直连抖音原生 WebSocket，把解码后的标准数据包投递到 asyncio 主循环。"""

    def __init__(
        self,
        target: str,
        loop: asyncio.AbstractEventLoop,
        on_packet: Callable[[Dict[str, Any]], Awaitable[Any]],
        on_status: Callable[[bool, str, Dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self.target = target.strip()
        self.loop = loop
        self.on_packet = on_packet
        self.on_status = on_status
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self.live_id: str = ""
        self.room_id: str = ""
        self.anchor_name: str = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run_forever, name="douyin-direct-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _emit_status(self, connected: bool, err: str = "") -> None:
        meta = {
            "mode": "direct",
            "live_id": self.live_id,
            "room_id": self.room_id,
            "anchor_name": self.anchor_name,
        }
        try:
            asyncio.run_coroutine_threadsafe(self.on_status(connected, err, meta), self.loop)
        except Exception:
            pass

    def _emit_packet(self, pkt: Dict[str, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.on_packet(pkt), self.loop)
        except Exception:
            pass

    def _run_forever(self) -> None:
        while not self._stop_evt.is_set():
            try:
                live_id, room_id, ttwid, anchor_name, status_str = resolve_room_info(self.target)
                self.live_id = live_id
                self.room_id = room_id
                self.anchor_name = anchor_name

                if status_str == "4":
                    self._emit_status(False, f"房间 {live_id}({anchor_name or '主播'}) 当前未开播，每 5 秒自动检测开播...")
                    self._stop_evt.wait(5.0)
                    continue

                wss = (
                    "wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
                    "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
                    "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
                    "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
                    "&browser_name=Mozilla"
                    "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
                    "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
                    "&browser_online=true&tz_name=Asia/Shanghai"
                    "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
                    f"&internal_ext=internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:7319483754668557238"
                    "|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
                    "wrds_v:7392094459690748497"
                    "&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
                    "&user_unique_id=7319483754668557238&im_path=/webcast/im/fetch/&identity=audience"
                    f"&need_persist_msg_count=0&insert_task_id=&live_reason=&room_id={room_id}&heartbeatDuration=0"
                )
                sig = _generate_signature(wss)
                wss += f"&signature={sig}"

                hb_stop = threading.Event()

                def on_open(ws):
                    self._emit_status(True, "")

                    def _hb():
                        while not hb_stop.is_set() and not self._stop_evt.is_set():
                            try:
                                hb_frame = PushFrame(payload_type="hb").SerializeToString()
                                ws.send(hb_frame, websocket.ABNF.OPCODE_PING)
                            except Exception:
                                break
                            hb_stop.wait(5.0)

                    threading.Thread(target=_hb, name="douyin-hb", daemon=True).start()

                def on_message(ws, raw_bytes):
                    try:
                        pkg = PushFrame().parse(raw_bytes)
                        resp = Response().parse(gzip.decompress(pkg.payload))
                        if resp.need_ack:
                            ack = PushFrame(
                                log_id=pkg.log_id,
                                payload_type="ack",
                                payload=resp.internal_ext.encode("utf-8"),
                            ).SerializeToString()
                            ws.send(ack, websocket.ABNF.OPCODE_BINARY)

                        for m in resp.messages_list:
                            self._dispatch_proto_msg(m.method, m.payload, ws)
                    except Exception:
                        pass

                def on_error(_ws, err):
                    if not self._stop_evt.is_set():
                        self._emit_status(False, f"抖音连接波动: {str(err)[:80]}")

                def on_close(_ws, *_args):
                    hb_stop.set()
                    if not self._stop_evt.is_set():
                        self._emit_status(False, "连接已断开，4 秒后自动重连...")

                self._ws = websocket.WebSocketApp(
                    wss,
                    header={"cookie": f"ttwid={ttwid}", "user-agent": UA},
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                # 严禁走本机海外代理连接国内抖音 WSS！
                self._ws.run_forever(http_proxy_host=None, http_proxy_port=None)
                hb_stop.set()
            except Exception as exc:
                self._emit_status(False, str(exc)[:110])

            if not self._stop_evt.is_set():
                self._stop_evt.wait(4.0)

        self._emit_status(False, "")

    def _dispatch_proto_msg(self, method: str, payload: bytes, ws) -> None:
        if method == "WebcastChatMessage":
            c = ChatMessage().parse(payload)
            content = (c.content or "").strip()
            if content:
                self._emit_packet({
                    "Type": 1,
                    "method": "WebcastChatMessage",
                    "Data": {
                        "User": {"Id": str(c.user.id or ""), "Nickname": c.user.nick_name or "抖友"},
                        "Content": content,
                    },
                })
        elif method == "WebcastGiftMessage":
            g = GiftMessage().parse(payload)
            gift_name = (g.gift.name or "").strip()
            gift_id = int(g.gift.id or g.gift_id or 0)
            diamond = int(g.gift.diamond_count or 0)
            # 抖音送礼连击：优先取 combo_count，其次 repeat_count / total_count，兜底 1
            cnt = int(g.combo_count or g.repeat_count or g.total_count or 1)
            grp = str(g.group_id or g.send_time or int(time.time() // 4))
            if gift_name:
                self._emit_packet({
                    "Type": 5,
                    "method": "WebcastGiftMessage",
                    "Data": {
                        "User": {"Id": str(g.user.id or ""), "Nickname": g.user.nick_name or "热心观众"},
                        "GiftId": gift_id or 1,
                        "GiftName": gift_name,
                        "DiamondCount": max(1, diamond),
                        "GiftCount": max(1, cnt),
                        "GroupId": grp,
                        "RepeatEnd": int(g.repeat_end or 0),
                    },
                })
        elif method == "WebcastMemberMessage":
            mb = MemberMessage().parse(payload)
            nick = (mb.user.nick_name or "").strip()
            if nick:
                self._emit_packet({
                    "Type": 3,
                    "method": "WebcastMemberMessage",
                    "Data": {
                        "User": {"Id": str(mb.user.id or ""), "Nickname": nick},
                    },
                })
        elif method == "WebcastLikeMessage":
            lk = LikeMessage().parse(payload)
            self._emit_packet({
                "Type": 2,
                "method": "WebcastLikeMessage",
                "Data": {
                    "User": {"Id": str(lk.user.id or ""), "Nickname": lk.user.nick_name or "抖友"},
                    "Count": int(lk.count or 1),
                },
            })
        elif method == "WebcastControlMessage":
            ctl = ControlMessage().parse(payload)
            if ctl.status == 3:
                self._emit_status(False, "主播已下播，等待下次开播自动重连...")
                try:
                    ws.close()
                except Exception:
                    pass
