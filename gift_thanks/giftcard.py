# -*- coding: utf-8 -*-
"""单个礼物 → 礼物卡片 PNG（M3）

用 Playwright chromium 渲染 templates/gift_card.html：
viewport 800x200、device_scale_factor=2。
本模块同时维护一个进程内共享的 chromium 实例（懒加载、复用），
poster.py 的整页海报渲染也复用这里的 _render_html_to_png。
"""

import base64
import datetime
import logging
import mimetypes
import os
import pathlib
import shutil
import tempfile

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Jinja2 环境：autoescape 默认开启，昵称等文本自动 HTML 转义
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

# ---- Playwright / chromium 实例（懒加载复用）----
# 注意：playwright 同步 API 的对象绑定创建它的线程（greenlet 不能跨线程切换），
# 监听线程（经线程池渲染卡片）与 GUI 主线程（渲染海报）必须用各自的实例，
# 所以按线程 ID 分别持有。
import subprocess
import sys
import threading

_thread_local_browsers: dict[int, tuple] = {}  # threading.get_ident() -> (playwright, browser)

# 首次运行自动下载渲染组件的进程内锁与完成标记（线程安全）
_download_lock = threading.Lock()
_download_done = False


class BrowserMissingError(RuntimeError):
    """本地没有任何可用的 chromium（内置/默认缓存/系统浏览器都找不到）。"""


def _playwright_driver_paths() -> tuple[str, str] | None:
    """返回 (node 可执行文件, cli.js) 路径；playwright 不可用返回 None。"""
    try:
        from playwright._impl._driver import compute_driver_executable

        node, cli = compute_driver_executable()
        return str(node), str(cli)
    except Exception:
        return None


def _bundled_browsers_dir() -> str | None:
    """随安装包分发的内置浏览器目录。

    构建时用 PLAYWRIGHT_BROWSERS_PATH=0 执行 playwright install chromium，
    浏览器会装进 playwright/driver/package/.local-browsers，PyInstaller 一并打包。
    """
    paths = _playwright_driver_paths()
    if not paths:
        return None
    _, cli = paths
    bundled = os.path.join(os.path.dirname(cli), ".local-browsers")
    if os.path.isdir(bundled):
        return bundled
    return None


def _default_browsers_dir() -> str:
    """playwright 默认浏览器缓存目录（首次运行自动下载的目标位置）。"""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        return env
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ms-playwright")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


