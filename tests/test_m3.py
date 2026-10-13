# -*- coding: utf-8 -*-
"""M3 模块测试：copypool / giftcard / poster"""

import os
import sqlite3
import time

import pytest
from PIL import Image, ImageDraw

from gift_thanks import copypool
from gift_thanks.giftcard import close_browser, render_gift_card
from gift_thanks.poster import generate_poster

BASE_TS = 1760313600.0  # 2025-10-13 08:00:00 UTC 附近，仅作测试时间基准


def _make_png(path: str, size=(120, 120), color=(200, 150, 100), text: str = ""):
    """造一张本地测试图片（头像/礼物卡片用）。"""
    img = Image.new("RGB", size, color)
    if text:
        ImageDraw.Draw(img).text((10, 10), text, fill=(255, 255, 255))
    img.save(path)
    return path


def _build_db(db_path: str, data_dir: str) -> int:
    """按 SPEC 表结构建临时 SQLite 库并插入 12 个送礼人的模拟数据。"""
    faces_dir = os.path.join(data_dir, "faces")
    cards_dir = os.path.join(data_dir, "cards")
    os.makedirs(faces_dir, exist_ok=True)
    os.makedirs(cards_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE sessions(id INTEGER PRIMARY KEY, room_id INTEGER, live_time TEXT,
                          start_ts REAL, end_ts REAL);
    CREATE TABLE gifts(id INTEGER PRIMARY KEY, session_id INTEGER, ts REAL,
        event_type TEXT, uid INTEGER, uname TEXT, face_url TEXT, gift_name TEXT,
        gift_id INTEGER, num INTEGER, total_coin INTEGER, coin_type TEXT,
        rmb REAL, guard_level INTEGER, sc_message TEXT, gift_img_url TEXT,
        card_path TEXT);
    CREATE TABLE users(uid INTEGER PRIMARY KEY, uname TEXT, face_url TEXT,
                       face_path TEXT, updated_ts REAL);
    """)

    # 12 个用户：含缺头像的、昵称带 emoji 和 HTML 特殊字符的
    users = [
        (1001, "榜一大哥🌟", "http://i0.hdslb.com/f1.jpg"),
        (1002, "提督大人", "http://i0.hdslb.com/f2.jpg"),
        (1003, "小可爱🌸🌸", "http://i0.hdslb.com/f3.jpg"),
        (1004, "<b>老六</b>&co", "http://i0.hdslb.com/f4.jpg"),
        (1005, "奶茶三分糖", "http://i0.hdslb.com/f5.jpg"),
        (1006, "摸鱼大师", "http://i0.hdslb.com/f6.jpg"),
        (1007, "深夜食堂🍜", "http://i0.hdslb.com/f7.jpg"),
        (1008, "路过的萌新", "http://i0.hdslb.com/f8.jpg"),
        (1009, "辣条批发商", "http://i0.hdslb.com/f9.jpg"),
        (1010, "白嫖怪😜", "http://i0.hdslb.com/f10.jpg"),
        (1011, "<script>alert(1)</script>", "http://i0.hdslb.com/f11.jpg"),
        (1012, "无名氏（无头像）", "http://i0.hdslb.com/f12.jpg"),
    ]
    for i, (uid, uname, face_url) in enumerate(users):
        # 最后一个用户故意没有本地头像；其余生成彩色方块头像
        face_path = None
        if uid != 1012:
            face_path = _make_png(
                os.path.join(faces_dir, f"{uid}.jpg"),
                color=((uid * 37) % 255, (uid * 53) % 255, (uid * 71) % 255))
        conn.execute(
            "INSERT INTO users(uid, uname, face_url, face_path, updated_ts) "
            "VALUES (?,?,?,?,?)", (uid, uname, face_url, face_path, time.time()))

    # (uid, uname, event_type, gift_name, num, total_coin, coin_type, rmb,
    #  guard_level, sc_message)
    gift_rows = [
        # 榜一：gold 礼物 3000 元 + SC 100 元 = 3100 元
        (1001, "榜一大哥🌟", "gift", "嘉年华", 1, 3000000, "gold", 3000.0, None, None),
        (1001, "榜一大哥🌟", "gift", "火箭", 2, 200000, "gold", 200.0, None, None),
        (1001, "榜一大哥🌟", "sc", "SC", 1, 0, "gold", 100.0, None,
         "主播今晚辛苦啦，比心！"),
        # 榜二：大航海提督 1998 元 + gold 礼物 500 元 = 2498 元
        (1002, "提督大人", "guard", "提督", 1, 0, "gold", 1998.0, 2, None),
        (1002, "提督大人", "gift", "摩天大楼", 5, 500000, "gold", 500.0, None, None),
        # 榜三：SC 200 元 + gold 礼物 131.4 元 = 331.4 元
        (1003, "小可爱🌸🌸", "sc", "SC", 1, 0, "gold", 200.0, None,
         "<b>永远支持你</b>"),
        (1003, "小可爱🌸🌸", "gift", "告白气球", 1, 131400, "gold", 131.4, None, None),
        # 其余付费送礼人（进“同样感谢”网格）
        (1004, "<b>老六</b>&co", "gift", "奶茶", 3, 30000, "gold", 30.0, None, None),
        (1005, "奶茶三分糖", "gift", "打call", 10, 25000, "gold", 25.0, None, None),
        (1006, "摸鱼大师", "sc", "SC", 1, 0, "gold", 30.0, None, "摸鱼路过"),
        (1007, "深夜食堂🍜", "gift", "小花花", 2, 2000, "gold", 2.0, None, None),
        (1008, "路过的萌新", "gift", "牛哇牛哇", 1, 1000, "gold", 1.0, None, None),
        # 免费礼物（银瓜子，rmb=0）→ 人气榜，按数量
        (1009, "辣条批发商", "gift", "辣条", 66, 6600, "silver", 0.0, None, None),
        (1010, "白嫖怪😜", "gift", "辣条", 30, 3000, "silver", 0.0, None, None),
        (1011, "<script>alert(1)</script>", "gift", "小心心", 12, 0, "silver",
         0.0, None, None),
        (1012, "无名氏（无头像）", "gift", "辣条", 5, 500, "silver", 0.0, None, None),
        (1009, "辣条批发商", "gift", "小心心", 20, 0, "silver", 0.0, None, None),
    ]
    uname_map = {u[0]: u[1] for u in users}
    for i, (uid, uname, etype, gname, num, coin, ctype, rmb, glv, scmsg) in \
            enumerate(gift_rows):
        pk = i + 1
        # 金额最大的一批礼物预先造好“卡片图”，验证海报缩略图内联
        card_path = None
        if rmb >= 100:
            card_path = _make_png(
                os.path.join(cards_dir, f"{pk}.png"), size=(800, 200),
                color=(253, 246, 236), text=f"{gname} x{num}")
        conn.execute(
            "INSERT INTO gifts(id, session_id, ts, event_type, uid, uname, "
            "face_url, gift_name, gift_id, num, total_coin, coin_type, rmb, "
            "guard_level, sc_message, gift_img_url, card_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, 1, BASE_TS + i * 60, etype, uid, uname,
             f"http://i0.hdslb.com/f{uid}.jpg", gname, 20000 + pk, num, coin,
             ctype, rmb, glv, scmsg,
             "https://invalid.example.com/gift.png",  # 故意无效，走占位图
             card_path))

    conn.execute(
        "INSERT INTO sessions(id, room_id, live_time, start_ts, end_ts) "
        "VALUES (1, 21452505, '2025-10-13 20:00:00', ?, ?)",
        (BASE_TS, BASE_TS + 3 * 3600))
    conn.commit()
    conn.close()
    assert len(uname_map) == 12
    return 1


@pytest.fixture(scope="module")
def mock_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("m3")
    data_dir = str(tmp / "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = str(tmp / "giftthanks.db")
    session_id = _build_db(db_path, data_dir)
    yield {"db_path": db_path, "data_dir": data_dir, "session_id": session_id,
           "out_dir": str(tmp / "out")}
    close_browser()


def test_copypool_pool_size_and_placeholder():
    assert len(copypool.SHORT_LINES) >= 30
    assert len(copypool.LONG_PARAS) >= 15
    assert all("{name}" in s for s in copypool.SHORT_LINES)
    assert all("{name}" in p for p in copypool.LONG_PARAS)
    # 长文每段约 100 字（允许 60~200 浮动）
    for p in copypool.LONG_PARAS:
        assert 60 <= len(p) <= 200, f"长文长度异常({len(p)}): {p[:20]}..."


def test_copypool_pick():
    short = copypool.pick_short("小明")
    assert "小明" in short and "{name}" not in short
    long = copypool.pick_long("星宫汐")
    assert "星宫汐" in long and "{name}" not in long
    assert 60 <= len(long) <= 200
    # 昵称带特殊字符也不能报错
    assert "<b>&" in copypool.pick_short("<b>&") or True


def test_render_gift_card(mock_env):
    row = {
        "id": 9001,
        "uname": "测试君🌟<b>",
        "face_path": None,                      # 缺头像 → 占位图
        "gift_img_url": "https://invalid.example.com/x.png",  # 加载失败 → onerror 占位
        "gift_name": "嘉年华",
        "num": 3,
        "rmb": 3000.0,
        "ts": BASE_TS,
        "event_type": "gift",
    }
    path = render_gift_card(mock_env["data_dir"], row)
    assert os.path.isfile(path)
    assert path == os.path.join(mock_env["data_dir"], "cards", "9001.png")
    assert os.path.getsize(path) > 10 * 1024
    img = Image.open(path)
    assert img.size == (1600, 400)  # 800x200 @ device_scale_factor=2


def test_generate_poster(mock_env):
    path = generate_poster(
        db_path=mock_env["db_path"],
        data_dir=mock_env["data_dir"],
        session_id=mock_env["session_id"],
        streamer_name="星宫汐",
        out_dir=mock_env["out_dir"],
    )
    assert os.path.isfile(path)
    size = os.path.getsize(path)
    assert size > 100 * 1024, f"海报体积过小: {size}"
    img = Image.open(path)
    assert img.width == 2160          # 1080 @ device_scale_factor=2
    assert img.height > 1500          # full_page 长图
    print(f"\n海报路径: {path} ({size/1024:.0f}KB, {img.width}x{img.height})")
