# -*- coding: utf-8 -*-
"""礼物答谢姬 —— 程序入口（M4）。

职责：创建数据目录、初始化数据库、配置日志、启动 QApplication 与主窗口。
运行方式：python -m gift_thanks.main
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def get_data_dir() -> str:
    """数据目录：Windows 下 %APPDATA%/GiftThanks/，无 APPDATA 则回退用户主目录。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "GiftThanks")


def setup_logging(data_dir: str) -> None:
    """日志写 {data_dir}/app.log（滚动文件，最多 3 个 1MB）。"""
    log_path = os.path.join(data_dir, "app.log")
    handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> int:
    # ---- 数据目录 & 数据库 ----
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    setup_logging(data_dir)
    logger = logging.getLogger("gift_thanks.main")

    from gift_thanks import db

    db_path = os.path.join(data_dir, "giftthanks.db")
    try:
        db.init_db(db_path)
    except Exception as exc:
        logger.exception("初始化数据库失败")
        # 数据库坏了也尽量弹窗告知，而不是无声崩溃
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication(sys.argv)
        QMessageBox.critical(None, "礼物答谢姬", f"初始化本地数据失败：{exc}\n\n程序无法继续运行。")
        return 1

    # ---- Qt 高 DPI 适配（PySide6 默认开启，显式设置以兼容旧行为）----
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass  # 高DPI设置失败不影响启动

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("礼物答谢姬")

    from gift_thanks.gui import MainWindow

    window = MainWindow(db_path=db_path, data_dir=data_dir)
    window.show()
    logger.info("程序启动完成，数据目录：%s", data_dir)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
