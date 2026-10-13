# -*- coding: utf-8 -*-
"""礼物答谢姬 —— PySide6 主窗口（M4）。

面向电脑小白用户：界面全中文、大按钮、少选项。
布局（从上到下）：
    1. 登录卡片区（二维码 200x200 + 状态文字 + 登录成功后昵称头像 + 「重新扫码」）
    2. 房间号输入框 + 「开始监听」「停止监听」按钮
    3. 状态区（场次状态、本场礼物件数、本场总金额、最近 5 条礼物滚动文本）
    4. 「生成感谢海报」+「打开海报文件夹」按钮

线程模型：
    - 扫码轮询：QTimer 每 2 秒调一次 auth.poll_qr（轻量 HTTP，主线程可承受）；
    - 礼物监听：独立 QThread 内跑 asyncio 事件循环，
      listener 的 on_event 回调通过 pyqtSignal 转回主线程刷新 UI（线程安全）。
"""

import asyncio
import logging
import os
import threading

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gift_thanks import auth, db, giftcard, listener, poster

logger = logging.getLogger("gift_thanks.gui")

# 海报输出目录名（位于数据目录下）
POSTER_DIR_NAME = "posters"


# ---------------------------------------------------------------------------
# 监听线程：在独立 QThread 中跑 asyncio 事件循环
# ---------------------------------------------------------------------------
class ListenerWorker(QObject):
    """把 GiftListener 包进独立线程。

    主线程信号：
        start_requested(room_id, sessdata, db_path, data_dir) -> 启动监听
        stop_requested()                                        -> 停止监听
    回主线程信号：
        event_received(kind, payload)  —— 礼物/场次/错误等事件（线程安全）
    """

    event_received = Signal(str, dict)
    start_requested = Signal(int, str, str, str)
    stop_requested = Signal()

    def __init__(self):
        super().__init__()
        self._listener = None
        self._loop = None
        self._loop_thread = None
        # 信号 -> 槽（跨线程，QueuedConnection 由 Qt 自动选择）
        self.start_requested.connect(self._on_start)
        self.stop_requested.connect(self._on_stop)

    # ---- 以下槽函数运行在本 worker 所在线程 ----

    @Slot(int, str, str, str)
    def _on_start(self, room_id: int, sessdata: str, db_path: str, data_dir: str):
        if self._listener is not None:
            logger.warning("监听已在运行，忽略重复启动请求")
            return

        def _on_event(kind: str, payload: dict):
            # listener 在 asyncio 线程里回调，这里只做信号转发（Qt 信号线程安全）
            try:
                self.event_received.emit(kind, dict(payload or {}))
            except Exception:  # 兜底：回调异常绝不弄崩监听
                logger.exception("转发监听事件失败")

        self._listener = listener.GiftListener(
            room_id=room_id,
            sessdata=sessdata,
            db_path=db_path,
            data_dir=data_dir,
            on_event=_on_event,
        )

        def _run_loop():
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(self._listener.start())
            except Exception as exc:  # 网络异常等，转成事件通知主线程
                logger.exception("监听线程异常退出")
                try:
                    self.event_received.emit("error", {"message": f"监听连接出错：{exc}"})
                except Exception:
                    pass
            finally:
                try:
                    if self._loop is not None:
                        self._loop.close()
                except Exception:
                    pass
                self._loop = None
                self._listener = None

        self._loop_thread = threading.Thread(target=_run_loop, name="gift-listener", daemon=True)
        self._loop_thread.start()

    @Slot()
    def _on_stop(self):
        lis, loop = self._listener, self._loop
        self._listener = None
        if lis is not None and loop is not None:
            try:
                # 把协程安全的 stop 调度进监听线程的事件循环
                asyncio.run_coroutine_threadsafe(lis.stop(), loop)
            except Exception:
                logger.exception("停止监听时出错")
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None


