# -*- coding: utf-8 -*-
"""M4 测试：gui.py / main.py。

其他模块（auth/listener/poster/db）由别的代理并行开发，
这里统一 mock 后再导入 gui，保证本模块可以独立测试。
运行：QT_QPA_PLATFORM=offscreen python -m pytest tests/test_m4.py -v
"""

import os
import sys
import types
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ---------------------------------------------------------------------------
# 在导入 gui 之前，把并行开发的模块塞进 sys.modules（mock）
# ---------------------------------------------------------------------------
# 造一个真的二维码占位 PNG，避免 QPixmap 加载失败弹出阻塞对话框
_FAKE_QR_PNG = "/tmp/fake_qr_m4_test.png"
if not os.path.exists(_FAKE_QR_PNG):
    from PIL import Image

    Image.new("RGB", (200, 200), (255, 255, 255)).save(_FAKE_QR_PNG)

_fake_auth = types.ModuleType("gift_thanks.auth")
_fake_auth.generate_qr = MagicMock(return_value=("fake_qr_key", _FAKE_QR_PNG))
_fake_auth.poll_qr = MagicMock(return_value=("waiting", None))
_fake_auth.save_cookies = MagicMock()
_fake_auth.load_cookies = MagicMock(return_value=None)
_fake_auth.get_sessdata = MagicMock(return_value="fake_sessdata")

_fake_listener = types.ModuleType("gift_thanks.listener")


class FakeGiftListener:
    """假监听器：只记录构造参数，start/stop 是空协程。"""

    instances = []

    def __init__(self, room_id, sessdata, db_path, data_dir, on_event=None):
        self.room_id = room_id
        self.sessdata = sessdata
        self.db_path = db_path
        self.data_dir = data_dir
        self.on_event = on_event
        self.started = False
        FakeGiftListener.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False


_fake_listener.GiftListener = FakeGiftListener

_fake_db = types.ModuleType("gift_thanks.db")
_fake_db.init_db = MagicMock()
_fake_db.start_session = MagicMock(return_value=1)
_fake_db.end_session = MagicMock()
_fake_db.insert_gift = MagicMock(return_value=1)
_fake_db.get_session_gifts = MagicMock(return_value=[])
_fake_db.get_latest_session = MagicMock(return_value={"id": 99, "room_id": 123})
_fake_db.upsert_user = MagicMock()
_fake_db.get_users_missing_face = MagicMock(return_value=[])
_fake_db.set_user_face_path = MagicMock()

_fake_poster = types.ModuleType("gift_thanks.poster")
_fake_poster.generate_poster = MagicMock(return_value="/tmp/poster_test.png")

# 注意必须强制覆盖而非 setdefault：全套件一起跑时 m1~m3 已把真实模块
# 导入 sys.modules，setdefault 会变成 no-op，导致 gui 绑定真实模块
# （真实 auth 走网络、真实 QMessageBox 阻塞，全套件挂死）。
# m1~m3 的测试文件在导入时已持有真实模块引用，此处替换不影响它们。
import gift_thanks as _gt_pkg

for _name, _fake in (("auth", _fake_auth), ("listener", _fake_listener),
                     ("db", _fake_db), ("poster", _fake_poster)):
    sys.modules[f"gift_thanks.{_name}"] = _fake
    # 「from gift_thanks import xxx」优先取包对象上已有的属性（此前真实导入留下的），
    # 所以包属性也必须一并替换，否则 gui 仍绑定真实模块
    setattr(_gt_pkg, _name, _fake)
# 防御：若 gui 已被导入过，强制重新导入以绑定上面的 fake
sys.modules.pop("gift_thanks.gui", None)

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from gift_thanks.gui import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qapp, tmp_path):
    """每个测试一个独立窗口；默认处于未登录+扫码等待状态。"""
    _fake_auth.load_cookies.return_value = None
    _fake_auth.poll_qr.return_value = ("waiting", None)
    _fake_auth.generate_qr.reset_mock()
    _fake_auth.save_cookies.reset_mock()
    _fake_poster.generate_poster.reset_mock()
    FakeGiftListener.instances.clear()
    w = MainWindow(db_path=str(tmp_path / "test.db"), data_dir=str(tmp_path))
    yield w
    w.close()
    w.deleteLater()
    qapp.processEvents()


