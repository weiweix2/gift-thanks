# -*- coding: utf-8 -*-
"""端到端集成测试（集成阶段补）。

不 mock 任何内部模块：真实走
    init_db → start_session → listener 纯函数解析原始B站消息 → insert_gift
    → end_session → generate_poster 生成海报 PNG
并做 OWASP 轻量安全抽查：
    ① cred.bin 不落明文 SESSDATA
    ② SQL 注入昵称不毁表
    ③ 海报 HTML 昵称自动转义（XSS）
    ④ 日志不出现 SESSDATA
GUI 集成：offscreen 起 MainWindow，只替换 listener 的网络连接部分
（start/stop 协程），保留 _build_handler 事件分发与真实入库/渲染链路。

运行：QT_QPA_PLATFORM=offscreen python -m pytest tests/test_e2e.py -v
"""

import asyncio
import logging
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gift_thanks import auth, db, listener, poster
from gift_thanks import giftcard
from gift_thanks.db import (end_session, get_session_gifts, init_db,
                            insert_gift, start_session, upsert_user)
from gift_thanks.listener import parse_command
from gift_thanks.poster import generate_poster

ROOM_ID = 21452505
LIVE_TIME = "2025-10-13 20:00:00"

# ---------------------------------------------------------------------------
# 模拟的原始 B站弹幕消息（cmd + data 结构以 blivedm 收到的 JSON 为准）
# 共 15 条原始消息：14 条有效 + 1 条 COMBO_SEND（连击，必须忽略防重复计数）
# ---------------------------------------------------------------------------
SECRET_SESSDATA = "e2e_SECRET_sessdata_9f8e7d6c5b4a"  # 假凭证，仅用于泄漏检查

RAW_COMMANDS = [
    # --- U1 大佬🌟emoji：嘉年华 3000元 + COMBO 连击重复包 + 火箭×2 200元 ---
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2001, "uname": "大佬🌟emoji", "face": "http://i0.hdslb.com/f2001.jpg",
        "giftId": 30433, "giftName": "嘉年华", "num": 1,
        "coin_type": "gold", "total_coin": 3000000,
        "gift_info": {"img_basic": "https://invalid.example.com/carnival.png"}}},
    # 连击汇报包：与上一条同礼物同总额，必须被忽略（只算一次）
    {"cmd": "COMBO_SEND", "data": {
        "uid": 2001, "uname": "大佬🌟emoji", "face": "http://i0.hdslb.com/f2001.jpg",
        "giftId": 30433, "giftName": "嘉年华", "num": 1,
        "coin_type": "gold", "total_coin": 3000000, "combo_num": 1,
        "gift_info": {"img_basic": "https://invalid.example.com/carnival.png"}}},
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2001, "uname": "大佬🌟emoji", "face": "http://i0.hdslb.com/f2001.jpg",
        "giftId": 20001, "giftName": "火箭", "num": 2,
        "coin_type": "gold", "total_coin": 200000,
        "gift_info": {"img_basic": "https://invalid.example.com/rocket.png"}}},
    # --- U2 提督<script>alert(1)</script>：开通提督 1998元 + SC 500元 ---
    {"cmd": "GUARD_BUY", "data": {
        "uid": 2002, "username": "提督<script>alert(1)</script>",
        "guard_level": 2, "num": 1, "price": 1998000,
        "gift_id": 10003, "gift_name": "提督"}},
    {"cmd": "SUPER_CHAT_MESSAGE", "data": {
        "uid": 2002, "price": 500, "message": "主播辛苦啦<b>比心</b>",
        "user_info": {"uname": "提督<script>alert(1)</script>",
                      "face": "http://i0.hdslb.com/f2002.jpg", "guard_level": 2}}},
    # --- U3 奶茶妹妹：两条SC（30+30）+ 打call×10（25元）= 85元 ---
    {"cmd": "SUPER_CHAT_MESSAGE", "data": {
        "uid": 2003, "price": 30, "message": "今天也要开心",
        "user_info": {"uname": "奶茶妹妹", "face": "http://i0.hdslb.com/f2003.jpg",
                      "guard_level": 0}}},
    {"cmd": "SUPER_CHAT_MESSAGE", "data": {
        "uid": 2003, "price": 30, "message": "加油哦",
        "user_info": {"uname": "奶茶妹妹", "face": "http://i0.hdslb.com/f2003.jpg",
                      "guard_level": 0}}},
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2003, "uname": "奶茶妹妹", "face": "http://i0.hdslb.com/f2003.jpg",
        "giftId": 31036, "giftName": "打call", "num": 10,
        "coin_type": "gold", "total_coin": 25000,
        "gift_info": {"img_basic": "https://invalid.example.com/call.png"}}},
    # --- U4 白嫖怪😜（缺 face）：3 个免费小心心 + 辣条×66（银瓜子，不计金额）---
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2004, "uname": "白嫖怪😜", "face": "",
        "giftId": 30607, "giftName": "小心心", "num": 1,
        "coin_type": "silver", "total_coin": 0}},
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2004, "uname": "白嫖怪😜", "face": "",
        "giftId": 30607, "giftName": "小心心", "num": 1,
        "coin_type": "silver", "total_coin": 0}},
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2004, "uname": "白嫖怪😜", "face": "",
        "giftId": 30607, "giftName": "小心心", "num": 1,
        "coin_type": "silver", "total_coin": 0}},
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2004, "uname": "白嫖怪😜", "face": "",
        "giftId": 1, "giftName": "辣条", "num": 66,
        "coin_type": "silver", "total_coin": 6600,
        "gift_info": {"img_basic": "https://invalid.example.com/latiao.png"}}},
    # --- U5 无名氏：小花花×2（2元）+ 牛哇牛哇×1（1元）= 3元 ---
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2005, "uname": "无名氏", "face": "http://i0.hdslb.com/f2005.jpg",
        "giftId": 20005, "giftName": "小花花", "num": 2,
        "coin_type": "gold", "total_coin": 2000,
        "gift_info": {"img_basic": "https://invalid.example.com/flower.png"}}},
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2005, "uname": "无名氏", "face": "http://i0.hdslb.com/f2005.jpg",
        "giftId": 31039, "giftName": "牛哇牛哇", "num": 1,
        "coin_type": "gold", "total_coin": 1000,
        "gift_info": {"img_basic": "https://invalid.example.com/niuwa.png"}}},
    # --- U6 路过：免费小心心×5 ---
    {"cmd": "SEND_GIFT", "data": {
        "uid": 2006, "uname": "路过的甲板清洁工", "face": "http://i0.hdslb.com/f2006.jpg",
        "giftId": 30607, "giftName": "小心心", "num": 5,
        "coin_type": "silver", "total_coin": 0}},
    # --- 1 条完全不相关的命令，必须被忽略 ---
    {"cmd": "DANMU_MSG", "data": {"info": ["弹幕内容"]}},
]

