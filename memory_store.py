# -*- coding: utf-8 -*-
"""
八位 AI 娘独立持久化记忆库 (memory_store.py · v14 SQLite WAL + 零延迟内存镜像版)
核心职责：
1. 每位角色拥有彼此隔离的专属记忆空间（记录谁给她送了「小心心」等礼物、累计小心心数、最近聊天梗与见面时间）；
2. 读操作 100% 走内存镜像（0ms 零磁盘 I/O 抖动），写操作同步更新内存镜像 + SQLite WAL 事务持久化，重启服务永久不丢记忆；
3. 提供严格按舞台从左到右顺位（stage_slots 索引升序）筛选「认识该观众的在场 AI」查询能力！
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).parent
DEFAULT_DB_PATH = BASE_DIR / "character_memories.db"


def normalize_user_key(user: str) -> str:
    return (user or "").strip().lower()[:24]


class CharacterMemoryStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        # 内存镜像: {persona_id: {user_key: {user, hearts, gift_count, last_gift, last_note, first_ts, last_ts}}}
        self._mem: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._init_db()
        self._load_all_into_cache()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS character_fan_memory (
                        persona_id TEXT NOT NULL,
                        user_key   TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        hearts     INTEGER NOT NULL DEFAULT 1,
                        gift_count INTEGER NOT NULL DEFAULT 1,
                        last_gift  TEXT NOT NULL DEFAULT '小心心',
                        last_note  TEXT NOT NULL DEFAULT '',
                        first_ts   REAL NOT NULL,
                        last_ts    REAL NOT NULL,
                        PRIMARY KEY (persona_id, user_key)
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _load_all_into_cache(self) -> None:
        with self._lock:
            self._mem.clear()
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT persona_id, user_key, display_name, hearts, gift_count, last_gift, last_note, first_ts, last_ts "
                    "FROM character_fan_memory"
                )
                for row in cur.fetchall():
                    pid, ukey, dname, hearts, gcnt, lgift, lnote, fts, lts = row
                    self._mem.setdefault(pid, {})[ukey] = {
                        "user": dname,
                        "hearts": int(hearts),
                        "gift_count": int(gcnt),
                        "last_gift": lgift,
                        "last_note": lnote,
                        "first_ts": float(fts),
                        "last_ts": float(lts),
                    }
            finally:
                conn.close()

    def record_heart_gift(
        self,
        persona_id: str,
        user: str,
        hearts_delta: int = 1,
        gift_name: str = "小心心",
        note: str = "",
    ) -> Dict[str, Any]:
        """将观众赠送的小心心写入指定角色的专属记忆库（内存 + SQLite 双落盘）"""
        clean_user = (user or "热心抖友").strip()[:16] or "热心抖友"
        ukey = normalize_user_key(clean_user)
        if not ukey or not persona_id:
            return {}

        delta = max(1, int(hearts_delta))
        now = time.time()
        with self._lock:
            bucket = self._mem.setdefault(persona_id, {})
            existing = bucket.get(ukey)
            if existing:
                existing["user"] = clean_user
                existing["hearts"] = int(existing["hearts"]) + delta
                existing["gift_count"] = int(existing["gift_count"]) + 1
                existing["last_gift"] = gift_name[:20]
                if note:
                    existing["last_note"] = note[:40]
                existing["last_ts"] = now
                rec = dict(existing)
            else:
                rec = {
                    "user": clean_user,
                    "hearts": delta,
                    "gift_count": 1,
                    "last_gift": gift_name[:20],
                    "last_note": note[:40],
                    "first_ts": now,
                    "last_ts": now,
                }
                bucket[ukey] = rec

            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO character_fan_memory
                        (persona_id, user_key, display_name, hearts, gift_count, last_gift, last_note, first_ts, last_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(persona_id, user_key) DO UPDATE SET
                        display_name = excluded.display_name,
                        hearts       = excluded.hearts,
                        gift_count   = excluded.gift_count,
                        last_gift    = excluded.last_gift,
                        last_note    = CASE WHEN excluded.last_note != '' THEN excluded.last_note ELSE character_fan_memory.last_note END,
                        last_ts      = excluded.last_ts
                    """,
                    (
                        persona_id,
                        ukey,
                        rec["user"],
                        rec["hearts"],
                        rec["gift_count"],
                        rec["last_gift"],
                        rec["last_note"],
                        rec["first_ts"],
                        rec["last_ts"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return dict(rec)

    def update_last_note(self, persona_id: str, user: str, note: str) -> None:
        """如果认识该观众，顺便记住他刚刚聊过的一句话梗，下次重逢更有温度"""
        ukey = normalize_user_key(user)
        if not ukey or not note:
            return
        now = time.time()
        with self._lock:
            rec = self._mem.get(persona_id, {}).get(ukey)
            if not rec:
                return
            rec["last_note"] = note.strip()[:40]
            rec["last_ts"] = now
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE character_fan_memory SET last_note = ?, last_ts = ? WHERE persona_id = ? AND user_key = ?",
                    (rec["last_note"], now, persona_id, ukey),
                )
                conn.commit()
            finally:
                conn.close()

    def knows_user(self, persona_id: str, user: str) -> bool:
        ukey = normalize_user_key(user)
        if not ukey:
            return False
        with self._lock:
            rec = self._mem.get(persona_id, {}).get(ukey)
            return bool(rec and int(rec.get("hearts", 0)) >= 1)

    def get_fan_record(self, persona_id: str, user: str) -> Optional[Dict[str, Any]]:
        ukey = normalize_user_key(user)
        if not ukey:
            return None
        with self._lock:
            rec = self._mem.get(persona_id, {}).get(ukey)
            return dict(rec) if rec else None

    def get_familiar_on_stage(self, user: str, stage_slots: List[str]) -> List[str]:
        """严格按照舞台从左到右顺序 (stage_slots[0] -> stage_slots[1] -> stage_slots[2]) 返回认识该观众的在场角色"""
        ukey = normalize_user_key(user)
        if not ukey or not stage_slots:
            return []
        with self._lock:
            result: List[str] = []
            for pid in stage_slots:
                rec = self._mem.get(pid, {}).get(ukey)
                if rec and int(rec.get("hearts", 0)) >= 1:
                    result.append(pid)
            return result

    def delete_user_memory(self, user: str) -> int:
        """从内存镜像与 SQLite 中同步移除指定用户的记忆条目"""
        ukey = normalize_user_key(user)
        if not ukey:
            return 0
        removed = 0
        with self._lock:
            for bucket in self._mem.values():
                if ukey in bucket:
                    bucket.pop(ukey, None)
                    removed += 1
            conn = self._connect()
            try:
                conn.execute("DELETE FROM character_fan_memory WHERE user_key = ?", (ukey,))
                conn.commit()
            finally:
                conn.close()
        return removed

    def get_summary(self, persona_order: List[str], top_k: int = 8) -> Dict[str, Dict[str, Any]]:
        """供 snapshot() 零磁盘延迟读取，直接对接前端中控台 memory_summary 结构"""
        with self._lock:
            summary: Dict[str, Dict[str, Any]] = {}
            for pid in persona_order:
                fans = list(self._mem.get(pid, {}).values())
                fans.sort(key=lambda r: (int(r.get("hearts", 0)), float(r.get("last_ts", 0.0))), reverse=True)
                summary[pid] = {
                    "total_fans": len(fans),
                    "fans": [
                        {
                            "user": f["user"],
                            "hearts": int(f["hearts"]),
                            "last_gift": f.get("last_gift", "小心心"),
                            "last_note": f.get("last_note", ""),
                        }
                        for f in fans[:top_k]
                    ],
                }
            return summary