def _find_system_browser() -> str | None:
    """在 PATH 与 Windows 常见安装位置找系统 chromium / chrome / edge。"""
    for cand in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome", "msedge"):
        exe = shutil.which(cand)
        if exe:
            return exe
    if sys.platform == "win32":
        prefixes = [os.environ.get(k, "") for k in
                    ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rel_paths = [
            os.path.join("Google", "Chrome", "Application", "chrome.exe"),
            os.path.join("Chromium", "Application", "chrome.exe"),
            os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        for prefix in prefixes:
            if not prefix:
                continue
            for rel in rel_paths:
                exe = os.path.join(prefix, rel)
                if os.path.isfile(exe):
                    return exe
    return None


def browser_available() -> bool:
    """不启动浏览器，快速判断本地是否有可用 chromium。

    供 GUI 在生成海报前决定是否需要弹「首次运行下载渲染组件」进度框。
    """
    if _bundled_browsers_dir():
        return True
    default_dir = _default_browsers_dir()
    try:
        if os.path.isdir(default_dir):
            for name in os.listdir(default_dir):
                if name.startswith("chromium"):
                    return True
    except OSError:
        pass
    return _find_system_browser() is not None


def _browser_install_cmd() -> list[str]:
    """构造 `playwright install chromium` 等价命令。

    PyInstaller 冻结环境下 sys.executable 是程序自身（不能 -m playwright），
    改用打包进来的 node + cli.js；开发环境直接 python -m playwright。
    """
    if getattr(sys, "frozen", False):
        paths = _playwright_driver_paths()
        if paths:
            node, cli = paths
            if os.path.isfile(node) and os.path.isfile(cli):
                return [node, cli, "install", "chromium"]
        raise RuntimeError("安装包缺少内置下载组件（node/cli.js），请重新安装程序。")
    return [sys.executable, "-m", "playwright", "install", "chromium"]


def download_browser(progress_callback=None) -> None:
    """首次运行自动下载 chromium 渲染组件（约 170MB）。

    线程安全：进程内同一时间只跑一个下载；下载成功后再调用直接返回。
    失败抛 RuntimeError（中文提示）。progress_callback(str) 逐行接收下载日志。
    """
    global _download_done
    with _download_lock:
        if _download_done:
            return
        # 下载目标是默认缓存目录，确保不把路径指到只读的安装目录
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        cmd = _browser_install_cmd()
        logger.info("开始下载浏览器渲染组件: %s", cmd)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"无法启动渲染组件下载程序：{exc}") from exc
        try:
            for line in proc.stdout or []:
                line = line.strip()
                if line and progress_callback is not None:
                    try:
                        progress_callback(line)
                    except Exception:
                        pass
            rc = proc.wait(timeout=1800)  # 30 分钟超时
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("渲染组件下载超时（30分钟）。请检查网络后重试。")
        if rc != 0:
            raise RuntimeError(
                f"渲染组件下载失败（退出码 {rc}）。\n"
                "请检查网络（可先关闭代理或换个网络）后重试。")
        if not browser_available():
            raise RuntimeError("渲染组件下载完成但未找到浏览器文件，请重启程序后重试。")
        _download_done = True
        logger.info("浏览器渲染组件下载完成")


def _start_and_launch(executable_path: str | None = None):
    """启动一个 playwright 并尝试拉起 chromium；返回 (playwright, browser|None)。
    失败时已清理 playwright，不抛异常。"""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        kwargs = {}
        if executable_path:
            kwargs = {"executable_path": executable_path,
                      "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        return playwright, playwright.chromium.launch(**kwargs)
    except Exception as exc:
        logger.warning("chromium 启动失败（%s）：%s",
                       executable_path or "playwright 托管浏览器", exc)
        try:
            playwright.stop()
        except Exception:
            pass
        return playwright, None


def _get_browser():
    """启动（或复用）当前线程的 chromium。优先级：
    ① 安装包内置浏览器（PLAYWRIGHT_BROWSERS_PATH=0 → 包内 .local-browsers）
    ② playwright 默认缓存（含首次运行自动下载来的浏览器）
    ③ 系统安装的 chromium / chrome / edge
    都没有则抛 BrowserMissingError（GUI 会引导自动下载）。"""
    tid = threading.get_ident()
    entry = _thread_local_browsers.get(tid)
    if entry is not None:
        return entry[1]

    browser = None
    playwright = None
    # ① 安装包内置浏览器
    if _bundled_browsers_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        playwright, browser = _start_and_launch()
        if browser is None:
            logger.warning("内置浏览器启动失败，改用默认缓存/系统浏览器")
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    # ② playwright 默认浏览器缓存
    if browser is None:
        playwright, browser = _start_and_launch()
    # ③ 系统浏览器
    if browser is None:
        exe = _find_system_browser()
        if exe:
            playwright, browser = _start_and_launch(executable_path=exe)
    if browser is None:
        raise BrowserMissingError(
            "没有找到可用的浏览器渲染组件。\n"
            "如为首次运行，请点击「生成感谢海报」，程序会自动下载渲染组件（约170MB）。")
    _thread_local_browsers[tid] = (playwright, browser)
    return browser


def close_browser() -> None:
    """关闭当前线程的浏览器（其他线程的实例无法跨线程关闭，随进程退出回收）。"""
    tid = threading.get_ident()
    entry = _thread_local_browsers.pop(tid, None)
    if entry is None:
        return
    playwright, browser = entry
    try:
        browser.close()
    except Exception:
        pass
    try:
        playwright.stop()
    except Exception:
        pass


def _render_html_to_png(html_text: str, out_path: str, width: int,
                        height: int = 600, full_page: bool = False,
                        scale: int = 2) -> None:
    """共享渲染内部函数：把 HTML 渲染成 PNG。

    HTML 先写到 templates/ 下的临时文件，再以 file:// 打开，
    这样模板里 @font-face 的相对路径（fonts/xxx.otf）能正常加载。
    """
    browser = _get_browser()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".html", prefix="_render_", dir=_TEMPLATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html_text)
        page = browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=scale,
        )
        try:
            page.goto(pathlib.Path(tmp).as_uri())
            # 等网络资源（礼物图等）加载完成；外网不可达时超时即走 onerror 占位
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            try:
                page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass
            page.wait_for_timeout(300)  # 给字体/图片渲染留一点时间
            page.screenshot(path=out_path, full_page=full_page)
        finally:
            page.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def file_to_data_uri(path: str) -> str | None:
    """把本地图片文件读成 base64 data URI；文件不存在/读取失败返回 None。"""
    try:
        if not path or not os.path.isfile(path):
            return None
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
    except Exception as exc:
        logger.warning("图片转 data URI 失败 %s: %s", path, exc)
        return None