# 手工核算的预期结果（COMBO_SEND 与 DANMU_MSG 不计）：
EXPECTED_RAW = 16                            # 原始消息条数
EXPECTED_INSERTED = 14                       # 入库条数
EXPECTED_PAID = {2001: 3200.0, 2002: 2498.0, 2003: 85.0, 2005: 3.0}
EXPECTED_FREE_COUNT = {2004: 69, 2006: 5}    # 人气榜按数量
EXPECTED_TOTAL_RMB = sum(EXPECTED_PAID.values())  # 5786.0


# ---------------------------------------------------------------------------
# 全流程端到端
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def e2e_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    data_dir = str(tmp / "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = str(tmp / "giftthanks.db")
    yield {"db_path": db_path, "data_dir": data_dir,
           "out_dir": str(tmp / "posters"), "tmp": tmp}
    giftcard.close_browser()


def test_full_pipeline_e2e(e2e_env):
    db_path, data_dir = e2e_env["db_path"], e2e_env["data_dir"]

    # ① 开播
    init_db(db_path)
    session_id = start_session(db_path, ROOM_ID, LIVE_TIME)
    assert isinstance(session_id, int) and session_id > 0

    # ② 解析 15 条原始消息并入库（COMBO_SEND / DANMU_MSG 应被忽略）
    inserted = []
    for raw in RAW_COMMANDS:
        parsed = parse_command(raw)
        if parsed is None:
            continue  # COMBO_SEND / 无关命令
        kind, record = parsed
        record["session_id"] = session_id
        pk = insert_gift(db_path, record)
        inserted.append((kind, pk))
        # listener 同款：顺便维护 users 表
        if record.get("uid"):
            upsert_user(db_path, record["uid"],
                        record.get("uname") or "", record.get("face_url") or "")

    # ③ COMBO 去重：16 条原始消息只入 14 条，嘉年华只算一次
    assert len(RAW_COMMANDS) == EXPECTED_RAW
    assert len(inserted) == EXPECTED_INSERTED
    gifts = get_session_gifts(db_path, session_id)
    assert len(gifts) == EXPECTED_INSERTED
    carnival = [g for g in gifts if g["gift_name"] == "嘉年华"]
    assert len(carnival) == 1, "COMBO_SEND 连击包没有被忽略，嘉年华重复计数"

    # ④ 下播
    end_session(db_path, session_id)
    latest = db.get_latest_session(db_path, ROOM_ID)
    assert latest["id"] == session_id and latest["end_ts"] is not None

    # ⑤ 金额榜/人气榜核算（用 poster 的真实聚合逻辑对手工预期）
    ranking, popular = poster._aggregate(gifts)
    paid_map = {e["uid"]: e["rmb"] for e in ranking}
    assert paid_map == EXPECTED_PAID, f"金额榜总额不符: {paid_map}"
    # 排序正确：降序且榜一是 U1
    assert [e["uid"] for e in ranking] == [2001, 2002, 2003, 2005]
    # 免费礼物没进金额榜
    assert 2004 not in paid_map and 2006 not in paid_map
    popular_map = {e["uid"]: e["count"] for e in popular}
    assert popular_map == EXPECTED_FREE_COUNT, f"人气榜数量不符: {popular_map}"

    # ⑥ 缺 face 的用户进待补全列表
    missing = db.get_users_missing_face(db_path, session_id)
    assert set(missing) == {2001, 2002, 2003, 2004, 2005, 2006}

    # ⑦ 生成海报
    path = generate_poster(db_path=db_path, data_dir=data_dir,
                           session_id=session_id, streamer_name="测试主播酱",
                           out_dir=e2e_env["out_dir"])
    assert os.path.isfile(path)
    size = os.path.getsize(path)
    assert size > 100 * 1024, f"海报体积过小: {size}"
    from PIL import Image
    img = Image.open(path)
    assert img.width == 2160 and img.height > 1500
    print(f"\nE2E 海报: {path} ({size / 1024:.0f}KB, {img.width}x{img.height})")


# ---------------------------------------------------------------------------
# COMBO 去重专项：同一对 SEND_GIFT + COMBO_SEND 只计一次
# ---------------------------------------------------------------------------
def test_combo_send_dedup(tmp_path):
    db_path = str(tmp_path / "dedup.db")
    init_db(db_path)
    sid = start_session(db_path, ROOM_ID, LIVE_TIME)
    send = RAW_COMMANDS[0]    # SEND_GIFT 嘉年华 3000 元
    combo = RAW_COMMANDS[1]   # 对应 COMBO_SEND
    assert parse_command(combo) is None, "COMBO_SEND 应被解析层忽略"
    for raw in (send, combo):
        parsed = parse_command(raw)
        if parsed is None:
            continue
        _, record = parsed
        record["session_id"] = sid
        insert_gift(db_path, record)
    gifts = get_session_gifts(db_path, sid)
    assert len(gifts) == 1
    assert gifts[0]["rmb"] == 3000.0  # 只计一次，不是 6000


# ---------------------------------------------------------------------------
# GUI 集成：offscreen MainWindow，只替换 GiftListener 的网络连接部分
# ---------------------------------------------------------------------------
GUI_RAW = [
    {"cmd": "SEND_GIFT", "data": {
        "uid": 3001, "uname": "GUI测试员", "face": "",
        "giftId": 31036, "giftName": "打call", "num": 3,
        "coin_type": "gold", "total_coin": 6000}},        # 6 元
    {"cmd": "SUPER_CHAT_MESSAGE", "data": {
        "uid": 3002, "price": 30, "message": "GUI SC",
        "user_info": {"uname": "SC用户", "face": "", "guard_level": 0}}},  # 30 元
    {"cmd": "GUARD_BUY", "data": {
        "uid": 3003, "username": "舰长君", "guard_level": 3,
        "num": 1, "price": 198000, "gift_id": 10003, "gift_name": "舰长"}},  # 198 元
]
GUI_EXPECT_COUNT = 3
GUI_EXPECT_AMOUNT = 234.0


def test_gui_integration_offscreen(tmp_path, monkeypatch):
    """起真实 MainWindow + 真实事件分发链路（_build_handler → parse → 入库
    → 渲染卡片 → on_event 信号 → 主线程刷新UI），只把 blivedm 网络连接换成
    推送模拟事件的协程。"""
    from PySide6.QtWidgets import QApplication, QMessageBox

    import gift_thanks.gui as gui_mod
    # 全套件一起跑时，test_m4 在收集期会把 gift_thanks 包属性和 sys.modules
    # 里的 auth/db/listener/poster 换成 fake（gui 若已导入则绑定 fake）。
    # 本测试要验证真实链路，强制把 gui 模块的依赖换回真实模块（结束后 monkeypatch 自动还原）。
    monkeypatch.setattr(gui_mod, "auth", auth)
    monkeypatch.setattr(gui_mod, "db", db)
    monkeypatch.setattr(gui_mod, "listener", listener)
    monkeypatch.setattr(gui_mod, "poster", poster)
    MainWindow = gui_mod.MainWindow

    qapp = QApplication.instance() or QApplication([])

    # 环境隔离：auth 的网络/弹窗不属于本次验证目标
    fake_qr = tmp_path / "qr.png"
    from PIL import Image as _Image
    _Image.new("RGB", (200, 200), (255, 255, 255)).save(fake_qr)
    monkeypatch.setattr(auth, "load_cookies", lambda: None)
    monkeypatch.setattr(auth, "generate_qr", lambda: ("k", str(fake_qr)))
    monkeypatch.setattr(auth, "poll_qr", lambda k: ("waiting", None))
    monkeypatch.setattr(auth, "save_cookies", lambda c: None)
    monkeypatch.setattr(auth, "get_sessdata", lambda: SECRET_SESSDATA)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)

    db_path = str(tmp_path / "gui.db")
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    init_db(db_path)

    async def fake_start(self):
        """替换网络连接：复用真实 _start_session / _build_handler 分发链路，
        把模拟原始命令推给真实 handler。"""
        self._start_session(LIVE_TIME)           # 真实开播（写库 + live_start 事件）
        handler = self._build_handler()          # 真实事件分发器
        for cmd in GUI_RAW:
            handler.handle(None, dict(cmd))      # 与 blivedm 回调同一路径
        # 等 create_task 的入库+卡片渲染完成（真实渲染，给足时间）：
        # 所有行都回填了 card_path 才算渲染链路完成
        deadline = time.time() + 120
        while time.time() < deadline:
            rows = get_session_gifts(self.db_path, self._session_id)
            if (len(rows) >= GUI_EXPECT_COUNT
                    and all(r["card_path"] for r in rows)):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)

    monkeypatch.setattr(listener.GiftListener, "start", fake_start)

    window = MainWindow(db_path=db_path, data_dir=data_dir)
    try:
        window.logged_in = True
        window.cookies = {"SESSDATA": SECRET_SESSDATA}
        window.room_input.setText(str(ROOM_ID))
        window._start_listening()
        assert window.monitoring is True

        # 等监听线程起来并跑完 fake_start
        thread = None
        for _ in range(200):
            qapp.processEvents()
            time.sleep(0.05)
            thread = window._worker._loop_thread
            if thread is not None:
                break
        assert thread is not None, "监听线程未启动"
        thread.join(timeout=150)
        assert not thread.is_alive(), "监听线程超时未结束"
        for _ in range(40):  # 抽干 queued 信号
            qapp.processEvents()
            time.sleep(0.05)

        # UI 计数断言
        assert f"本场礼物件数：{GUI_EXPECT_COUNT}" in window.gift_count_label.text()
        assert f"{GUI_EXPECT_AMOUNT:.2f}" in window.gift_amount_label.text()
        assert "GUI测试员" in window.recent_text.text()
        assert "醒目留言" in window.recent_text.text()
        assert "舰长" in window.recent_text.text()
        # live_start 事件刷新了场次
        assert window.session_id is not None

        # 真实入库 + 真实卡片渲染（listener 的 _handle_gift_record 链路）
        rows = get_session_gifts(db_path, window.session_id)
        assert len(rows) == GUI_EXPECT_COUNT
        assert sum(r["rmb"] for r in rows) == pytest.approx(GUI_EXPECT_AMOUNT)
        for r in rows:
            assert r["card_path"] and os.path.isfile(r["card_path"]), \
                f"礼物卡片未渲染: {r['id']}"
            assert os.path.getsize(r["card_path"]) > 10 * 1024

        # 停止监听 → 最近场次就绪，可生成海报
        window._stop_listening()
        qapp.processEvents()
        assert window.last_session_id is not None
        window._generate_poster()  # QMessageBox.exec 已 patch，不阻塞
        posters = [f for f in os.listdir(window.poster_dir) if f.endswith(".png")]
        assert posters, "GUI 触发海报生成失败"
        poster_path = os.path.join(window.poster_dir, posters[0])
        assert os.path.getsize(poster_path) > 100 * 1024
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 安全抽查（OWASP 轻量版）
# ---------------------------------------------------------------------------
def test_sec1_cred_bin_no_plaintext_sessdata(tmp_path, monkeypatch):
    """① cookie 加密文件 cred.bin 不出现明文 SESSDATA，且能正确解密回读。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cookies = {"SESSDATA": SECRET_SESSDATA, "bili_jct": "x", "DedeUserID": "1"}
    auth.save_cookies(cookies)
    cred = os.path.join(tmp_path, "GiftThanks", "cred.bin")
    assert os.path.isfile(cred)
    blob = open(cred, "rb").read()
    assert SECRET_SESSDATA.encode() not in blob, "cred.bin 含明文 SESSDATA！"
    assert b"SESSDATA" not in blob
    assert auth.load_cookies() == cookies  # 加密可逆、完整性 OK
    assert auth.get_sessdata() == SECRET_SESSDATA


def test_sec2_sql_injection_nickname(tmp_path):
    """② 昵称带 SQL 注入 Payload，gifts 表必须还在且数据原样存储。"""
    db_path = str(tmp_path / "inj.db")
    init_db(db_path)
    sid = start_session(db_path, ROOM_ID, LIVE_TIME)
    evil = "';DROP TABLE gifts;--"
    record = listener._new_record("gift")
    record.update(session_id=sid, uid=4001, uname=evil, gift_name=evil,
                  coin_type="gold", total_coin=1000, rmb=1.0)
    insert_gift(db_path, record)
    # 表还在、可读
    rows = get_session_gifts(db_path, sid)
    assert len(rows) == 1 and rows[0]["uname"] == evil
    # 再插一条验证表结构完好
    insert_gift(db_path, dict(record))
    assert len(get_session_gifts(db_path, sid)) == 2
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "gifts", "users"} <= tables


def test_sec3_poster_xss_escaped():
    """③ <script> 昵称在海报/卡片 HTML 中被转义（Jinja2 autoescape 开启）。"""
    env = giftcard._jinja_env
    assert env.autoescape, "Jinja2 autoescape 未开启"
    evil = "<script>alert(1)</script>"
    # 用与 generate_poster 完全相同的模板环境渲染 poster.html
    tpl = env.get_template("poster.html")
    html = tpl.render(
        streamer_name=evil, end_str="2025年10月13日 23:00",
        top3=[{"rank": 1, "uname": evil, "uid": 2002,
               "face": giftcard.PLACEHOLDER_FACE,
               "lines": [{"name": evil, "num": 1}], "total_str": "1998",
               "quote": evil, "thumb": ""}],
        others=[{"uname": evil, "face": giftcard.PLACEHOLDER_FACE,
                 "total_str": "1"}],
        popular=[{"uname": evil, "face": giftcard.PLACEHOLDER_FACE,
                  "count": 12}],
        long_text=evil, gen_time="2025-10-13 23:01:00",
        total_rmb="1999", gift_count=3,
    )
    assert "<script>alert(1)</script>" not in html, "海报 HTML 存在未转义 script 注入"
    assert "&lt;script&gt;" in html
    # 礼物卡片模板同样验证
    card_html = env.get_template("gift_card.html").render(
        uname=evil, face_src=giftcard.PLACEHOLDER_FACE,
        gift_img=giftcard.PLACEHOLDER_GIFT,
        placeholder_gift=giftcard.PLACEHOLDER_GIFT,
        gift_name=evil, num=1, rmb="1", is_free=False,
        kind_label="送出礼物", sc_message=evil, time_str="2025-10-13 20:00:00")
    assert "<script>alert(1)</script>" not in card_html


def test_sec4_no_sessdata_in_logs(tmp_path, monkeypatch, caplog):
    """④ 日志里不出现 SESSDATA（覆盖 auth/listener/gui 常用日志路径）。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with caplog.at_level(logging.DEBUG):
        auth.save_cookies({"SESSDATA": SECRET_SESSDATA})
        auth.load_cookies()
        auth.get_sessdata()
        # listener 构造与事件回调（异常路径也走一遍）
        lis = listener.GiftListener(room_id=ROOM_ID, sessdata=SECRET_SESSDATA,
                                    db_path=str(tmp_path / "x.db"),
                                    data_dir=str(tmp_path),
                                    on_event=lambda k, p: None)
        lis._emit("status", {"message": "正在连接直播间"})
        lis._emit("error", {"message": "弹幕连接断开: test"})
        # 解析失败路径（会 logger.exception）
        listener.parse_send_gift({"uid": object()})
    assert SECRET_SESSDATA not in caplog.text, "日志中泄漏了 SESSDATA"
