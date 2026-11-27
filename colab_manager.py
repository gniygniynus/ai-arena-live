# -*- coding: utf-8 -*-
"""
Colab 95GB 防掉线自动保活、隧道自愈、官方 OAuth 账号管理 & 一键恢复引擎 (colab_manager.py · v10 加固版)
核心增强：
1. 与 tts_service 熔断器实时双向联动：探测云端离线立刻触发熔断让舞台发言 0ms 瞬切本机 8766，恢复在线立刻解锁满血 1.7B；
2. 复用持久化 HTTP 连接池，消除每 25 秒巡检频繁建连开销，严格隔离本地 127.0.0.1 (trust_env=False) 与 Google OAuth 代理；
3. colab_account.json 采用 .tmp + os.replace 原子写入，防止意外中断损坏凭证文件。
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

import httpx

from tts_service import mark_colab_offline, reset_colab_circuit

BASE_DIR = Path(__file__).parent
ACCOUNT_CFG_PATH = BASE_DIR / "colab_account.json"
VOICE_REFS_DIR = BASE_DIR / "static" / "voice_refs"
CLOUD_SCRIPT_PATH = BASE_DIR / "colab" / "cloud_studio_server.py"

COLAB_GAPI_BASE = "https://colab.pa.googleapis.com"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
# 仅请求标准免审核公开身份权限 (openid/profile/email)，使 Google Cloud 点击「发布应用 (Publish App)」无需填写任何官方审核资料即可秒切正式版，全网任意 Google 账号通用不弹 403！
COLAB_OAUTH_SCOPES = [
    "openid",
    "profile",
    "email",
]


class ColabManager:
    def __init__(
        self,
        engine: Any,
        broadcast_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self.engine = engine
        self.broadcast = broadcast_fn
        self._running = False
        self._task: Optional[asyncio.Task[Any]] = None
        self._local_client: Optional[httpx.AsyncClient] = None
        self._direct_client: Optional[httpx.AsyncClient] = None
        self._proxy_client: Optional[httpx.AsyncClient] = None

        self.account: Dict[str, Any] = {
            "google_email": "",
            "oauth_client_id": "",
            "oauth_client_secret": "",
            "oauth_access_token": "",
            "oauth_refresh_token": "",
            "oauth_expires_at": 0,
            "cf_tunnel_token": "",
            "colab_notebook_url": "https://colab.research.google.com/",
            "proxy_url": "",
            "auto_reconnect": True,
            "auto_downgrade": True,
            "idle_auto_shutdown": False,
            "preferred_gpu_queue": ["G4", "A100", "L4", "T4"],
            "remote_admin_url": os.getenv("COLAB_REMOTE_URL", "").strip().rstrip("/"),
            "remote_admin_token": os.getenv("COLAB_ADMIN_TOKEN", "").strip(),
        }
        self._load_account()

        self.cu_info: Dict[str, Any] = {
            "email": self.account.get("google_email", ""),
            "cu_balance": None,
            "cu_rate_per_hour": None,
            "tier": "未授权 Google OAuth",
            "active_runtimes": 0,
            "available_gpus": ["G4", "A100", "L4", "T4"],
            "source": "unlinked",
        }
        self.health_cache: Dict[str, Any] = {"connected": False}
        self.local_cloner_health: Dict[str, Any] = {"connected": False}
        self.last_check_ts: float = 0.0
        self.reconnect_count: int = 0
        self.logs: List[Dict[str, Any]] = []
        self._oauth_pending: Dict[str, Dict[str, Any]] = {}

    def _log(self, level: str, msg: str) -> None:
        item = {"ts": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.logs.append(item)
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]

    def _load_account(self) -> None:
        if ACCOUNT_CFG_PATH.exists():
            try:
                saved = json.loads(ACCOUNT_CFG_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.account.update(saved)
            except Exception:
                pass
        if not self.account.get("remote_admin_url") and os.getenv("COLAB_REMOTE_URL"):
            self.account["remote_admin_url"] = os.getenv("COLAB_REMOTE_URL", "").strip().rstrip("/")
        if not self.account.get("remote_admin_token") and os.getenv("COLAB_ADMIN_TOKEN"):
            self.account["remote_admin_token"] = os.getenv("COLAB_ADMIN_TOKEN", "").strip()

    def _save_account(self) -> None:
        tmp = BASE_DIR / "colab_account.json.tmp"
        tmp.write_text(json.dumps(self.account, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, ACCOUNT_CFG_PATH)

    def public_account_view(self) -> Dict[str, Any]:
        return {
            "google_email": self.account.get("google_email", ""),
            "has_oauth_client": bool(self.account.get("oauth_client_id")),
            "has_oauth_token": bool(self.account.get("oauth_access_token") or self.account.get("oauth_refresh_token")),
            "has_cf_tunnel_token": bool(self.account.get("cf_tunnel_token")),
            "colab_notebook_url": self.account.get("colab_notebook_url", "https://colab.research.google.com/"),
            "proxy_url": self.account.get("proxy_url", ""),
            "detected_proxy": self._detect_local_http_proxy() or "",
            "auto_reconnect": bool(self.account.get("auto_reconnect", True)),
            "auto_downgrade": bool(self.account.get("auto_downgrade", True)),
            "idle_auto_shutdown": bool(self.account.get("idle_auto_shutdown", False)),
            "preferred_gpu_queue": self.account.get("preferred_gpu_queue", ["G4", "A100", "L4", "T4"]),
            "remote_admin_url": self.account.get("remote_admin_url", ""),
            "has_remote_admin_token": bool(self.account.get("remote_admin_token")),
        }

    def update_account(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        secret_fields = ("oauth_client_secret", "oauth_access_token", "oauth_refresh_token", "cf_tunnel_token", "remote_admin_token")
        for k, v in patch.items():
            if k not in self.account:
                continue
            if k in secret_fields:
                sv = str(v or "").strip()
                if sv and not sv.startswith("***"):
                    self.account[k] = sv
            elif isinstance(v, str):
                self.account[k] = v.strip().rstrip("/") if "url" in k else v.strip()
            else:
                self.account[k] = v
        if self.account.get("google_email"):
            self.cu_info["email"] = self.account["google_email"]
        self._save_account()
        self._log("info", "已保存 Colab 账号与自动保活恢复配置")
        return self.public_account_view()

    @classmethod
    def _get_proxy_candidates(cls, manual_proxy: str = "") -> List[str]:
        """按可靠性优先级收集本机可用 HTTP 代理候选：
        1. 用户在面板显式填写的 proxy_url
        2. Windows 注册表实时系统代理 (Internet Settings -> ProxyEnable/ProxyServer，跟随 Clash 系统代理开关实时变化)
        3. 本机常见代理端口实测开放列表 (7897, 7890, 10809, 10808, 7891)
        4. 进程环境变量 HTTPS_PROXY / HTTP_PROXY（放最后以防残留失效端口如 2080 劫持）
        """
        import socket
        raw_list: List[str] = []

        def _norm(p: str) -> str:
            s = (p or "").strip()
            if not s:
                return ""
            if "=" in s:
                # 兼容 Windows 注册表形如 "http=127.0.0.1:7897;https=127.0.0.1:7897"
                for part in s.split(";"):
                    if part.startswith("https=") or part.startswith("http="):
                        s = part.split("=", 1)[1].strip()
                        break
            if not s.startswith(("http://", "https://", "socks5://")):
                s = f"http://{s}"
            return s.rstrip("/")

        if manual_proxy:
            raw_list.append(_norm(manual_proxy))

        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                ) as k:
                    enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
                    if int(enabled) == 1:
                        server, _ = winreg.QueryValueEx(k, "ProxyServer")
                        if server:
                            raw_list.append(_norm(str(server)))
            except Exception:
                pass

        for port in (7897, 7890, 10809, 10808, 7891):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.15)
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        raw_list.append(f"http://127.0.0.1:{port}")
            except Exception:
                pass

        for env_k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            val = os.getenv(env_k, "").strip()
            if val:
                raw_list.append(_norm(val))

        seen = set()
        out: List[str] = []
        for item in raw_list:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def _detect_local_http_proxy(self) -> Optional[str]:
        cands = self._get_proxy_candidates(str(self.account.get("proxy_url") or ""))
        return cands[0] if cands else None

    def _get_client(self, mode: str) -> httpx.AsyncClient:
        if mode == "local":
            if self._local_client is None or self._local_client.is_closed:
                self._local_client = httpx.AsyncClient(timeout=4.0, trust_env=False)
            return self._local_client
        if self._direct_client is None or self._direct_client.is_closed:
            self._direct_client = httpx.AsyncClient(timeout=15.0, trust_env=False)
        return self._direct_client

    async def test_google_connectivity(self, override_proxy: str = "") -> Dict[str, Any]:
        """主动实测本机代理连通 Google OAuth 服务器情况，供桌面面板一键诊断。"""
        manual = override_proxy.strip() or str(self.account.get("proxy_url") or "").strip()
        cands = self._get_proxy_candidates(manual)
        errors: List[str] = []
        t0 = time.perf_counter()
        for px in cands:
            try:
                t_st = time.perf_counter()
                tout = httpx.Timeout(connect=3.5, read=8.0, write=8.0, pool=3.5)
                async with httpx.AsyncClient(proxy=px, trust_env=False, timeout=tout) as cli:
                    r = await cli.post(GOOGLE_TOKEN_URL, data={})
                ms = int((time.perf_counter() - t_st) * 1000)
                return {
                    "ok": True,
                    "proxy_used": px,
                    "status_code": r.status_code,
                    "latency_ms": ms,
                    "candidates": cands,
                    "message": f"连通成功！经 {px} 抵达 Google 授权节点 ({ms}ms, HTTP {r.status_code})",
                }
            except Exception as exc:
                errors.append(f"{px} -> {type(exc).__name__}")
        return {
            "ok": False,
            "proxy_used": "",
            "candidates": cands,
            "errors": errors,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": "所有候选代理均无法连通 Google: " + ("; ".join(errors) if errors else "未检测到开放的本地代理端口"),
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._watchdog_loop())
        self._log("info", "Colab 掉线守护看门狗已启动（每 25 秒自动巡检 + 本地同源克隆 0ms 熔断接管）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        for cli in (self._local_client, self._direct_client, self._proxy_client):
            if cli is not None and not cli.is_closed:
                try:
                    await cli.aclose()
                except Exception:
                    pass
        self._local_client = None
        self._direct_client = None
        self._proxy_client = None

    async def _req(self, method: str, url: str, timeout: float = 10.0, **kwargs: Any) -> httpx.Response:
        is_google = "googleapis.com" in url or "google.com" in url
        cands = self._get_proxy_candidates(str(self.account.get("proxy_url") or ""))
        if is_google:
            # 针对 Google 域名：实时轮询本机开放代理候选（短连接超时 4s 快速跳过死端口），绝不回退国内直连傻等 20s 掩盖真实错误！
            errors: List[str] = []
            for px in cands:
                try:
                    tout = httpx.Timeout(connect=4.0, read=max(8.0, timeout), write=10.0, pool=4.0)
                    async with httpx.AsyncClient(proxy=px, trust_env=False, timeout=tout) as cli:
                        return await cli.request(method, url, **kwargs)
                except Exception as exc:
                    errors.append(f"{px}({type(exc).__name__})")
            # 若没有任何本地代理候选，最后才用 4s 快速尝试一次直连（兼容海外云服务器环境）
            if not cands:
                tout = httpx.Timeout(connect=4.0, read=max(8.0, timeout), write=10.0, pool=4.0)
                async with httpx.AsyncClient(trust_env=False, timeout=tout) as cli:
                    return await cli.request(method, url, **kwargs)
            raise RuntimeError("代理候选均不可达 [" + ", ".join(errors) + "]")

        # 非 Google 域名（如 trycloudflare 隧道）：优先直连，失败再尝试最优代理
        last_exc: Optional[Exception] = None
        try:
            return await self._get_client("direct").request(method, url, timeout=timeout, **kwargs)
        except Exception as exc:
            last_exc = exc
        for px in cands[:2]:
            try:
                tout = httpx.Timeout(connect=4.0, read=max(8.0, timeout), write=10.0, pool=4.0)
                async with httpx.AsyncClient(proxy=px, trust_env=False, timeout=tout) as cli:
                    return await cli.request(method, url, **kwargs)
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("HTTP request failed")

    # ==================== 官方 Google OAuth 2.0 浏览器回环登录 ====================
    def import_google_client_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        data = json.loads(raw_bytes.decode("utf-8"))
        node = data.get("installed") or data.get("web") or data
        cid = str(node.get("client_id") or "").strip()
        csec = str(node.get("client_secret") or "").strip()
        if not cid:
            return {"ok": False, "error": "JSON 中未找到 client_id，请确认是 Google OAuth 桌面客户端凭据文件"}
        self.account["oauth_client_id"] = cid
        if csec:
            self.account["oauth_client_secret"] = csec
        self._save_account()
        self._log("ok", "已一键导入 Google OAuth 客户端凭据，可直接点击官方登录按钮授权！")
        return {"ok": True, "has_oauth_client": True}

    def start_official_oauth_flow(self, base_url: str = "http://127.0.0.1:8765") -> Dict[str, Any]:
        cid = str(self.account.get("oauth_client_id") or "").strip()
        if not cid:
            return {
                "ok": False,
                "need_client_json": True,
                "error": "首次启用官方浏览器自动登录需先导入 1 份 Google OAuth 桌面客户端 JSON（免费 30 秒创建，之后永久自动登录续期）",
            }
        now = time.time()
        for k in list(self._oauth_pending.keys()):
            if now - self._oauth_pending[k]["created_at"] > 600:
                self._oauth_pending.pop(k, None)

        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        redirect_uri = f"{base_url.rstrip('/')}/api/colab/oauth/callback"

        self._oauth_pending[state] = {
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "created_at": now,
        }
        q = urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(COLAB_OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "include_granted_scopes": "true",
        })
        auth_url = f"{GOOGLE_AUTH_URL}?{q}"
        self._log("info", "已发起官方 Google OAuth2 浏览器登录授权流程，等待回调自动提取凭证...")
        return {"ok": True, "auth_url": auth_url, "redirect_uri": redirect_uri}

    async def complete_official_oauth_callback(self, code: str, state: str) -> Dict[str, Any]:
        pending = self._oauth_pending.pop(state, None)
        if not pending:
            return {"ok": False, "error": "登录会话已过期或 state 无效，请重新点击登录按钮"}

        cid = str(self.account.get("oauth_client_id") or "").strip()
        csec = str(self.account.get("oauth_client_secret") or "").strip()
        form = {
            "client_id": cid,
            "code": code,
            "code_verifier": pending["verifier"],
            "grant_type": "authorization_code",
            "redirect_uri": pending["redirect_uri"],
        }
        if csec:
            form["client_secret"] = csec

        try:
            r = await self._req("POST", GOOGLE_TOKEN_URL, timeout=20.0, data=form)
        except Exception as exc:
            err_msg = f"连接 Google 授权服务器失败（请确认本机代理如 Clash 7897 已开启）：{type(exc).__name__}: {str(exc)[:140]}"
            self._log("warn", err_msg)
            return {"ok": False, "error": err_msg}
        if r.status_code != 200:
            self._log("warn", f"Google OAuth 换取 Token 失败: HTTP {r.status_code} {r.text[:120]}")
            return {"ok": False, "error": f"Token 换取失败 (HTTP {r.status_code}): {r.text[:180]}"}

        try:
            d = r.json()
        except Exception as exc:
            return {"ok": False, "error": f"Google 返回数据解析异常: {exc}"}
        tok = d.get("access_token", "")
        if not tok:
            return {"ok": False, "error": "Google 未返回 access_token"}
        self.account["oauth_access_token"] = tok
        if d.get("refresh_token"):
            self.account["oauth_refresh_token"] = d["refresh_token"]
        self.account["oauth_expires_at"] = int(time.time() + int(d.get("expires_in", 3500)))
        self._save_account()

        cu = await self.fetch_google_cu_info()
        self._log("ok", f"Google 官方登录成功！已自动提取账号 {cu.get('email') or '已授权'}，CU 余额：{cu.get('cu_balance')}")
        return {"ok": True, "email": cu.get("email", ""), "cu_info": cu}

    async def _get_valid_oauth_token(self) -> str:
        tok = str(self.account.get("oauth_access_token") or "").strip()
        exp = float(self.account.get("oauth_expires_at") or 0)
        ref = str(self.account.get("oauth_refresh_token") or "").strip()
        cid = str(self.account.get("oauth_client_id") or "").strip()
        csec = str(self.account.get("oauth_client_secret") or "").strip()

        if ref and cid and csec and (not tok or time.time() > exp - 120):
            try:
                r = await self._req(
                    "POST",
                    GOOGLE_TOKEN_URL,
                    timeout=12.0,
                    data={
                        "client_id": cid,
                        "client_secret": csec,
                        "refresh_token": ref,
                        "grant_type": "refresh_token",
                    },
                )
                if r.status_code == 200:
                    d = r.json()
                    tok = d.get("access_token", "")
                    if tok:
                        self.account["oauth_access_token"] = tok
                        self.account["oauth_expires_at"] = int(time.time() + int(d.get("expires_in", 3500)))
                        self._save_account()
                        self._log("ok", "已通过 Refresh Token 自动续期 Google OAuth 凭证")
            except Exception as exc:
                self._log("warn", f"刷新 Google OAuth Token 失败: {str(exc)[:80]}")
        return tok

    async def fetch_google_cu_info(self) -> Dict[str, Any]:
        tok = await self._get_valid_oauth_token()
        if not tok:
            self.cu_info["source"] = "unlinked"
            self.cu_info["email"] = self.account.get("google_email", "")
            return self.cu_info

        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        try:
            ur = await self._req("GET", GOOGLE_USERINFO_URL, timeout=10.0, headers=headers)
            if ur.status_code == 200:
                u = ur.json()
                if u.get("email"):
                    self.account["google_email"] = u["email"]
                    self.cu_info["email"] = u["email"]
                    self._save_account()
        except Exception:
            pass

        try:
            cr = await self._req(
                "GET",
                f"{COLAB_GAPI_BASE}/v1/user-info?get_ccu_consumption_info=true",
                timeout=12.0,
                headers=headers,
            )
            if cr.status_code == 200:
                raw = cr.text.lstrip()
                if raw.startswith(")]}'"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[4:]
                data = json.loads(raw)
                paid = data.get("paidComputeUnitsBalance")
                tier = data.get("subscriptionTier", "PRO")
                elig = data.get("eligibleAccelerators") or []
                gpus = []
                for item in elig:
                    for m in (item.get("models") or []):
                        if m not in gpus:
                            gpus.append(m)
                self.cu_info.update({
                    "cu_balance": round(float(paid), 1) if paid is not None else self.cu_info.get("cu_balance"),
                    "tier": str(tier),
                    "available_gpus": gpus or ["G4", "A100", "L4", "T4"],
                    "source": "google_oauth_live",
                })
                self._log("ok", f"已实时同步 Colab 官方 CU 余额: {self.cu_info['cu_balance']} CU ({tier})")
            else:
                self._log("warn", f"Colab 官方 CU 接口返回 HTTP {cr.status_code}（需确认 OAuth 包含 colaboratory 权限）")
        except Exception as exc:
            self._log("warn", f"查询官方 CU 余额异常: {str(exc)[:90]}")
        return self.cu_info

    # ==================== 云端/本机双巡检与熔断自愈联动 ====================
    async def check_local_cloner(self) -> Dict[str, Any]:
        url = (self.engine.local_genie_url or "http://127.0.0.1:8766").rstrip("/")
        try:
            r = await self._get_client("local").get(f"{url}/health", timeout=3.0)
            if r.status_code == 200:
                d = r.json()
                self.local_cloner_health = {"connected": True, "url": url, **d}
                return self.local_cloner_health
        except Exception:
            pass
        self.local_cloner_health = {"connected": False, "url": url}
        return self.local_cloner_health

    async def notify_local_cloner_reload(self) -> None:
        """同步新参考干声后尝试通知本机 8766 热更新声纹缓存（兼容旧进程静默忽略）"""
        url = (self.engine.local_genie_url or "http://127.0.0.1:8766").rstrip("/")
        try:
            await self._get_client("local").post(f"{url}/reload_refs", timeout=6.0)
        except Exception:
            pass

    async def probe_cloud_studio(self) -> Dict[str, Any]:
        if not self.engine.colab_url:
            self.health_cache = {"connected": False, "message": "未配置 Colab 语音隧道"}
            mark_colab_offline(30.0, "unconfigured")
            return self.health_cache
        try:
            r = await self._req("GET", f"{self.engine.colab_url.rstrip('/')}/health", timeout=6.0)
            if r.status_code == 200:
                d = r.json()
                self.health_cache = {"connected": True, "colab_url": self.engine.colab_url, **d}
                self.cu_info["active_runtimes"] = 1
                reset_colab_circuit()
                return self.health_cache
            self.health_cache = {"connected": False, "message": f"HTTP {r.status_code}"}
            mark_colab_offline(30.0, f"probe_http_{r.status_code}")
        except Exception as exc:
            self.health_cache = {"connected": False, "message": str(exc)[:100]}
            mark_colab_offline(30.0, "probe_failed")
        self.cu_info["active_runtimes"] = 0
        return self.health_cache

    async def recover_cloud_via_admin(self) -> Dict[str, Any]:
        admin_url = str(self.account.get("remote_admin_url") or "").strip().rstrip("/")
        admin_tok = str(self.account.get("remote_admin_token") or "").strip()
        if not admin_url or not admin_tok:
            return {"ok": False, "reason": "未配置远程管理基座地址 (COLAB_REMOTE_URL / TOKEN)"}

        headers = {"Authorization": f"Bearer {admin_tok}"}
        self._log("info", "正在尝试通过远程管理基座自动恢复 95GB 云端语音工坊与隧道...")
        try:
            hr = await self._req("GET", f"{admin_url}/health", timeout=7.0)
            if hr.status_code != 200:
                self._log("warn", f"远程管理基座不可达 (HTTP {hr.status_code})，当前 Colab 运行时已到期关闭")
                return {"ok": False, "reason": "Colab 运行时已到期回收，已自动由本机实时克隆引擎接管"}

            if CLOUD_SCRIPT_PATH.exists():
                await self._req(
                    "POST",
                    f"{admin_url}/admin/upload",
                    timeout=30.0,
                    headers=headers,
                    data={"path": "/content/ai_live_cloud/cloud_studio_server.py"},
                    files={"file": ("cloud_studio_server.py", CLOUD_SCRIPT_PATH.read_bytes(), "text/x-python")},
                )

            for wav_file in VOICE_REFS_DIR.glob("*_baby_ref.wav"):
                await self._req(
                    "POST",
                    f"{admin_url}/admin/upload",
                    timeout=20.0,
                    headers=headers,
                    data={"path": f"/content/ai_live_cloud/voice_refs/{wav_file.name}"},
                    files={"file": (wav_file.name, wav_file.read_bytes(), "audio/wav")},
                )

            cf_tok = str(self.account.get("cf_tunnel_token") or "").strip()
            tunnel_cmd = (
                f'setsid cloudflared tunnel run --token "{cf_tok}" </dev/null >/content/ai_live_cloud/studio_tunnel.log 2>&1 &'
                if cf_tok
                else 'setsid cloudflared tunnel --url http://127.0.0.1:7862 --no-autoupdate </dev/null >/content/ai_live_cloud/studio_tunnel.log 2>&1 &'
            )
            sh = (
                "#!/bin/bash\n"
                "pkill -f 'http://127.0.0.1:7862' || true\n"
                "if ! pgrep -f cloud_studio_server.py >/dev/null; then\n"
                f'  export STUDIO_PORT=7862 STUDIO_TOKEN="{admin_tok}" STUDIO_WORK_ROOT="/content/ai_live_cloud"\n'
                "  setsid python3 -u /content/ai_live_cloud/cloud_studio_server.py </dev/null >/content/ai_live_cloud/studio.log 2>&1 &\n"
                "fi\n"
                f"{tunnel_cmd}\n"
                "echo RECOVER_LAUNCHED\n"
            )
            await self._req(
                "POST",
                f"{admin_url}/admin/upload",
                timeout=15.0,
                headers=headers,
                data={"path": "/content/ai_live_cloud/recover_studio.sh"},
                files={"file": ("recover_studio.sh", sh.encode("utf-8"), "text/x-shellscript")},
            )
            await self._req(
                "POST", f"{admin_url}/admin/exec", timeout=20.0, headers=headers,
                json={"cmd": "bash /content/ai_live_cloud/recover_studio.sh", "timeout": 15},
            )
            await asyncio.sleep(5.0)

            chk = await self._req(
                "POST",
                f"{admin_url}/admin/exec",
                timeout=15.0,
                headers=headers,
                json={"cmd": r'grep -oE "https://[a-zA-Z0-9-]+\.trycloudflare\.com" /content/ai_live_cloud/studio_tunnel.log | tail -n 1', "timeout": 10},
            )
            out = chk.json().get("stdout", "").strip()
            m = re.search(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", out)
            if m:
                new_url = m.group(1)
                self.engine.update_config(
                    self.engine.llm_base_url,
                    "",
                    self.engine.llm_model,
                    colab_url=new_url,
                    colab_token=admin_tok,
                    colab_tts_model="qwen3-tts-1.7b",
                    use_colab_tts=True,
                    save_env=True,
                )
                reset_colab_circuit()
                self.reconnect_count += 1
                self._log("ok", f"自愈恢复成功！已自动重连新隧道 {new_url} 并同步 8 位宝宝原声！")
                await self.probe_cloud_studio()
                await self.broadcast(self.engine.snapshot())
                return {"ok": True, "new_url": new_url}
            return {"ok": True, "message": "已重启服务（若使用固定域名请稍候 3 秒刷新）"}
        except Exception as exc:
            self._log("warn", f"自动恢复失败（已由本机实时克隆引擎无感兜底）: {str(exc)[:100]}")
            return {"ok": False, "reason": str(exc)[:140]}

    async def _watchdog_loop(self) -> None:
        await asyncio.sleep(2.0)
        fail_streak = 0
        while self._running:
            try:
                self.last_check_ts = time.time()
                await self.check_local_cloner()
                ch = await self.probe_cloud_studio()
                if ch.get("connected"):
                    fail_streak = 0
                else:
                    fail_streak += 1
                    if fail_streak == 1:
                        self._log("warn", "检测到云端语音隧道失联，已触发熔断并由本机 0.6B 克隆引擎 0ms 无缝接管！")
                    if fail_streak in (2, 6) and self.account.get("auto_reconnect", True):
                        res = await self.recover_cloud_via_admin()
                        if res.get("ok"):
                            fail_streak = 0
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(25.0)

    def get_dashboard_state(self) -> Dict[str, Any]:
        return {
            "account": self.public_account_view(),
            "cu_info": self.cu_info,
            "cloud_health": self.health_cache,
            "local_cloner": self.local_cloner_health,
            "reconnect_count": self.reconnect_count,
            "last_check_ts": int(self.last_check_ts),
            "logs": self.logs[-18:],
        }
