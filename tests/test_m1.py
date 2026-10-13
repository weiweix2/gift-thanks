# -*- coding: utf-8 -*-
"""M1 测试：db.py 数据层 + listener.py 消息解析纯函数。

不触碰网络（listener 连接部分不测），用模拟的原始消息 dict 验证：
- SEND_GIFT / SUPER_CHAT_MESSAGE / GUARD_BUY 解析入库
- COMBO_SEND 去重（忽略，防连击重复计数）
- 金额换算（1000金瓜子=1元、SC price 单位元、上舰 198000=198元、免费礼物 rmb=0）
- 场次开始/结束/复用
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gift_thanks import db
from gift_thanks import listener


# ---------------------------------------------------------------- 模拟消息

def make_send_gift_cmd(**overrides):
    """模拟 SEND_GIFT 命令（字段名与 blivedm models/web.py GiftMessage.from_command 一致）"""
    data = {
        "uid": 123456,
        "uname": "测试用户甲",
        "face": "https://i0.hdslb.com/bfs/face/aaa.jpg",
        "giftId": 30607,
        "giftName": "打call",
        "num": 10,
        "price": 1000,
        "coin_type": "gold",
        "total_coin": 10000,
        "timestamp": 1700000000,
        "guard_level": 0,
        "giftType": 0,
        "gift_info": {"img_basic": "https://i0.hdslb.com/bfs/live/gift30607.png"},
        "action": "赠送",
        "rnd": "1700000000",
        "tid": "abc",
    }
    data.update(overrides)
    return {"cmd": "SEND_GIFT", "data": data}


def make_super_chat_cmd(**overrides):
    """模拟 SUPER_CHAT_MESSAGE 命令"""
    data = {
        "uid": 654321,
        "price": 30,
        "message": "主播加油！",
        "user_info": {
            "uname": "醒目留言用户",
            "face": "https://i1.hdslb.com/bfs/face/bbb.jpg",
            "guard_level": 0,
        },
    }
    data.update(overrides)
    return {"cmd": "SUPER_CHAT_MESSAGE", "data": data}


def make_guard_buy_cmd(**overrides):
    """模拟 GUARD_BUY 命令（username 而非 uname；price 单位金瓜子）"""
    data = {
        "uid": 777888,
        "username": "新舰长",
        "guard_level": 3,
        "num": 1,
        "price": 198000,
        "gift_id": 10003,
        "gift_name": "舰长",
        "start_time": 1700000000,
        "end_time": 1700000000,
    }
    data.update(overrides)
    return {"cmd": "GUARD_BUY", "data": data}


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "giftthanks.db")
    db.init_db(path)
    return path


# ---------------------------------------------------------------- 解析纯函数

class TestParseGift:
    def test_gold_gift_rmb(self):
        """金瓜子礼物：rmb = total_coin / 1000"""
        parsed = listener.parse_command(make_send_gift_cmd())
        assert parsed is not None
        kind, rec = parsed
        assert kind == "gift" == rec["event_type"]
        assert rec["uid"] == 123456
        assert rec["uname"] == "测试用户甲"
        assert rec["face_url"] == "https://i0.hdslb.com/bfs/face/aaa.jpg"
        assert rec["gift_name"] == "打call"
        assert rec["gift_id"] == 30607
        assert rec["num"] == 10
        assert rec["coin_type"] == "gold"
        assert rec["total_coin"] == 10000
        assert rec["rmb"] == pytest.approx(10.0)
        assert rec["gift_img_url"] == "https://i0.hdslb.com/bfs/live/gift30607.png"

    def test_silver_gift_free_rmb_zero(self):
        """银瓜子（免费）礼物：rmb = 0"""
        _, rec = listener.parse_command(
            make_send_gift_cmd(coin_type="silver", total_coin=5000, giftName="辣条")
        )
        assert rec["coin_type"] == "silver"
        assert rec["rmb"] == 0.0

    def test_gift_img_fallback_top_level_img(self):
        """礼物图兜底：gift_info 缺失时取 data 顶层 img"""
        cmd = make_send_gift_cmd()
        del cmd["data"]["gift_info"]
        cmd["data"]["img"] = "https://example.com/fallback.png"
        _, rec = listener.parse_command(cmd)
        assert rec["gift_img_url"] == "https://example.com/fallback.png"

    def test_cmd_with_colon_suffix(self):
        """cmd 带 ':xxx' 后缀也能解析"""
        cmd = make_send_gift_cmd()
        cmd["cmd"] = "SEND_GIFT:12345"
        parsed = listener.parse_command(cmd)
        assert parsed is not None and parsed[0] == "gift"

    def test_bad_data_returns_none(self):
        """坏数据不抛异常，返回 None"""
        assert listener.parse_command({"cmd": "SEND_GIFT", "data": {"uid": "not-an-int"}}) is None
        assert listener.parse_command({"cmd": "SEND_GIFT"}) is None


class TestParseSuperChat:
    def test_sc_price_is_yuan(self):
        """醒目留言：price 单位就是元"""
        kind, rec = listener.parse_command(make_super_chat_cmd())
        assert kind == "sc" == rec["event_type"]
        assert rec["uid"] == 654321
        assert rec["uname"] == "醒目留言用户"
        assert rec["face_url"] == "https://i1.hdslb.com/bfs/face/bbb.jpg"
        assert rec["rmb"] == pytest.approx(30.0)
        assert rec["sc_message"] == "主播加油！"


class TestParseGuardBuy:
    def test_guard_price_conversion(self):
        """上舰：price 单位金瓜子，198000 = 198 元"""
        kind, rec = listener.parse_command(make_guard_buy_cmd())
        assert kind == "guard" == rec["event_type"]
        assert rec["uid"] == 777888
        assert rec["uname"] == "新舰长"
        assert rec["guard_level"] == 3
        assert rec["rmb"] == pytest.approx(198.0)

    def test_guard_levels(self):
        """1总督/2提督/3舰长 的金额换算"""
        for level, price, rmb in [(1, 1999800, 1999.8), (2, 199800, 199.8), (3, 198000, 198.0)]:
            _, rec = listener.parse_command(make_guard_buy_cmd(guard_level=level, price=price))
            assert rec["guard_level"] == level
            assert rec["rmb"] == pytest.approx(rmb)


class TestComboSendIgnored:
    def test_combo_send_ignored(self):
        """COMBO_SEND 必须忽略，否则连击重复计数"""
        cmd = {"cmd": "COMBO_SEND", "data": make_send_gift_cmd()["data"]}
        assert listener.parse_command(cmd) is None

    def test_combo_send_only_send_gift_counted(self, db_path):
        """同一批连击礼物：SEND_GIFT + COMBO_SEND 同时到达，只入库一条"""
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        count = 0
        for cmd in (make_send_gift_cmd(), {"cmd": "COMBO_SEND", "data": make_send_gift_cmd()["data"]}):
            parsed = listener.parse_command(cmd)
            if parsed is None:
                continue
            _, rec = parsed
            rec["session_id"] = sid
            db.insert_gift(db_path, rec)
            count += 1
        gifts = db.get_session_gifts(db_path, sid)
        assert count == 1
        assert len(gifts) == 1

    def test_unknown_cmd_ignored(self):
        assert listener.parse_command({"cmd": "DANMU_MSG", "info": []}) is None
        assert listener.parse_command({"cmd": "LIVE"}) is None
        assert listener.parse_command({}) is None


# ---------------------------------------------------------------- 数据层

class TestDb:
    def test_init_idempotent(self, db_path):
        db.init_db(db_path)  # 二次初始化不报错

    def test_insert_and_get_gifts(self, db_path):
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        for cmd in (make_send_gift_cmd(), make_super_chat_cmd(), make_guard_buy_cmd()):
            kind, rec = listener.parse_command(cmd)
            rec["session_id"] = sid
            pk = db.insert_gift(db_path, rec)
            assert pk > 0
        gifts = db.get_session_gifts(db_path, sid)
        assert len(gifts) == 3
        assert [g["event_type"] for g in gifts] == ["gift", "sc", "guard"]
        assert gifts[0]["rmb"] == pytest.approx(10.0)
        assert gifts[1]["rmb"] == pytest.approx(30.0)
        assert gifts[2]["rmb"] == pytest.approx(198.0)
        # 全部列都在
        for col in ("id", "session_id", "ts", "uid", "uname", "face_url", "gift_name",
                    "gift_id", "num", "total_coin", "coin_type", "rmb", "guard_level",
                    "sc_message", "gift_img_url", "card_path"):
            assert col in gifts[0]

    def test_total_rmb_of_session(self, db_path):
        """一场总金额 = 金瓜子礼物 + SC + 上舰，免费礼物不计"""
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        for cmd in (make_send_gift_cmd(),
                    make_send_gift_cmd(coin_type="silver", total_coin=999999),
                    make_super_chat_cmd(),
                    make_guard_buy_cmd()):
            _, rec = listener.parse_command(cmd)
            rec["session_id"] = sid
            db.insert_gift(db_path, rec)
        total = sum(g["rmb"] for g in db.get_session_gifts(db_path, sid))
        assert total == pytest.approx(10.0 + 30.0 + 198.0)

    def test_session_start_end(self, db_path):
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        sess = db.get_latest_session(db_path, 123)
        assert sess["id"] == sid
        assert sess["end_ts"] is None
        db.end_session(db_path, sid)
        sess = db.get_latest_session(db_path, 123)
        assert sess["end_ts"] is not None
        assert sess["end_ts"] >= sess["start_ts"]

    def test_session_reuse_same_live_time(self, db_path):
        """同 live_time 且未结束：复用同一场次"""
        s1 = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        s2 = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        assert s1 == s2

    def test_session_no_reuse_after_end(self, db_path):
        """已结束或 live_time 不同：开新场次"""
        s1 = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        db.end_session(db_path, s1)
        s2 = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        assert s2 != s1
        s3 = db.start_session(db_path, 123, "2026-10-13 22:00:00")
        assert s3 != s2
        # 不同房间互不复用
        s4 = db.start_session(db_path, 456, "2026-10-13 22:00:00")
        assert s4 != s3

    def test_get_latest_session_empty(self, db_path):
        assert db.get_latest_session(db_path, 999) is None

    def test_upsert_user_and_face(self, db_path):
        db.upsert_user(db_path, 111, "用户A", "https://face/a.jpg")
        db.upsert_user(db_path, 111, "用户A改名", "https://face/a2.jpg")
        db.upsert_user(db_path, 222, "用户B", "")
        db.set_user_face_path(db_path, 111, "/data/faces/111.jpg")
        # 已回填 face_path 的用户不再缺头像
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        for uid in (111, 222):
            _, rec = listener.parse_command(make_send_gift_cmd(uid=uid))
            rec["session_id"] = sid
            db.insert_gift(db_path, rec)
        missing = db.get_users_missing_face(db_path, sid)
        assert missing == [222]

    def test_missing_face_includes_unknown_user(self, db_path):
        """users 表里没有记录但送过礼的 uid 也算缺头像"""
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        _, rec = listener.parse_command(make_send_gift_cmd(uid=333))
        rec["session_id"] = sid
        db.insert_gift(db_path, rec)
        assert db.get_users_missing_face(db_path, sid) == [333]

    def test_set_gift_card_path(self, db_path):
        sid = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        _, rec = listener.parse_command(make_send_gift_cmd())
        rec["session_id"] = sid
        pk = db.insert_gift(db_path, rec)
        assert db.get_session_gifts(db_path, sid)[0]["card_path"] is None
        db.set_gift_card_path(db_path, pk, "/data/cards/1.png")
        assert db.get_session_gifts(db_path, sid)[0]["card_path"] == "/data/cards/1.png"

    def test_gifts_isolated_between_sessions(self, db_path):
        s1 = db.start_session(db_path, 123, "2026-10-13 20:00:00")
        s2 = db.start_session(db_path, 123, "2026-10-13 22:00:00")
        for sid in (s1, s2):
            _, rec = listener.parse_command(make_send_gift_cmd())
            rec["session_id"] = sid
            db.insert_gift(db_path, rec)
        assert len(db.get_session_gifts(db_path, s1)) == 1
        assert len(db.get_session_gifts(db_path, s2)) == 1


# ---------------------------------------------------------------- GiftListener 基本行为（不联网）

class TestGiftListener:
    def test_init(self, db_path, tmp_path):
        events = []
        gl = listener.GiftListener(
            room_id=123, sessdata="fake-sessdata", db_path=db_path,
            data_dir=str(tmp_path), on_event=lambda k, p: events.append((k, p)),
        )
        assert gl.room_id == 123
        assert gl._session_id is None

    def test_emit_callback_error_not_crash(self, db_path, tmp_path):
        """on_event 回调抛异常不能让监听崩溃"""

        def bad_callback(kind, payload):
            raise RuntimeError("GUI炸了")

        gl = listener.GiftListener(1, "", db_path, str(tmp_path), on_event=bad_callback)
        gl._emit("status", {"message": "x"})  # 不抛异常即通过

    def test_handle_gift_record_offline(self, db_path, tmp_path):
        """无 LIVE 先手收到礼物：自动开场、入库、upsert_user、回调（渲染卡片兜底不炸）"""
        events = []
        gl = listener.GiftListener(123, "", db_path, str(tmp_path),
                                   on_event=lambda k, p: events.append(k))

        async def fake_room_init():
            return {"live_time": "2026-10-13 20:00:00"}

        gl._fetch_room_init = fake_room_init
        _, rec = listener.parse_command(make_send_gift_cmd())

        import asyncio
        asyncio.run(gl._handle_gift_record("gift", rec))

        gifts = db.get_session_gifts(db_path, gl._session_id)
        assert len(gifts) == 1
        assert gifts[0]["uname"] == "测试用户甲"
        assert gifts[0]["rmb"] == pytest.approx(10.0)
        assert db.get_users_missing_face(db_path, gl._session_id) == [123456]
        assert "gift" in events and "live_start" in events

    def test_stop_ends_session(self, db_path, tmp_path):
        """手动 stop：未结束场次立即 end_session"""
        import asyncio

        async def main():
            gl = listener.GiftListener(123, "", db_path, str(tmp_path))
            gl._session_id = db.start_session(db_path, 123, "2026-10-13 20:00:00")
            await gl.stop()
            assert gl._session_id is None
            assert db.get_latest_session(db_path, 123)["end_ts"] is not None

        asyncio.run(main())
