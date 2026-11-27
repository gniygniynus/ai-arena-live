# -*- coding: utf-8 -*-
"""
阳光小院 · Bilibili 授权、多线路分片自动投稿 & AI 智能看视频/评论托管引擎 (bili_agent_service.py)
设计灵感致谢：
- 视频学习笔记巡航参考开源社区 xiaoyaya191/bilibili_learning_bot 与 Shanoa2/BiliInsight 理念；
- 自己稿件评论区楼中楼回复参考 Janson20/BiliCommentBot 理念；
- 扫码授权与 UPOS 分片上传遵循 B 站官方 Web 端创作中心公开协议（兼容 biliup cookies.json 格式）。
安全与防风控硬性准则（本模块强制执行）：
1. 全部 B 站 HTTP 请求硬编码 trust_env=False 强制走国内直连网络，严禁经过海外代理触发异地风控；
2. 严禁绕过任何图形/极验 CAPTCHA：推荐官方 APP 扫码登录；账密遇到验证码立即引导人工完成；
3. 对外评论默认进入「人工审批草稿箱 (approval_queue)」，叠加每日配额、90~260s 随机冷却、深夜休眠、同 BV 永久去重与风控码自动急停。
"""

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import random
import sqlite3
import time
import urllib.parse
import uuid
from functools import reduce
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import qrcode

BASE_DIR = Path(__file__).resolve().parent
PROMO_DIR = BASE_DIR / "static" / "promo"
COOKIE_PATH = BASE_DIR / "bili_cookies.json"
DB_PATH = BASE_DIR / "studio_bili.db"

BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _get_mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def sign_wbi_params(params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
    mixin_key = _get_mixin_key(img_key + sub_key)
    curr_time = round(time.time())
    signed = dict(params, wts=curr_time)
    signed = dict(sorted(signed.items()))
    signed = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in signed.items()
    }
    query = urllib.parse.urlencode(signed)
    wbi_sign = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    signed["w_rid"] = wbi_sign
    return signed