# ---------------------------------------------------------------------------
# 渲染组件下载线程：首次运行本地没有可用浏览器时，后台下载 chromium
# ---------------------------------------------------------------------------
class BrowserDownloadWorker(QObject):
    """在独立线程里下载浏览器渲染组件（约170MB）。

    回主线程信号：finished(ok, message) —— ok=False 时 message 为中文错误原因。
    """

    finished = Signal(bool, str)

    @Slot()
    def run(self):
        try:
            giftcard.download_browser()
        except Exception as exc:
            logger.exception("下载浏览器渲染组件失败")
            self.finished.emit(False, str(exc))
        else:
            self.finished.emit(True, "")


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """礼物答谢姬主窗口。"""

    def __init__(self, db_path: str, data_dir: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.data_dir = data_dir
        self.poster_dir = os.path.join(data_dir, POSTER_DIR_NAME)
        os.makedirs(self.poster_dir, exist_ok=True)

        # 运行时状态
        self.qrcode_key = None          # 当前二维码的 key
        self.logged_in = False          # 是否已登录
        self.cookies = None             # 登录后的 cookie 字典
        self.room_id = None             # 当前监听的房间号
        self.monitoring = False         # 是否正在监听
        self.session_id = None          # 当前场次 id
        self.last_session_id = None     # 最近一场（已结束/停止的）会话 id
        self.gift_count = 0             # 本场礼物件数
        self.gift_amount = 0.0          # 本场总金额（元）
        self._poster_running = False    # 海报是否生成中

        self._build_ui()

        # 扫码轮询定时器（每 2 秒一次）
        self._qr_timer = QTimer(self)
        self._qr_timer.setInterval(2000)
        self._qr_timer.timeout.connect(self._poll_qr_once)

        # 监听线程
        self._listener_thread = QThread(self)
        self._worker = ListenerWorker()
        self._worker.moveToThread(self._listener_thread)
        self._worker.event_received.connect(self._on_listener_event)
        self._listener_thread.start()

        # 启动时尝试读本地 cookie，有则直接进入已登录态
        self._try_auto_login()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.setWindowTitle("礼物答谢姬")
        self.resize(520, 780)

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ---------- ① 登录卡片区 ----------
        login_box = QGroupBox("第一步：登录B站账号", self)
        login_layout = QHBoxLayout(login_box)

        self.qr_label = QLabel("二维码加载中…", login_box)
        self.qr_label.setFixedSize(200, 200)
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setFrameShape(QFrame.StyledPanel)
        self.qr_label.setStyleSheet("background:#fff; color:#666;")
        login_layout.addWidget(self.qr_label)

        login_right = QVBoxLayout()
        self.login_status_label = QLabel("正在准备二维码，请稍候…", login_box)
        self.login_status_label.setWordWrap(True)
        login_right.addWidget(self.login_status_label)

        # 登录成功后显示的昵称 + 头像
        user_row = QHBoxLayout()
        self.avatar_label = QLabel(login_box)
        self.avatar_label.setFixedSize(48, 48)
        self.avatar_label.setStyleSheet("background:#eee; border-radius:24px;")
        self.avatar_label.hide()
        user_row.addWidget(self.avatar_label)
        self.nickname_label = QLabel("", login_box)
        self.nickname_label.hide()
        user_row.addWidget(self.nickname_label)
        user_row.addStretch(1)
        login_right.addLayout(user_row)

        login_right.addStretch(1)
        self.relogin_button = QPushButton("重新扫码", login_box)
        self.relogin_button.setMinimumHeight(44)
        self.relogin_button.clicked.connect(self._start_qr_login)
        login_right.addWidget(self.relogin_button)
        login_layout.addLayout(login_right)
        root.addWidget(login_box)

        # ---------- ② 房间号 + 监听按钮 ----------
        listen_box = QGroupBox("第二步：监听你的直播间", self)
        listen_layout = QVBoxLayout(listen_box)

        self.room_input = QLineEdit(listen_box)
        self.room_input.setPlaceholderText("输入你的直播间房间号")
        self.room_input.setMinimumHeight(40)
        listen_layout.addWidget(self.room_input)

        btn_row = QHBoxLayout()
        self.start_button = QPushButton("开始监听", listen_box)
        self.start_button.setMinimumHeight(48)
        self.start_button.clicked.connect(self._start_listening)
        btn_row.addWidget(self.start_button)
        self.stop_button = QPushButton("停止监听", listen_box)
        self.stop_button.setMinimumHeight(48)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_listening)
        btn_row.addWidget(self.stop_button)
        listen_layout.addLayout(btn_row)
        root.addWidget(listen_box)

        # ---------- ③ 状态区 ----------
        status_box = QGroupBox("本场直播情况", self)
        status_layout = QVBoxLayout(status_box)

        self.session_status_label = QLabel("场次状态：未开始监听", status_box)
        status_layout.addWidget(self.session_status_label)

        count_row = QHBoxLayout()
        self.gift_count_label = QLabel("本场礼物件数：0", status_box)
        count_row.addWidget(self.gift_count_label)
        self.gift_amount_label = QLabel("本场总金额：0.00 元", status_box)
        count_row.addWidget(self.gift_amount_label)
        count_row.addStretch(1)
        status_layout.addLayout(count_row)

        recent_box = QScrollArea(status_box)
        recent_box.setMinimumHeight(110)
        recent_box.setWidgetResizable(True)
        self.recent_text = QLabel("（还没有收到礼物）", recent_box)
        self.recent_text.setWordWrap(True)
        self.recent_text.setAlignment(Qt.AlignTop)
        self.recent_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        recent_box.setWidget(self.recent_text)
        self.recent_scroll = recent_box  # 测试用别名
        status_layout.addWidget(recent_box)
        self._recent_lines = []
        root.addWidget(status_box, stretch=1)

        # ---------- ④ 海报按钮 ----------
        bottom_row = QHBoxLayout()
        self.poster_button = QPushButton("生成感谢海报", central)
        self.poster_button.setMinimumHeight(52)
        self.poster_button.clicked.connect(self._generate_poster)
        bottom_row.addWidget(self.poster_button)
        self.open_poster_button = QPushButton("打开海报文件夹", central)
        self.open_poster_button.setMinimumHeight(52)
        self.open_poster_button.clicked.connect(self._open_poster_folder)
        bottom_row.addWidget(self.open_poster_button)
        root.addLayout(bottom_row)

        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------
    def _try_auto_login(self):
        """启动时尝试读取本地加密 cookie，成功则直接进入已登录态。"""
        try:
            cookies = auth.load_cookies()
        except Exception:
            logger.exception("读取本地登录信息失败")
            cookies = None
        if cookies:
            self._on_login_success(cookies, announce=False)
        else:
            self._start_qr_login()

    def _start_qr_login(self):
        """生成二维码并开启轮询。任何失败都只提示，不崩溃。"""
        self._qr_timer.stop()
        try:
            self.qrcode_key, qr_png = auth.generate_qr()
            pixmap = QPixmap(qr_png)
            if pixmap.isNull():
                raise RuntimeError("二维码图片加载失败")
            self.qr_label.setPixmap(
                pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            self.login_status_label.setText("请用B站App扫码登录")
            self.avatar_label.hide()
            self.nickname_label.hide()
            self._qr_timer.start()
        except Exception as exc:
            logger.exception("生成登录二维码失败")
            self.login_status_label.setText("二维码获取失败，请点击「重新扫码」重试")
            self._show_error("登录", f"获取二维码失败：{exc}\n\n请检查网络后点击「重新扫码」。")

    @Slot()
    def _poll_qr_once(self):
        """每 2 秒轮询一次扫码状态。"""
        if not self.qrcode_key:
            self._qr_timer.stop()
            return
        try:
            status, cookies = auth.poll_qr(self.qrcode_key)
        except Exception as exc:
            # 网络抖动不致命，本轮跳过
            logger.warning("扫码轮询失败（本轮忽略）：%s", exc)
            return

        if status == "waiting":
            self.login_status_label.setText("请用B站App扫码登录")
        elif status == "scanned":
            self.login_status_label.setText("已扫码，请在手机上点「确认登录」")
        elif status == "expired":
            self._qr_timer.stop()
            self.login_status_label.setText("二维码已过期，请点击「重新扫码」")
        elif status == "success" and cookies:
            self._qr_timer.stop()
            try:
                auth.save_cookies(cookies)
            except Exception:
                logger.exception("保存登录信息失败")
            self._on_login_success(cookies, announce=True)

    def _on_login_success(self, cookies: dict, announce: bool):
        """登录成功：更新状态与昵称头像。"""
        self.logged_in = True
        self.cookies = cookies
        nickname = str(cookies.get("nickname") or cookies.get("uname") or "B站用户")
        face_url = str(cookies.get("face") or cookies.get("face_url") or "")
        self.login_status_label.setText(f"已登录：{nickname}")
        self.nickname_label.setText(nickname)
        self.nickname_label.show()
        self.avatar_label.show()
        if face_url:
            self._load_avatar(face_url)
        if announce:
            self.session_status_label.setText("场次状态：已登录，可以开始监听")

    def _load_avatar(self, face_url: str):
        """尽量加载头像；失败就只显示昵称（不阻塞、不崩溃）。"""
        try:
            import urllib.request

            req = urllib.request.Request(face_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            if not pixmap.isNull():
                self.avatar_label.setPixmap(
                    pixmap.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
        except Exception:
            logger.warning("头像加载失败（忽略）", exc_info=True)

    # ------------------------------------------------------------------
    # 监听控制
    # ------------------------------------------------------------------
    def _start_listening(self):
        if self.monitoring:
            return
        if not self.logged_in:
            self._show_info("提示", "请先扫码登录B站账号，再开始监听。")
            return
        room_text = self.room_input.text().strip()
        if not room_text.isdigit():
            self._show_info("提示", "请输入正确的直播间房间号（纯数字）。")
            self.room_input.setFocus()
            return

        self.room_id = int(room_text)
        self.gift_count = 0
        self.gift_amount = 0.0
        self._recent_lines = []
        self._refresh_stats()
        self._refresh_recent()

        sessdata = ""
        try:
            sessdata = auth.get_sessdata() or ""
        except Exception:
            logger.exception("读取登录凭证失败")

        self.monitoring = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.room_input.setEnabled(False)
        self.session_status_label.setText(f"场次状态：正在连接房间 {self.room_id} …")
        self._worker.start_requested.emit(self.room_id, sessdata, self.db_path, self.data_dir)

    def _stop_listening(self):
        if not self.monitoring:
            return
        self._worker.stop_requested.emit()
        self.monitoring = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.room_input.setEnabled(True)
        if self.session_id is not None:
            self.last_session_id = self.session_id
            self.session_id = None
        self.session_status_label.setText("场次状态：已停止监听，可以生成感谢海报")

    # ------------------------------------------------------------------
    # 监听事件（来自监听线程的信号，运行在主线程）
    # ------------------------------------------------------------------
    @Slot(str, dict)
    def _on_listener_event(self, kind: str, payload: dict):
        try:
            if kind in ("gift", "sc", "guard"):
                self._handle_gift_event(kind, payload)
            elif kind == "live_start":
                self.session_id = payload.get("session_id", self.session_id)
                self.session_status_label.setText("场次状态：直播进行中，正在记录礼物")
            elif kind == "live_end":
                if self.session_id is not None:
                    self.last_session_id = self.session_id
                    self.session_id = None
                elif payload.get("session_id"):
                    self.last_session_id = payload["session_id"]
                self.session_status_label.setText("场次状态：本场直播已结束，可以生成感谢海报")
            elif kind == "status":
                msg = payload.get("message", "")
                if msg:
                    self.session_status_label.setText(f"场次状态：{msg}")
            elif kind == "error":
                msg = payload.get("message", "未知错误")
                self.session_status_label.setText(f"场次状态：出错（{msg}）")
                logger.error("监听错误：%s", msg)
        except Exception:
            logger.exception("处理监听事件失败")

    def _handle_gift_event(self, kind: str, payload: dict):
        """礼物事件：累计件数/金额，更新最近 5 条滚动列表。"""
        self.gift_count += 1
        try:
            self.gift_amount += float(payload.get("rmb") or 0)
        except (TypeError, ValueError):
            pass

        uname = payload.get("uname") or "好心人"
        num = payload.get("num") or 1
        rmb = payload.get("rmb") or 0
        if kind == "sc":
            text = f"{uname} 发送了醒目留言（{rmb}元）"
        elif kind == "guard":
            text = f"{uname} 开通了舰长/守护（{rmb}元）"
        else:
            text = f"{uname} 送出 {payload.get('gift_name') or '礼物'} ×{num}（{rmb}元）"

        self._recent_lines.append(text)
        self._recent_lines = self._recent_lines[-5:]  # 只保留最近 5 条
        self._refresh_stats()
        self._refresh_recent()

    def _refresh_stats(self):
        self.gift_count_label.setText(f"本场礼物件数：{self.gift_count}")
        self.gift_amount_label.setText(f"本场总金额：{self.gift_amount:.2f} 元")

    def _refresh_recent(self):
        if self._recent_lines:
            self.recent_text.setText("\n".join(self._recent_lines))
        else:
            self.recent_text.setText("（还没有收到礼物）")

    # ------------------------------------------------------------------
    # 海报生成 / 打开文件夹
    # ------------------------------------------------------------------
    def _target_session_id(self):
        """确定要生成海报的会话：当前场优先，其次最近一场。"""
        if self.session_id is not None:
            return self.session_id
        if self.last_session_id is not None:
            return self.last_session_id
        if self.room_id is not None:
            try:
                latest = db.get_latest_session(self.db_path, self.room_id)
                if latest:
                    return latest.get("id")
            except Exception:
                logger.exception("查询最近场次失败")
        return None

    def _generate_poster(self):
        if self._poster_running:
            return
        session_id = self._target_session_id()
        if session_id is None:
            self._show_info("提示", "还没有可生成海报的直播场次。\n请先监听一场直播（或输入房间号监听过）。")
            return

        # 首次运行兜底：本地没有任何可用浏览器时，先弹进度框自动下载渲染组件
        try:
            need_download = not giftcard.browser_available()
        except Exception:
            need_download = False
        if need_download and not self._ensure_browser_ready():
            return

        self._poster_running = True
        self.poster_button.setEnabled(False)
        self.poster_button.setText("生成中…")
        try:
            streamer = self.nickname_label.text() or "主播"
            path = poster.generate_poster(
                db_path=self.db_path,
                data_dir=self.data_dir,
                session_id=session_id,
                streamer_name=streamer,
                out_dir=self.poster_dir,
            )
        except Exception as exc:
            logger.exception("生成感谢海报失败")
            self._show_error("生成海报", f"海报生成失败：{exc}\n\n请重试，或稍后再试。")
            return
        finally:
            self._poster_running = False
            self.poster_button.setEnabled(True)
            self.poster_button.setText("生成感谢海报")

        box = QMessageBox(self)
        box.setWindowTitle("海报已生成")
        box.setIcon(QMessageBox.Information)
        box.setText(f"感谢海报已经做好啦！\n\n保存位置：{path}")
        open_btn = box.addButton("打开所在文件夹", QMessageBox.AcceptRole)
        box.addButton("好的", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_poster_folder()

    def _ensure_browser_ready(self) -> bool:
        """首次运行：弹进度对话框，后台线程下载 chromium 渲染组件。

        成功返回 True；失败返回 False（已弹中文错误提示）。
        不提供取消按钮：下载中途取消容易留下半成品的浏览器目录。
        """
        progress = QProgressDialog(
            "首次运行需要下载渲染组件（约170MB），请稍候…\n"
            "下载完成后海报会自动生成，只需下载这一次。",
            None, 0, 0, self)
        progress.setWindowTitle("正在准备渲染组件")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        thread = QThread(self)
        worker = BrowserDownloadWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)

        result = {"ok": False, "message": ""}

        def _on_finished(ok: bool, message: str):
            result["ok"] = ok
            result["message"] = message
            progress.close()  # 结束下面的模态事件循环

        worker.finished.connect(_on_finished)
        thread.start()
        progress.exec()  # 模态显示，直到下载完成信号触发 close()
        thread.wait(5000)
        worker.deleteLater()

        if result["ok"]:
            self._show_info("准备完成", "渲染组件已就绪，正在为你生成海报。")
            return True
        self._show_error(
            "下载失败",
            result["message"] or "渲染组件下载失败。\n请检查网络后重试。")
        return False

    def _open_poster_folder(self):
        try:
            os.makedirs(self.poster_dir, exist_ok=True)
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(self.poster_dir))
            if not ok:
                raise RuntimeError("系统无法打开文件夹")
        except Exception as exc:
            logger.exception("打开海报文件夹失败")
            self._show_error("打开文件夹", f"无法打开海报文件夹：{exc}\n\n路径：{self.poster_dir}")

    # ------------------------------------------------------------------
    # 通用提示
    # ------------------------------------------------------------------
    def _show_info(self, title: str, text: str):
        QMessageBox.information(self, title, text)

    def _show_error(self, title: str, text: str):
        QMessageBox.warning(self, title, text)

    # ------------------------------------------------------------------
    # 关闭窗口：先停监听再退出
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        try:
            if self.monitoring:
                self._worker.stop_requested.emit()
            self._qr_timer.stop()
            self._listener_thread.quit()
            self._listener_thread.wait(3000)
        except Exception:
            logger.exception("关闭窗口清理失败")
        super().closeEvent(event)
