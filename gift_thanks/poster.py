# -*- coding: utf-8 -*-
"""场次聚合 → 感谢海报 PNG（M3）

聚合 get_session_gifts：按 uid 汇总人民币金额（gold 礼物 + SC + 大航海），
排序取榜一/二/三；免费礼物（rmb=0）单独计人气榜（按数量）。
用 templates/poster.html 渲染，宽 1080px、full_page 截图、2x 分辨率。
"""

import datetime
import logging
import os
import sqlite3

from .copypool import pick_long, pick_short
from .giftcard import (PLACEHOLDER_FACE, _fmt_money, _jinja_env,
                       _render_html_to_png, file_to_data_uri)

logger = logging.getLogger(__name__)


def _get_session_gifts(db_path: str, session_id: int) -> list[dict]:
    """优先调用 M1 db.py 的 get_session_gifts；
    db.py 尚未合入时（单独开发分支）用等价 SQL 兜底。"""
    try:
        from .db import get_session_gifts  # type: ignore
        return get_session_gifts(db_path, session_id)
    except ImportError:
        logger.warning("db.py 不可用，使用内置 SQL 读取 gifts 表")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM gifts WHERE session_id = ? ORDER BY ts", (session_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _query_users(db_path: str) -> dict[int, dict]:
    """读取 users 表（uid -> {uname, face_url, face_path}），用于取本地头像。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT uid, uname, face_url, face_path FROM users")
        return {row["uid"]: dict(row) for row in cur.fetchall()}
    except sqlite3.Error as exc:  # users 表不存在等异常不阻塞海报生成
        logger.warning("读取 users 表失败: %s", exc)
        return {}
    finally:
        conn.close()


def _get_session_end_ts(db_path: str, session_id: int,
                        gifts: list[dict]) -> float:
    """下播时间：sessions.end_ts；为空则退化为最后一条礼物时间，再退化为现在。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT end_ts FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row and row[0]:
            return float(row[0])
    except sqlite3.Error as exc:
        logger.warning("读取 sessions 表失败: %s", exc)
    finally:
        conn.close()
    ts_list = [g.get("ts") for g in gifts if g.get("ts")]
    if ts_list:
        return float(max(ts_list))
    return datetime.datetime.now().timestamp()


def _fmt_date_cn(ts: float) -> str:
    """格式化为「2026年10月13日 22:30」。"""
    dt = datetime.datetime.fromtimestamp(ts)
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}"


def _face_data_uri(uid: int, users: dict[int, dict]) -> str:
    """本地头像转 base64 data URI；缺失用占位图。"""
    info = users.get(uid) or {}
    return file_to_data_uri(info.get("face_path") or "") or PLACEHOLDER_FACE


def _aggregate(gifts: list[dict]):
    """聚合礼物记录：
    返回 (金额榜列表, 人气榜列表)。
    金额榜元素: {uid, uname, rmb, lines:[{name,num}], best_row}
    人气榜元素: {uid, uname, count}
    """
    paid: dict[int, dict] = {}   # uid -> 聚合信息（rmb>0）
    free: dict[int, dict] = {}   # uid -> 免费礼物计数
    for g in gifts:
        uid = g.get("uid")
        if uid is None:
            continue
        rmb = float(g.get("rmb") or 0)
        num = int(g.get("num") or 1)
        uname = g.get("uname") or "神秘人"
        gift_name = g.get("gift_name") or ("SC" if g.get("event_type") == "sc" else "礼物")
        if rmb > 0:
            entry = paid.setdefault(uid, {"uid": uid, "uname": uname, "rmb": 0.0,
                                          "_detail": {}, "best_row": None})
            entry["rmb"] += rmb
            if uname and uname != "神秘人":
                entry["uname"] = uname
            det = entry["_detail"].setdefault(gift_name, {"name": gift_name,
                                                          "num": 0, "rmb": 0.0})
            det["num"] += num
            det["rmb"] += rmb
            if entry["best_row"] is None or rmb > float(
                    entry["best_row"].get("rmb") or 0):
                entry["best_row"] = g
        else:
            # 免费礼物（银瓜子）进人气榜，按数量
            entry = free.setdefault(uid, {"uid": uid, "uname": uname, "count": 0})
            entry["count"] += num
            if uname and uname != "神秘人":
                entry["uname"] = uname

    ranking = sorted(paid.values(), key=lambda e: (-e["rmb"], e["uid"]))
    for e in ranking:
        details = sorted(e.pop("_detail").values(),
                         key=lambda d: (-d["rmb"], d["name"]))
        e["lines"] = [{"name": d["name"], "num": d["num"]} for d in details[:8]]
        e["total_str"] = _fmt_money(e["rmb"])
    popular = sorted(free.values(), key=lambda e: (-e["count"], e["uid"]))
    return ranking, popular


def generate_poster(db_path: str, data_dir: str, session_id: int,
                    streamer_name: str, out_dir: str) -> str:
    """生成感谢海报 PNG，返回文件路径。"""
    gifts = _get_session_gifts(db_path, session_id)
    users = _query_users(db_path)
    end_ts = _get_session_end_ts(db_path, session_id, gifts)
    ranking, popular = _aggregate(gifts)

    # 榜一/二/三大卡片：头像+昵称+UID+送礼明细+总金额+随机感谢语+最大礼物卡片缩略图
    top3 = []
    for rank, e in enumerate(ranking[:3], start=1):
        best = e.get("best_row") or {}
        thumb = file_to_data_uri(best.get("card_path") or "")
        top3.append({
            "rank": rank,
            "uname": e["uname"],
            "uid": e["uid"],
            "face": _face_data_uri(e["uid"], users),
            "lines": e["lines"],
            "total_str": e["total_str"],
            "quote": pick_short(e["uname"]),
            "thumb": thumb,
        })

    # 「同样感谢」其余送礼人网格：头像+昵称+总额
    others = [{
        "uname": e["uname"],
        "face": _face_data_uri(e["uid"], users),
        "total_str": e["total_str"],
    } for e in ranking[3:]]

    # 免费礼物人气榜（按数量）
    popular_list = [{
        "uname": e["uname"],
        "face": _face_data_uri(e["uid"], users),
        "count": e["count"],
    } for e in popular]

    template = _jinja_env.get_template("poster.html")
    html_text = template.render(
        streamer_name=streamer_name,
        end_str=_fmt_date_cn(end_ts),
        top3=top3,
        others=others,
        popular=popular_list,
        long_text=pick_long(streamer_name),
        gen_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_rmb=_fmt_money(sum(e["rmb"] for e in ranking)),
        gift_count=len(gifts),
    )

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.fromtimestamp(end_ts).strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"poster_session{session_id}_{stamp}.png")
    _render_html_to_png(html_text, out_path, width=1080, height=1200,
                        full_page=True, scale=2)
    logger.info("感谢海报已生成: %s", out_path)
    return out_path
