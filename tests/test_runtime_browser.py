# -*- coding: utf-8 -*-
"""打包/运行时浏览器获取逻辑测试（M5）：不真正启动浏览器，全部走 mock。"""

import subprocess

import pytest

from gift_thanks import giftcard


@pytest.fixture(autouse=True)
def _reset_download_state(monkeypatch):
    """每个测试前重置下载状态，避免 _download_done 互相影响。"""
    monkeypatch.setattr(giftcard, "_download_done", False)
    yield


def test_browser_available_returns_bool():
    """沙箱里有系统 chromium，应为 True；返回值必须是布尔。"""
    result = giftcard.browser_available()
    assert isinstance(result, bool)


def test_browser_install_cmd_dev_mode(monkeypatch):
    """开发环境（非 frozen）：python -m playwright install chromium。"""
    monkeypatch.setattr(giftcard.sys, "frozen", False, raising=False)
    cmd = giftcard._browser_install_cmd()
    assert cmd[1:] == ["-m", "playwright", "install", "chromium"]


def test_browser_install_cmd_frozen_mode(monkeypatch, tmp_path):
    """冻结环境：用打包的 node + cli.js，而不是 sys.executable -m playwright。"""
    node = tmp_path / "node.exe"
    cli = tmp_path / "package" / "cli.js"
    cli.parent.mkdir()
    node.write_text("x")
    cli.write_text("x")
    monkeypatch.setattr(giftcard.sys, "frozen", True, raising=False)
    monkeypatch.setattr(giftcard, "_playwright_driver_paths",
                        lambda: (str(node), str(cli)))
    cmd = giftcard._browser_install_cmd()
    assert cmd == [str(node), str(cli), "install", "chromium"]


def test_browser_install_cmd_frozen_missing_driver(monkeypatch):
    """冻结环境但驱动文件缺失：抛中文错误而不是误用程序自身。"""
    monkeypatch.setattr(giftcard.sys, "frozen", True, raising=False)
    monkeypatch.setattr(giftcard, "_playwright_driver_paths", lambda: None)
    with pytest.raises(RuntimeError, match="内置下载组件"):
        giftcard._browser_install_cmd()


def test_download_browser_success_idempotent(monkeypatch):
    """下载成功：调用 install 命令一次；再次调用直接返回（锁内去重）。"""
    calls = []

    class _FakeProc:
        stdout = iter(["downloading…\n"])

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: calls.append(cmd) or _FakeProc())
    monkeypatch.setattr(giftcard, "_browser_install_cmd", lambda: ["fake", "install"])
    monkeypatch.setattr(giftcard, "browser_available", lambda: True)
    lines = []
    giftcard.download_browser(progress_callback=lines.append)
    giftcard.download_browser()
    assert len(calls) == 1
    assert lines == ["downloading…"]


def test_download_browser_failure_chinese_message(monkeypatch):
    """下载命令非零退出：抛 RuntimeError，中文提示可重试，且不记住成功状态。"""

    class _FakeProc:
        stdout = iter([])

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(giftcard, "_browser_install_cmd", lambda: ["fake", "install"])
    with pytest.raises(RuntimeError, match="下载失败"):
        giftcard.download_browser()
    assert giftcard._download_done is False