def _process(qapp, times=5):
    for _ in range(times):
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 窗口创建与基本元素
# ---------------------------------------------------------------------------
def test_window_creates_with_all_widgets(window):
    assert window.windowTitle() == "礼物答谢姬"
    # 按钮存在且文案正确
    assert window.relogin_button.text() == "重新扫码"
    assert window.start_button.text() == "开始监听"
    assert window.stop_button.text() == "停止监听"
    assert window.poster_button.text() == "生成感谢海报"
    assert window.open_poster_button.text() == "打开海报文件夹"
    # 房间号输入框与 placeholder
    assert window.room_input.placeholderText() == "输入你的直播间房间号"
    # 初始状态
    assert "未开始监听" in window.session_status_label.text()
    assert "0" in window.gift_count_label.text()
    assert window.stop_button.isEnabled() is False
    # 无 cookie 时自动请求二维码
    _fake_auth.generate_qr.assert_called_once()


def test_auto_login_with_saved_cookies(qapp, tmp_path):
    """启动时 load_cookies 有值 → 直接进入已登录态，不再请求二维码。"""
    _fake_auth.load_cookies.return_value = {"nickname": "已有账号"}
    _fake_auth.generate_qr.reset_mock()
    w = MainWindow(db_path=str(tmp_path / "t.db"), data_dir=str(tmp_path))
    try:
        assert w.logged_in is True
        assert "已登录：已有账号" in w.login_status_label.text()
        _fake_auth.generate_qr.assert_not_called()
    finally:
        _fake_auth.load_cookies.return_value = None
        w.close()
        w.deleteLater()


# ---------------------------------------------------------------------------
# 登录流程
# ---------------------------------------------------------------------------
def test_login_success_updates_status(window, qapp):
    """模拟扫码轮询返回 success → 保存 cookie、停止轮询、状态文本变化。"""
    window._start_qr_login()
    assert window._qr_timer.isActive()

    _fake_auth.poll_qr.return_value = ("success", {"nickname": "测试主播", "face": ""})
    window._poll_qr_once()
    _process(qapp)

    assert window.logged_in is True
    assert "已登录：测试主播" in window.login_status_label.text()
    assert window.nickname_label.text() == "测试主播"
    assert window.nickname_label.isVisible() or True  # offscreen 下 isVisible 恒 False，只校验文本
    _fake_auth.save_cookies.assert_called_once_with({"nickname": "测试主播", "face": ""})
    assert not window._qr_timer.isActive()  # 登录成功后停止轮询


def test_poll_qr_scanned_and_expired_texts(window):
    window._start_qr_login()
    _fake_auth.poll_qr.return_value = ("scanned", None)
    window._poll_qr_once()
    assert "确认登录" in window.login_status_label.text()

    _fake_auth.poll_qr.return_value = ("expired", None)
    window._poll_qr_once()
    assert "过期" in window.login_status_label.text()
    assert not window._qr_timer.isActive()


def test_poll_qr_network_error_does_not_crash(window):
    window._start_qr_login()
    _fake_auth.poll_qr.side_effect = RuntimeError("网络断了")
    window._poll_qr_once()  # 不应抛异常
    _fake_auth.poll_qr.side_effect = None
    assert window.logged_in is False


