"""SQLite 数据层（M1）。

表结构见 SPEC.md「数据层契约」，字段名一字不改。
所有函数都接收 db_path 参数，不维护全局连接，方便 GUI 线程与监听线程共用。
"""

from __future__ import annotations

import os
import sqlite3
import time

# SQLite 文件名
DB_FILENAME = "giftthanks.db"

# gifts 表除 id/ts 外的全部列（顺序与 SPEC 一致）
_GIFT_COLUMNS = (
    "session_id",
    "event_type",
    "uid",
    "uname",
    "face_url",
    "gift_name",
    "gift_id",
    "num",
    "total_coin",
    "coin_type",
    "rmb",
    "guard_level",
    "sc_message",
    "gift_img_url",
    "card_path",
)


def get_data_dir() -> str:
    """数据目录：Windows 下 %APPDATA%/GiftThanks/，无 APPDATA 则回退用户主目录下 .GiftThanks/"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "GiftThanks")
    return os.path.join(os.path.expanduser("~"), ".GiftThanks")


def _connect(db_path: str) -> sqlite3.Connection:
    """打开连接（dict 行工厂，外键打开）"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    """建库建表（幂等）"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                room_id INTEGER,
                live_time TEXT,
                start_ts REAL,
                end_ts REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gifts (
                id INTEGER PRIMARY KEY,
                session_id INTEGER,
                ts REAL,
                event_type TEXT,
                uid INTEGER,
                uname TEXT,
                face_url TEXT,
                gift_name TEXT,
                gift_id INTEGER,
                num INTEGER,
                total_coin INTEGER,
                coin_type TEXT,
                rmb REAL,
                guard_level INTEGER,
                sc_message TEXT,
                gift_img_url TEXT,
                card_path TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                uname TEXT,
                face_url TEXT,
                face_path TEXT,
                updated_ts REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gifts_session ON gifts(session_id)")
        conn.commit()


def start_session(db_path: str, room_id: int, live_time: str) -> int:
    """开播新场次，返回 session_id；若存在同房间 end_ts IS NULL 且 live_time 相同的会话则复用"""
    now = time.time()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE room_id = ? AND end_ts IS NULL AND live_time = ? "
            "ORDER BY id DESC LIMIT 1",
            (room_id, live_time),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO sessions (room_id, live_time, start_ts, end_ts) VALUES (?, ?, ?, NULL)",
            (room_id, live_time, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def end_session(db_path: str, session_id: int) -> None:
    """下播：写入 end_ts（幂等，已结束的场次不会覆盖原结束时间）"""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET end_ts = ? WHERE id = ? AND end_ts IS NULL",
            (time.time(), session_id),
        )
        conn.commit()


def insert_gift(db_path: str, record: dict) -> int:
    """插入一条礼物/SC/上舰事件，返回礼物主键 id。

    record 支持的键即 gifts 表除 id 外的列；ts 缺省取当前时间。
    """
    record = dict(record)
    record.setdefault("ts", time.time())
    columns = ["ts", *[c for c in _GIFT_COLUMNS if c in record]]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO gifts ({', '.join(columns)}) VALUES ({placeholders})"
    values = [record[c] for c in columns]
    with _connect(db_path) as conn:
        cur = conn.execute(sql, values)
        conn.commit()
        return int(cur.lastrowid)


def get_session_gifts(db_path: str, session_id: int) -> list[dict]:
    """取某场次全部礼物事件（按时间升序），每行为含全部列的 dict"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gifts WHERE session_id = ? ORDER BY ts ASC, id ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_session(db_path: str, room_id: int) -> dict | None:
    """取该房间最近一场会话（不管是否结束），无则 None"""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE room_id = ? ORDER BY id DESC LIMIT 1",
            (room_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def upsert_user(db_path: str, uid: int, uname: str, face_url: str) -> None:
    """记录/更新用户信息；face_path 只在 userinfo 回填时更新，此处不覆盖"""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (uid, uname, face_url, face_path, updated_ts)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(uid) DO UPDATE SET
                uname = excluded.uname,
                face_url = CASE
                    WHEN excluded.face_url IS NOT NULL AND excluded.face_url != ''
                    THEN excluded.face_url ELSE users.face_url END,
                updated_ts = excluded.updated_ts
            """,
            (uid, uname, face_url, time.time()),
        )
        conn.commit()


def get_users_missing_face(db_path: str, session_id: int) -> list[int]:
    """该场次送礼用户中 face_path 为空的 uid 列表（去重）"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT g.uid AS uid FROM gifts g
            LEFT JOIN users u ON u.uid = g.uid
            WHERE g.session_id = ? AND g.uid IS NOT NULL AND g.uid != 0
              AND (u.face_path IS NULL OR u.face_path = '')
            """,
            (session_id,),
        ).fetchall()
        return [int(r["uid"]) for r in rows]


def set_user_face_path(db_path: str, uid: int, face_path: str) -> None:
    """回填本地头像路径"""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET face_path = ?, updated_ts = ? WHERE uid = ?",
            (face_path, time.time(), uid),
        )
        conn.commit()


def set_gift_card_path(db_path: str, gift_pk: int, path: str) -> None:
    """回填礼物卡片 PNG 本地路径（listener 调 render_gift_card 成功后调用）"""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE gifts SET card_path = ? WHERE id = ?",
            (path, gift_pk),
        )
        conn.commit()