class BiliAgentService:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._init_db()
        self.cookies: Dict[str, str] = {}
        self.refresh_token: str = ""
        self.user_info: Dict[str, Any] = {}
        self._wbi_keys: Tuple[str, str] = ("", "")
        self._load_cookies()

        self.agent_cfg: Dict[str, Any] = self._load_agent_config()
        self.running: bool = False
        self._agent_task: Optional[asyncio.Task[Any]] = None
        self.logs: List[Dict[str, str]] = []
        self.upload_tasks: Dict[str, Dict[str, Any]] = {}

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.logs.append({"ts": ts, "msg": msg})
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_config (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watched_videos (
                    bvid TEXT PRIMARY KEY,
                    aid INTEGER,
                    title TEXT,
                    up_name TEXT,
                    note TEXT,
                    watched_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS commented_bvids (
                    bvid TEXT PRIMARY KEY,
                    content TEXT,
                    commented_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS replied_rpids (
                    rpid TEXT PRIMARY KEY,
                    bvid TEXT,
                    reply_text TEXT,
                    replied_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS comment_drafts (
                    id TEXT PRIMARY KEY,
                    bvid TEXT,
                    aid INTEGER,
                    video_title TEXT,
                    content TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS upload_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bvid TEXT,
                    aid INTEGER,
                    title TEXT,
                    video_filename TEXT,
                    created_at INTEGER
                );
                """
            )

    def _load_agent_config(self) -> Dict[str, Any]:
        default = {
            "enable_watch": True,
            "enable_fav": True,
            "feed_source": "rcmd_and_keyword",
            "keywords": "AI直播,虚拟主播,Live2D,AI Agent,大模型应用,独立开发",
            "watch_min_sec": 45,
            "watch_max_sec": 140,
            "reply_own_comments": True,
            "generate_ext_comments": True,
            "ext_comment_mode": "approval_queue",
            "persona_style": "longbao",
            "system_prompt": "结合视频具体内容发表真诚、有共鸣的短评（15~45字），严禁硬广、严禁引导加群或留联系方式，像真实热心B站观众一样自然交流。",
            "daily_watch_limit": 20,
            "daily_comment_limit": 10,
            "cooldown_min_sec": 90,
            "cooldown_max_sec": 260,
            "active_start": "09:00",
            "active_end": "23:30",
            "auto_circuit_break": True,
            "dedup_bvid": True,
        }
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute("SELECT v FROM kv_config WHERE k='agent_cfg'").fetchone()
                if row:
                    default.update(json.loads(row[0]))
        except Exception:
            pass
        return default

    def save_agent_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        self.agent_cfg.update(patch)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_config (k, v) VALUES ('agent_cfg', ?)",
                (json.dumps(self.agent_cfg, ensure_ascii=False),),
            )
        self._log("💾 已保存 B 站 AI 托管与防风控安全策略")
        return {"ok": True, "config": self.agent_cfg}

    # ==================== Cookie 持久化与国内直连客户端 ====================
    def _load_cookies(self) -> None:
        if not COOKIE_PATH.exists():
            return
        try:
            data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "cookie_info" in data and isinstance(data["cookie_info"], dict):
                    # 兼容 biliup 生成的 cookies.json 结构
                    for c in data["cookie_info"].get("cookies", []):
                        if c.get("name") and c.get("value"):
                            self.cookies[str(c["name"])] = str(c["value"])
                    self.refresh_token = str(data.get("token_info", {}).get("refresh_token", ""))
                else:
                    self.cookies = {str(k): str(v) for k, v in data.get("cookies", data).items() if isinstance(v, (str, int))}
                    self.refresh_token = str(data.get("refresh_token", ""))
                    self.user_info = data.get("user_info", {}) if isinstance(data.get("user_info"), dict) else {}
        except Exception:
            pass

    def _save_cookies(self) -> None:
        payload = {
            "cookies": self.cookies,
            "refresh_token": self.refresh_token,
            "user_info": self.user_info,
            "updated_at": int(time.time()),
            # 同时写出兼容 biliup 的 cookie_info 块，方便外部工具零配置复用
            "cookie_info": {
                "cookies": [{"name": k, "value": v} for k, v in self.cookies.items()],
            },
        }
        tmp = BASE_DIR / "bili_cookies.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, COOKIE_PATH)

    def _client(self, timeout: float = 15.0) -> httpx.AsyncClient:
        """强制国内直连 (trust_env=False)，绝不经过海外代理！"""
        headers = {
            "User-Agent": BILI_UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        return httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            headers=headers,
            cookies=self.cookies,
            follow_redirects=True,
        )

    # ==================== 1. 官方扫码 / Cookie 授权登录 ====================
    async def generate_login_qrcode(self) -> Dict[str, Any]:
        async with self._client(10.0) as cli:
            r = await cli.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
            d = r.json()
        if d.get("code") != 0:
            return {"ok": False, "error": f"获取二维码失败: {d.get('message')}"}
        qr_url = d["data"]["url"]
        qr_key = d["data"]["qrcode_key"]

        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1e293b", back_color="#ffffff")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        self._log("📱 已生成 B 站官方安全扫码二维码，请使用手机哔哩哔哩 APP 扫码确认")
        return {"ok": True, "qrcode_key": qr_key, "qr_url": qr_url, "qr_image_base64": b64}

    async def poll_login_qrcode(self, qrcode_key: str) -> Dict[str, Any]:
        async with self._client(10.0) as cli:
            r = await cli.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
            )
            d = r.json()
            data = d.get("data") or {}
            code = data.get("code", -1)
            if code == 86101:
                return {"ok": True, "logged_in": False, "code": code, "status_text": "等待手机扫描二维码..."}
            if code == 86090:
                return {"ok": True, "logged_in": False, "code": code, "status_text": "已扫码！请在手机上点击「确认登录」"}
            if code == 86038:
                return {"ok": False, "logged_in": False, "code": code, "status_text": "二维码已过期，请点击刷新"}
            if code == 0:
                # 提取响应 Set-Cookie 与 URL 参数中的核心凭证
                for k, v in r.cookies.items():
                    self.cookies[k] = v
                url_str = str(data.get("url") or "")
                if "?" in url_str:
                    qs = urllib.parse.parse_qs(url_str.split("?", 1)[1])
                    for ck in ("DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct", "sid"):
                        if ck in qs and qs[ck]:
                            self.cookies[ck] = qs[ck][0]
                self.refresh_token = str(data.get("refresh_token") or "")
                nav = await self.verify_and_refresh_nav()
                self._save_cookies()
                uname = nav.get("user", {}).get("uname", "B站用户")
                self._log(f"✅ B站扫码登录成功！当前账号：{uname}")
                return {"ok": True, "logged_in": True, "status_text": "扫码登录成功！", "user": nav.get("user", {})}
        return {"ok": False, "logged_in": False, "status_text": f"未知状态: {data}"}

    async def import_raw_cookie(self, raw_cookie: str) -> Dict[str, Any]:
        s = (raw_cookie or "").strip()
        if not s:
            return {"ok": False, "error": "粘贴内容为空"}
        parsed: Dict[str, str] = {}
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if "cookie_info" in obj:
                    for c in obj["cookie_info"].get("cookies", []):
                        if c.get("name") and c.get("value"):
                            parsed[str(c["name"])] = str(c["value"])
                elif "cookies" in obj and isinstance(obj["cookies"], dict):
                    parsed = {str(k): str(v) for k, v in obj["cookies"].items()}
                else:
                    parsed = {str(k): str(v) for k, v in obj.items() if isinstance(v, (str, int))}
            except Exception as exc:
                return {"ok": False, "error": f"JSON 解析失败: {exc}"}
        else:
            for part in s.replace("\n", ";").split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    parsed[k.strip()] = v.strip()

        if "SESSDATA" not in parsed or "bili_jct" not in parsed:
            return {"ok": False, "error": "未找到 SESSDATA 或 bili_jct，请复制完整 Cookie 或 biliup cookies.json"}

        self.cookies.update(parsed)
        nav = await self.verify_and_refresh_nav()
        if not nav.get("logged_in"):
            return {"ok": False, "error": "该 Cookie 已失效或未登录，请重新使用扫码登录"}
        self._save_cookies()
        self._log(f"✅ Cookie 凭证验证通过！当前账号：{nav['user'].get('uname')}")
        return {"ok": True, "logged_in": True, "user": nav["user"]}

    async def verify_and_refresh_nav(self) -> Dict[str, Any]:
        if not self.cookies.get("SESSDATA"):
            return {"ok": True, "logged_in": False, "user": {}}
        try:
            async with self._client(10.0) as cli:
                r = await cli.get("https://api.bilibili.com/x/web-interface/nav")
                d = r.json()
            data = d.get("data") or {}
            wbi_img = data.get("wbi_img") or {}
            img_url = str(wbi_img.get("img_url") or "")
            sub_url = str(wbi_img.get("sub_url") or "")
            if img_url and sub_url:
                ik = img_url.rsplit("/", 1)[-1].split(".")[0]
                sk = sub_url.rsplit("/", 1)[-1].split(".")[0]
                self._wbi_keys = (ik, sk)

            if d.get("code") == 0 and data.get("isLogin"):
                self.user_info = {
                    "mid": data.get("mid"),
                    "uname": data.get("uname"),
                    "level": (data.get("level_info") or {}).get("current_level", 0),
                    "money": data.get("money", 0),
                    "face": data.get("face", ""),
                }
                return {"ok": True, "logged_in": True, "user": self.user_info}
        except Exception as exc:
            return {"ok": False, "logged_in": False, "error": str(exc)[:120], "user": {}}
        return {"ok": True, "logged_in": False, "user": {}}

    def logout(self) -> Dict[str, Any]:
        self.cookies.clear()
        self.user_info.clear()
        self.refresh_token = ""
        COOKIE_PATH.unlink(missing_ok=True)
        self._log("🔓 已退出并清理本机 B 站登录凭证")
        return {"ok": True}

    async def _ensure_wbi_keys(self) -> Tuple[str, str]:
        if self._wbi_keys[0] and self._wbi_keys[1]:
            return self._wbi_keys
        async with self._client(10.0) as cli:
            r = await cli.get("https://api.bilibili.com/x/web-interface/nav")
            data = (r.json().get("data") or {}).get("wbi_img") or {}
            ik = str(data.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
            sk = str(data.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
            if ik and sk:
                self._wbi_keys = (ik, sk)
        return self._wbi_keys

    # ==================== 2. 原生 B 站 UPOS 分片自动投稿 ====================
    async def upload_video_to_bilibili(
        self,
        payload: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.cookies.get("SESSDATA") or not self.cookies.get("bili_jct"):
            return {"ok": False, "error": "请先扫码登录 B 站账号后再发起投稿"}

        csrf = self.cookies["bili_jct"]
        v_name = Path(str(payload.get("video_filename") or "promo_longbao_v1.mp4")).name
        video_path = PROMO_DIR / v_name
        if not video_path.exists():
            return {"ok": False, "error": f"找不到成片文件: {video_path}"}

        def _up_prog(pct: int, msg: str):
            if task_id and task_id in self.upload_tasks:
                self.upload_tasks[task_id].update({"progress": pct, "stage_text": msg})

        _up_prog(5, "正在校验登录态并上传 16:9 高清封面...")
        cover_url = ""
        cover_cand = str(payload.get("cover_path") or "").strip()
        cover_path = Path(cover_cand) if cover_cand and Path(cover_cand).exists() else video_path.with_suffix(".jpg")

        async with self._client(60.0) as cli:
            if cover_path.exists():
                b64_cov = "data:image/jpeg;base64," + base64.b64encode(cover_path.read_bytes()).decode("ascii")
                cr = await cli.post(
                    "https://member.bilibili.com/x/vu/web/cover/up",
                    data={"cover": b64_cov, "csrf": csrf},
                    headers={"Referer": "https://member.bilibili.com/platform/upload/video/frame"},
                )
                cd = cr.json()
                if cd.get("code") == 0:
                    cover_url = cd["data"]["url"]

            _up_prog(15, "正在向 B 站创作中心申请 UPOS 高速分片通道...")
            fsize = video_path.stat().st_size
            pre_params = {
                "name": video_path.name,
                "size": str(fsize),
                "r": "upos",
                "profile": "ugcupos/bup",
                "ssl": "0",
                "version": "2.14.0",
                "build": "2140000",
                "upcdn": payload.get("upload_line") if payload.get("upload_line") not in (None, "", "auto") else "bda2",
                "probe_version": "20221109",
            }
            pr = await cli.get(
                "https://member.bilibili.com/preupload",
                params=pre_params,
                headers={"Referer": "https://member.bilibili.com/platform/upload/video/frame"},
            )
            pre = pr.json()
            if pre.get("OK") != 1:
                return {"ok": False, "error": f"申请上传通道失败: {pre}"}

            endpoint = "https:" + str(pre["endpoint"])
            upos_uri = str(pre["upos_uri"]).replace("upos://", "")
            auth = str(pre["auth"])
            biz_id = pre["biz_id"]
            chunk_size = int(pre.get("chunk_size") or 4 * 1024 * 1024)
            up_base = f"{endpoint}/{upos_uri}"
            up_headers = {"X-Upos-Auth": auth, "User-Agent": BILI_UA}

            ir = await cli.post(f"{up_base}?uploads&output=json", headers=up_headers)
            init_data = ir.json()
            upload_id = init_data["upload_id"]
            bili_filename = Path(upos_uri).stem

            total_chunks = math.ceil(fsize / chunk_size)
            parts_info = []
            with video_path.open("rb") as vf:
                for idx in range(total_chunks):
                    chunk = vf.read(chunk_size)
                    start = idx * chunk_size
                    end = start + len(chunk)
                    q = {
                        "partNumber": idx + 1,
                        "uploadId": upload_id,
                        "chunk": idx,
                        "chunks": total_chunks,
                        "size": len(chunk),
                        "start": start,
                        "end": end,
                        "total": fsize,
                    }
                    ur = await cli.put(f"{up_base}", params=q, content=chunk, headers=up_headers)
                    if ur.status_code not in (200, 201, 204):
                        return {"ok": False, "error": f"分片 {idx+1}/{total_chunks} 上传失败: HTTP {ur.status_code}"}
                    parts_info.append({"partNumber": idx + 1, "eTag": "etag"})
                    pct = 20 + int(65 * (idx + 1) / total_chunks)
                    _up_prog(pct, f"正在高速上传视频分片 ({idx+1}/{total_chunks})...")

            _up_prog(88, "分片上传完成，正在合并校验并提交稿件信息...")
            fin_q = {
                "output": "json",
                "name": video_path.name,
                "profile": "ugcupos/bup",
                "uploadId": upload_id,
                "biz_id": biz_id,
            }
            fr = await cli.post(f"{up_base}", params=fin_q, json={"parts": parts_info}, headers=up_headers)
            if fr.json().get("OK") != 1:
                return {"ok": False, "error": f"UPOS 合片失败: {fr.text[:160]}"}

            tags_raw = payload.get("tags") or ["AI直播", "虚拟主播", "2D骨骼", "独立开发", "人工智能"]
            tag_str = ",".join(str(t).strip() for t in tags_raw if str(t).strip())[:200]
            title = str(payload.get("title") or "我给AI娘做了58部件活体骨骼！会自己眨眼斗嘴、还能记住每个人的名字！")[:80]
            submit_body: Dict[str, Any] = {
                "copyright": int(payload.get("copyright") or 1),
                "videos": [{"filename": bili_filename, "title": video_path.stem, "desc": "", "cid": biz_id}],
                "tid": int(payload.get("tid") or 231),
                "title": title,
                "tag": tag_str,
                "desc_format_id": 0,
                "desc": str(payload.get("desc") or "")[:2000],
                "dynamic": str(payload.get("dynamic") or "")[:230],
                "cover": cover_url,
                "up_close_reply": False,
                "up_selection_reply": False,
                "up_close_danmu": False,
            }
            if int(submit_body["copyright"]) == 2:
                submit_body["source"] = str(payload.get("source") or "https://www.bilibili.com")

            ik, sk = await self._ensure_wbi_keys()
            add_params = sign_wbi_params({"csrf": csrf}, ik, sk) if (ik and sk) else {"csrf": csrf}
            sr = await cli.post(
                "https://member.bilibili.com/x/vu/web/add/v3",
                params=add_params,
                json=submit_body,
                headers={"Referer": "https://member.bilibili.com/platform/upload/video/frame"},
            )
            sd = sr.json()
            if sd.get("code") != 0:
                err = f"稿件投递返回错误 ({sd.get('code')}): {sd.get('message')}"
                self._log(f"⚠️ {err}")
                return {"ok": False, "error": err}

            bvid = sd["data"].get("bvid", "")
            aid = sd["data"].get("aid", 0)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO upload_history (bvid, aid, title, video_filename, created_at) VALUES (?, ?, ?, ?, ?)",
                    (bvid, aid, title, video_path.name, int(time.time())),
                )
            _up_prog(100, f"🎉 投稿成功！稿件 BV 号：{bvid}")
            self._log(f"🎉 自动投稿成功！《{title}》 -> {bvid}")
            return {"ok": True, "bvid": bvid, "aid": aid, "url": f"https://www.bilibili.com/video/{bvid}"}

    def start_upload_task(self, payload: Dict[str, Any]) -> str:
        tid = f"up_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        self.upload_tasks[tid] = {"ok": True, "task_id": tid, "status": "running", "progress": 1, "stage_text": "准备上传..."}

        async def _run():
            try:
                res = await self.upload_video_to_bilibili(payload, task_id=tid)
                if res.get("ok"):
                    self.upload_tasks[tid].update({"status": "done", "progress": 100, **res})
                else:
                    self.upload_tasks[tid].update({"status": "failed", "stage_text": res.get("error", "失败"), **res})
            except Exception as exc:
                self.upload_tasks[tid].update({"status": "failed", "error": str(exc)[:200], "stage_text": f"异常: {exc}"})

        asyncio.create_task(_run())
        return tid

    async def generate_bili_meta_via_llm(self, summary: str = "") -> Dict[str, Any]:
        api_key = getattr(self.engine, "llm_api_key", "")
        base_url = getattr(self.engine, "llm_base_url", "https://bbying.me/v1").rstrip("/")
        model = getattr(self.engine, "llm_model", "deepseek-chat")
        if api_key:
            prompt = (
                "为『阳光小院 2D 58部件活体骨骼 AI 娘互动直播间（支持弹幕红蓝辩论PK、小心心长期记忆库、龙宝配音、现已开源）』生成一套高吸引力的 B 站投稿元数据。"
                f"补充信息：{summary}。只返回 JSON 对象，包含 title(<=45字)、desc(120字左右)、tags(8个短标签数组)。"
            )
            try:
                async with httpx.AsyncClient(timeout=15.0, trust_env=False) as cli:
                    r = await cli.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.75},
                    )
                if r.status_code == 200:
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    i0, i1 = raw.find("{"), raw.rfind("}")
                    if i0 != -1 and i1 > i0:
                        obj = json.loads(raw[i0 : i1 + 1])
                        return {"ok": True, **obj}
            except Exception:
                pass
        return {
            "ok": True,
            "title": "开源了！我给AI娘做了58部件活体骨骼，会眨眼斗嘴还能记住每个观众！",
            "desc": "推开阳光小院的小木门！由白龙团长「龙宝」带大家逛逛我们全栈自研并开源的 2D 活体 AI 直播系统：\n★ 8 位性格各异的 58 部件弹簧骨骼 AI 轮流值班\n★ 弹幕实时触发红蓝 PK 投票与观众出题 3 人接力吐槽\n★ 独立长期记忆库 + 三大原生桌面控制面板互联互通！",
            "tags": ["AI直播", "虚拟主播", "开源项目", "2D骨骼", "AI娘", "独立开发", "人工智能", "大模型"],
        }

    # ==================== 3. B站 AI 智能托管（自动看视频写笔记 & 评论区陪聊） ====================
    async def _llm_chat(self, sys_prompt: str, user_prompt: str, max_len: int = 80) -> str:
        api_key = getattr(self.engine, "llm_api_key", "")
        base_url = getattr(self.engine, "llm_base_url", "https://bbying.me/v1").rstrip("/")
        model = getattr(self.engine, "llm_model", "deepseek-chat")
        if api_key:
            try:
                async with httpx.AsyncClient(timeout=16.0, trust_env=False) as cli:
                    r = await cli.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": sys_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0.7,
                        },
                    )
                if r.status_code == 200:
                    txt = r.json()["choices"][0]["message"]["content"].strip().strip('"').strip("“”")
                    return txt[:max_len]
            except Exception:
                pass
        return ""

    def _in_active_hours(self) -> bool:
        now_hm = time.strftime("%H:%M")
        st = str(self.agent_cfg.get("active_start") or "09:00")
        ed = str(self.agent_cfg.get("active_end") or "23:30")
        return st <= now_hm <= ed

    def _today_counts(self) -> Tuple[int, int]:
        day_start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
        with sqlite3.connect(DB_PATH) as conn:
            w = conn.execute("SELECT COUNT(*) FROM watched_videos WHERE watched_at >= ?", (day_start,)).fetchone()[0]
            c = conn.execute("SELECT COUNT(*) FROM commented_bvids WHERE commented_at >= ?", (day_start,)).fetchone()[0]
            r = conn.execute("SELECT COUNT(*) FROM replied_rpids WHERE replied_at >= ?", (day_start,)).fetchone()[0]
        return int(w), int(c + r)

    async def _fetch_video_subtitle_snippet(self, cli: httpx.AsyncClient, aid: int, cid: int) -> str:
        if not aid or not cid:
            return ""
        try:
            ik, sk = await self._ensure_wbi_keys()
            q = sign_wbi_params({"aid": aid, "cid": cid}, ik, sk) if (ik and sk) else {"aid": aid, "cid": cid}
            r = await cli.get("https://api.bilibili.com/x/player/wbi/v2", params=q)
            sub_list = (((r.json().get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
            if sub_list:
                sub_url = str(sub_list[0].get("subtitle_url") or "")
                if sub_url.startswith("//"):
                    sub_url = "https:" + sub_url
                if sub_url:
                    sr = await cli.get(sub_url)
                    body = sr.json().get("body") or []
                    lines = [str(it.get("content") or "") for it in body[:35]]
                    return " ".join(lines)[:600]
        except Exception:
            pass
        return ""

    async def _patrol_own_video_comments(self, cli: httpx.AsyncClient) -> None:
        if not self.agent_cfg.get("reply_own_comments", True):
            return
        mid = self.user_info.get("mid")
        csrf = self.cookies.get("bili_jct")
        if not mid or not csrf:
            return

        ik, sk = await self._ensure_wbi_keys()
        q = sign_wbi_params({"mid": mid, "ps": 5, "pn": 1}, ik, sk) if (ik and sk) else {"mid": mid, "ps": 5, "pn": 1}
        r = await cli.get("https://api.bilibili.com/x/space/wbi/arc/search", params=q)
        vlist = (((r.json().get("data") or {}).get("list") or {}).get("vlist")) or []

        for v in vlist[:3]:
            aid = v.get("aid")
            bvid = v.get("bvid", "")
            vtitle = v.get("title", "")
            if not aid:
                continue
            rr = await cli.get("https://api.bilibili.com/x/v2/reply", params={"type": 1, "oid": aid, "sort": 0, "ps": 10})
            replies = ((rr.json().get("data") or {}).get("replies")) or []
            for rp in replies:
                rpid = str(rp.get("rpid_str") or rp.get("rpid") or "")
                sender_mid = (rp.get("member") or {}).get("mid")
                sender_name = (rp.get("member") or {}).get("uname", "小伙伴")
                msg = ((rp.get("content") or {}).get("message") or "").strip()
                if not rpid or not msg or str(sender_mid) == str(mid):
                    continue
                with sqlite3.connect(DB_PATH) as conn:
                    exists = conn.execute("SELECT 1 FROM replied_rpids WHERE rpid=?", (rpid,)).fetchone()
                if exists:
                    continue

                _, c_cnt = self._today_counts()
                if c_cnt >= int(self.agent_cfg.get("daily_comment_limit") or 10):
                    return

                persona = "你是阳光小院的白龙团长「龙宝」，语气软萌真诚、礼貌暖心，绝不刷屏打广告。"
                reply_txt = await self._llm_chat(
                    persona,
                    f"粉丝「{sender_name}」在我们的视频《{vtitle}》下评论说：『{msg}』。请写一句 12~40 字的真诚暖心回复：",
                    max_len=65,
                )
                if not reply_txt:
                    reply_txt = f"谢谢 {sender_name} 的支持呀！龙宝拿小本本认真记下来啦~"

                send_r = await cli.post(
                    "https://api.bilibili.com/x/v2/reply/add",
                    data={"oid": aid, "type": 1, "root": rpid, "parent": rpid, "message": reply_txt, "plat": 1, "csrf": csrf},
                )
                sd = send_r.json()
                if sd.get("code") == 0:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO replied_rpids (rpid, bvid, reply_text, replied_at) VALUES (?, ?, ?, ?)",
                            (rpid, bvid, reply_txt, int(time.time())),
                        )
                    self._log(f"💬 已暖心回复自己稿件 [{bvid}] 粉丝 @{sender_name}：{reply_txt}")
                    return
                elif self.agent_cfg.get("auto_circuit_break", True):
                    self._log(f"🛑 触发防风控熔断：回复评论返回 code={sd.get('code')} ({sd.get('message')})，已自动暂停托管！")
                    self.running = False
                    return

    async def _watch_one_recommended_video(self, cli: httpx.AsyncClient) -> None:
        if not self.agent_cfg.get("enable_watch", True):
            return
        w_cnt, c_cnt = self._today_counts()
        if w_cnt >= int(self.agent_cfg.get("daily_watch_limit") or 20):
            self._log("🛡️ 今日 AI 观影配额已达安全上限，进入待机保护")
            return

        ik, sk = await self._ensure_wbi_keys()
        q = sign_wbi_params({"ps": 12, "fresh_type": 4}, ik, sk) if (ik and sk) else {"ps": 12}
        r = await cli.get("https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd", params=q)
        items = ((r.json().get("data") or {}).get("item")) or []

        kws = [k.strip().lower() for k in str(self.agent_cfg.get("keywords") or "").split(",") if k.strip()]
        chosen = None
        with sqlite3.connect(DB_PATH) as conn:
            for it in items:
                bvid = str(it.get("bvid") or "")
                if not bvid:
                    continue
                if conn.execute("SELECT 1 FROM watched_videos WHERE bvid=?", (bvid,)).fetchone():
                    continue
                title_l = str(it.get("title") or "").lower()
                if not kws or any(k in title_l for k in kws):
                    chosen = it
                    break
            if chosen is None:
                for it in items:
                    bvid = str(it.get("bvid") or "")
                    if bvid and not conn.execute("SELECT 1 FROM watched_videos WHERE bvid=?", (bvid,)).fetchone():
                        chosen = it
                        break

        if not chosen:
            return

        bvid = str(chosen["bvid"])
        vr = await cli.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
        vdata = vr.json().get("data") or {}
        aid = int(vdata.get("aid") or chosen.get("id") or 0)
        cid = int(vdata.get("cid") or chosen.get("cid") or 0)
        title = str(vdata.get("title") or chosen.get("title") or bvid)
        desc = str(vdata.get("desc") or "")[:300]
        up_name = str((vdata.get("owner") or {}).get("name") or "")

        sub_txt = await self._fetch_video_subtitle_snippet(cli, aid, cid)
        stay_sec = random.randint(
            max(15, int(self.agent_cfg.get("watch_min_sec") or 40)),
            max(35, int(self.agent_cfg.get("watch_max_sec") or 110)),
        )
        self._log(f"👀 正在观看学习 [{bvid}]《{title[:26]}》(UP:{up_name})，模拟停留 {stay_sec}s...")

        # 分片睡眠以便随时响应一键急停
        for _ in range(stay_sec):
            if not self.running:
                return
            await asyncio.sleep(1.0)

        csrf = self.cookies.get("bili_jct", "")
        if aid and cid and csrf:
            try:
                await cli.post(
                    "https://api.bilibili.com/x/click-interface/web/heartbeat",
                    data={"aid": aid, "cid": cid, "bvid": bvid, "played_time": stay_sec, "csrf": csrf},
                )
            except Exception:
                pass

        note = await self._llm_chat(
            "你是技术与内容学习助手，请用 1~2 句话精炼总结该视频的核心看点笔记。",
            f"视频标题：《{title}》\n简介：{desc}\n字幕片段：{sub_txt}",
            max_len=140,
        ) or f"已观看《{title}》，记录关键创作灵感与互动节奏。"

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO watched_videos (bvid, aid, title, up_name, note, watched_at) VALUES (?, ?, ?, ?, ?, ?)",
                (bvid, aid, title, up_name, note, int(time.time())),
            )
        self._log(f"📝 观影笔记 [{bvid}]：{note}")

        # 生成真诚互动短评（默认进入人工审批草稿箱，绝不乱发刷屏！）
        if self.agent_cfg.get("generate_ext_comments", True) and aid:
            with sqlite3.connect(DB_PATH) as conn:
                already = conn.execute("SELECT 1 FROM commented_bvids WHERE bvid=?", (bvid,)).fetchone()
            if already and self.agent_cfg.get("dedup_bvid", True):
                return

            sys_p = str(self.agent_cfg.get("system_prompt") or "")
            draft_txt = await self._llm_chat(
                sys_p,
                f"视频标题：《{title}》\nUP主：{up_name}\n内容摘要：{note}\n请写一条 15~40 字自然真诚的观众评论：",
                max_len=60,
            )
            if draft_txt:
                mode = str(self.agent_cfg.get("ext_comment_mode") or "approval_queue")
                if mode == "auto_strict" and c_cnt < int(self.agent_cfg.get("daily_comment_limit") or 10) and csrf:
                    sr = await cli.post(
                        "https://api.bilibili.com/x/v2/reply/add",
                        data={"oid": aid, "type": 1, "message": draft_txt, "plat": 1, "csrf": csrf},
                    )
                    if sr.json().get("code") == 0:
                        with sqlite3.connect(DB_PATH) as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO commented_bvids (bvid, content, commented_at) VALUES (?, ?, ?)",
                                (bvid, draft_txt, int(time.time())),
                            )
                        self._log(f"✅ 已发送走心评论 [{bvid}]：{draft_txt}")
                else:
                    did = f"d_{int(time.time())}_{uuid.uuid4().hex[:4]}"
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            "INSERT INTO comment_drafts (id, bvid, aid, video_title, content, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                            (did, bvid, aid, title, draft_txt, int(time.time())),
                        )
                    self._log(f"🛡️ 已生成待审批评论草稿 [{bvid}]，请在面板点「批准发送」后发出")

    async def _agent_loop(self) -> None:
        self._log("🚀 B 站 AI 智能托管巡航已启动（强制国内直连 + 防风控护盾生效中）")
        while self.running:
            try:
                if not self.cookies.get("SESSDATA"):
                    self._log("⚠️ 尚未登录 B 站账号，托管暂停等待扫码登录...")
                    self.running = False
                    break
                if not self._in_active_hours():
                    self._log("🌙 当前处于设定的深夜休眠保护时段，静默休眠中...")
                else:
                    async with self._client(15.0) as cli:
                        await self._patrol_own_video_comments(cli)
                        if self.running:
                            await self._watch_one_recommended_video(cli)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log(f"⚠️ 巡航单轮异常: {str(exc)[:100]}")

            cd = random.randint(
                max(45, int(self.agent_cfg.get("cooldown_min_sec") or 90)),
                max(90, int(self.agent_cfg.get("cooldown_max_sec") or 240)),
            )
            for _ in range(cd):
                if not self.running:
                    break
                await asyncio.sleep(1.0)
        self._log("⏹️ B 站 AI 智能托管已安全停止")

    def start_agent(self) -> Dict[str, Any]:
        if self.running:
            return {"ok": True, "running": True}
        self.running = True
        self._agent_task = asyncio.create_task(self._agent_loop())
        return {"ok": True, "running": True}

    def stop_agent(self) -> Dict[str, Any]:
        self.running = False
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
        self._agent_task = None
        return {"ok": True, "running": False}

    async def approve_draft(self, draft_id: str, override_content: str = "") -> Dict[str, Any]:
        csrf = self.cookies.get("bili_jct", "")
        if not csrf:
            return {"ok": False, "error": "请先扫码登录 B 站"}
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT bvid, aid, video_title, content FROM comment_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "找不到该草稿"}
        bvid, aid, vtitle, saved_txt = row
        final_txt = (override_content or saved_txt).strip()
        async with self._client(12.0) as cli:
            r = await cli.post(
                "https://api.bilibili.com/x/v2/reply/add",
                data={"oid": aid, "type": 1, "message": final_txt, "plat": 1, "csrf": csrf},
            )
            d = r.json()
        if d.get("code") != 0:
            return {"ok": False, "error": f"B 站返回 ({d.get('code')}): {d.get('message')}"}

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE comment_drafts SET status='sent', content=? WHERE id=?", (final_txt, draft_id))
            conn.execute(
                "INSERT OR REPLACE INTO commented_bvids (bvid, content, commented_at) VALUES (?, ?, ?)",
                (bvid, final_txt, int(time.time())),
            )
        self._log(f"✅ 人工审批通过并成功发送评论 [{bvid}]《{vtitle[:18]}》：{final_txt}")
        return {"ok": True}

    def reject_draft(self, draft_id: str) -> Dict[str, Any]:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT bvid FROM comment_drafts WHERE id=?", (draft_id,)).fetchone()
            conn.execute("UPDATE comment_drafts SET status='rejected' WHERE id=?", (draft_id,))
            if row and row[0]:
                conn.execute(
                    "INSERT OR REPLACE INTO commented_bvids (bvid, content, commented_at) VALUES (?, '[ignored]', ?)",
                    (row[0], int(time.time())),
                )
        return {"ok": True}

    def get_status(self) -> Dict[str, Any]:
        w_cnt, c_cnt = self._today_counts()
        with sqlite3.connect(DB_PATH) as conn:
            draft_rows = conn.execute(
                "SELECT id, bvid, video_title, content, created_at FROM comment_drafts WHERE status='pending' ORDER BY created_at DESC LIMIT 15"
            ).fetchall()
            note_rows = conn.execute(
                "SELECT bvid, title, up_name, note, watched_at FROM watched_videos ORDER BY watched_at DESC LIMIT 12"
            ).fetchall()
            up_rows = conn.execute(
                "SELECT bvid, aid, title, video_filename, created_at FROM upload_history ORDER BY id DESC LIMIT 8"
            ).fetchall()

        drafts = [
            {"id": r[0], "bvid": r[1], "video_title": r[2], "content": r[3], "ts": time.strftime("%H:%M", time.localtime(r[4]))}
            for r in draft_rows
        ]
        notes = [
            {"bvid": r[0], "title": r[1], "up_name": r[2], "note": r[3], "ts": time.strftime("%m-%d %H:%M", time.localtime(r[4]))}
            for r in note_rows
        ]
        uploads = [
            {"bvid": r[0], "aid": r[1], "title": r[2], "video_filename": r[3], "ts": time.strftime("%m-%d %H:%M", time.localtime(r[4]))}
            for r in up_rows
        ]
        return {
            "ok": True,
            "running": self.running,
            "logged_in": bool(self.cookies.get("SESSDATA")),
            "user": self.user_info,
            "config": self.agent_cfg,
            "today_watched": w_cnt,
            "today_commented": c_cnt,
            "drafts": drafts,
            "notes": notes,
            "uploads": uploads,
            "logs": self.logs[-25:],
        }