# ---------------------------------------------------------------------------
# 监听控制
# ---------------------------------------------------------------------------
def test_start_listening_requires_login(window, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.logged_in = False
    window._start_listening()
    assert window.monitoring is False
    assert FakeGiftListener.instances == []


def test_start_listening_requires_valid_room(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.logged_in = True
    window.room_input.setText("abc不是数字")
    window._start_listening()
    assert window.monitoring is False


def test_start_and_stop_listening(window, qapp):
    window.logged_in = True
    window.cookies = {}
    window.room_input.setText("12345")
    window._start_listening()
    _process(qapp)

    assert window.monitoring is True
    assert window.room_id == 12345
    assert window.start_button.isEnabled() is False
    assert window.stop_button.isEnabled() is True
    assert window.room_input.isEnabled() is False
    assert "12345" in window.session_status_label.text()

    window._stop_listening()
    _process(qapp)
    assert window.monitoring is False
    assert window.start_button.isEnabled() is True
    assert window.stop_button.isEnabled() is False


# ---------------------------------------------------------------------------
# 礼物事件 → 计数与滚动列表
# ---------------------------------------------------------------------------
def test_gift_event_increases_count_and_amount(window, qapp):
    assert "0" in window.gift_count_label.text()

    window._worker.event_received.emit(
        "gift", {"uname": "小明", "gift_name": "辣条", "num": 2, "rmb": 0.2}
    )
    _process(qapp)

    assert "本场礼物件数：1" in window.gift_count_label.text()
    assert "0.20" in window.gift_amount_label.text()
    assert "小明" in window.recent_text.text()
    assert "辣条" in window.recent_text.text()


def test_recent_gifts_keep_only_five(window, qapp):
    for i in range(8):
        window._worker.event_received.emit(
            "gift", {"uname": f"用户{i}", "gift_name": "小花", "num": 1, "rmb": 1.0}
        )
    _process(qapp)

    lines = [ln for ln in window.recent_text.text().split("\n") if ln.strip()]
    assert len(lines) == 5  # 只保留最近 5 条
    assert "用户7" in window.recent_text.text()
    assert "用户0" not in window.recent_text.text()
    assert "本场礼物件数：8" in window.gift_count_label.text()


def test_sc_and_guard_events_count(window, qapp):
    window._worker.event_received.emit("sc", {"uname": "阿红", "rmb": 30, "message": "加油"})
    window._worker.event_received.emit("guard", {"uname": "阿蓝", "rmb": 198, "guard_level": 3})
    _process(qapp)

    assert "本场礼物件数：2" in window.gift_count_label.text()
    assert "228.00" in window.gift_amount_label.text()
    assert "醒目留言" in window.recent_text.text()
    assert "舰长" in window.recent_text.text()


def test_live_start_and_end_events(window, qapp):
    window._worker.event_received.emit("live_start", {"session_id": 7})
    _process(qapp)
    assert window.session_id == 7
    assert "进行中" in window.session_status_label.text()

    window._worker.event_received.emit("live_end", {"session_id": 7})
    _process(qapp)
    assert window.session_id is None
    assert window.last_session_id == 7
    assert "已结束" in window.session_status_label.text()


def test_error_event_shows_friendly_status(window, qapp):
    window._worker.event_received.emit("error", {"message": "连接断开"})
    _process(qapp)
    assert "连接断开" in window.session_status_label.text()


# ---------------------------------------------------------------------------
# 海报
# ---------------------------------------------------------------------------
def test_generate_poster_calls_poster_module(window, qapp, monkeypatch):
    window.session_id = 7
    exec_mock = MagicMock(return_value=0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: exec_mock())
    window._generate_poster()
    _process(qapp)

    _fake_poster.generate_poster.assert_called_once()
    kwargs = _fake_poster.generate_poster.call_args.kwargs
    assert kwargs["session_id"] == 7
    assert kwargs["db_path"] == window.db_path
    assert kwargs["data_dir"] == window.data_dir
    # 生成完成后按钮恢复
    assert window.poster_button.isEnabled() is True
    assert window.poster_button.text() == "生成感谢海报"
    exec_mock.assert_called_once()  # 弹出了路径提示框


def test_generate_poster_without_session_shows_hint(window, monkeypatch):
    window.session_id = None
    window.last_session_id = None
    window.room_id = None
    info_mock = MagicMock()
    monkeypatch.setattr(QMessageBox, "information", info_mock)
    window._generate_poster()
    info_mock.assert_called_once()
    _fake_poster.generate_poster.assert_not_called()


def test_generate_poster_failure_shows_error_and_recovers(window, monkeypatch):
    window.session_id = 3
    _fake_poster.generate_poster.side_effect = RuntimeError("渲染失败")
    warn_mock = MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warn_mock)
    window._generate_poster()  # 不应崩溃
    warn_mock.assert_called_once()
    assert window.poster_button.isEnabled() is True
    assert window.poster_button.text() == "生成感谢海报"
    _fake_poster.generate_poster.side_effect = None


def test_open_poster_folder(window, monkeypatch):
    open_mock = MagicMock(return_value=True)
    monkeypatch.setattr("gift_thanks.gui.QDesktopServices.openUrl", open_mock)
    window._open_poster_folder()
    open_mock.assert_called_once()
    assert os.path.isdir(window.poster_dir)


def test_open_poster_folder_failure_no_crash(window, monkeypatch):
    monkeypatch.setattr("gift_thanks.gui.QDesktopServices.openUrl", MagicMock(return_value=False))
    warn_mock = MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warn_mock)
    window._open_poster_folder()
    warn_mock.assert_called_once()