def _svg_data_uri(svg: str) -> str:
    """SVG 转 base64 data URI（base64 可避免引号/井号破坏 HTML 属性）。"""
    return ("data:image/svg+xml;base64,"
            + base64.b64encode(svg.encode("utf-8")).decode("ascii"))


# 头像/图片缺失时的占位图（内联 SVG，无外部依赖）
PLACEHOLDER_FACE = _svg_data_uri(
    "<svg xmlns='http://www.w3.org/2000/svg' width='120' height='120'>"
    "<rect width='120' height='120' rx='24' fill='#f0e6da'/>"
    "<circle cx='60' cy='48' r='20' fill='#d9c6b0'/>"
    "<path d='M22 108 Q60 62 98 108 Z' fill='#d9c6b0'/>"
    "</svg>")
PLACEHOLDER_GIFT = _svg_data_uri(
    "<svg xmlns='http://www.w3.org/2000/svg' width='120' height='120'>"
    "<rect width='120' height='120' rx='20' fill='#f6ece0'/>"
    "<rect x='30' y='52' width='60' height='44' rx='8' fill='#e8b98a'/>"
    "<rect x='26' y='38' width='68' height='20' rx='8' fill='#f2cda0'/>"
    "<rect x='55' y='38' width='10' height='58' fill='#fff4e3'/>"
    "</svg>")


def _fmt_money(rmb: float) -> str:
    """金额格式化：去掉多余的 0（198.00 -> 198，19.80 -> 19.8）。"""
    try:
        s = f"{float(rmb):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        s = "0"
    return s


def render_gift_card(data_dir: str, gift_row: dict) -> str:
    """渲染单个礼物卡片 PNG，返回 {data_dir}/cards/{礼物记录主键id}.png。"""
    pk = gift_row.get("id")
    if pk is None:
        raise ValueError("gift_row 缺少主键 id")
    out_path = os.path.join(data_dir, "cards", f"{pk}.png")

    # 头像：优先本地 face_path 转 data URI，规避防盗链；缺失用占位图
    face_src = file_to_data_uri(gift_row.get("face_path") or "") or PLACEHOLDER_FACE

    # 礼物图：直接用 gift_img_url（HTML 里 onerror 换占位图）；
    # 若已经是本地文件路径则转 data URI
    gift_img = gift_row.get("gift_img_url") or ""
    if gift_img and not gift_img.startswith(("http://", "https://", "data:")):
        gift_img = file_to_data_uri(gift_img) or ""
    if not gift_img:
        gift_img = PLACEHOLDER_GIFT

    # 事件类型文案：普通礼物 / 醒目留言 SC / 大航海
    event_type = gift_row.get("event_type") or "gift"
    kind_label = {"gift": "送出礼物", "sc": "醒目留言", "guard": "开通大航海"}.get(
        event_type, "送出礼物")

    # 时间：来自事件时间戳 ts
    try:
        time_str = datetime.datetime.fromtimestamp(
            float(gift_row.get("ts") or 0)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        time_str = ""

    gift_name = gift_row.get("gift_name") or ("SC" if event_type == "sc" else "礼物")
    if event_type == "guard":
        guard_level = gift_row.get("guard_level") or 3
        gift_name = {1: "总督", 2: "提督", 3: "舰长"}.get(guard_level, "舰长")

    num = gift_row.get("num") or 1
    rmb = float(gift_row.get("rmb") or 0)

    template = _jinja_env.get_template("gift_card.html")
    html_text = template.render(
        uname=gift_row.get("uname") or "神秘人",
        face_src=face_src,
        gift_img=gift_img,
        placeholder_gift=PLACEHOLDER_GIFT,
        gift_name=gift_name,
        num=num,
        rmb=_fmt_money(rmb),
        is_free=rmb <= 0 and event_type == "gift",
        kind_label=kind_label,
        sc_message=gift_row.get("sc_message") or "",
        time_str=time_str,
    )
    _render_html_to_png(html_text, out_path, width=800, height=200,
                        full_page=False, scale=2)
    logger.info("礼物卡片已生成: %s", out_path)
    return out_path
