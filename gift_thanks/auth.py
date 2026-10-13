"""auth.py — B站扫码登录 + cookie 加密存储（M2）

契约（见 SPEC.md 登录层契约）：
    def generate_qr() -> tuple[str, str]
    def poll_qr(qrcode_key: str) -> tuple[str, dict | None]
    def save_cookies(cookies: dict) -> None
    def load_cookies() -> dict | None
    def get_sessdata() -> str | None
"""

import base64
import hashlib
import json
import logging
import os
import sys
import uuid

import qrcode
import requests
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# 固定盐（SPEC 约定）
_SALT = "GiftThanks2026"

# 扫码登录接口（公开接口，实测需 GET + 浏览器 UA）
_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 轮询接口 data.code → 状态映射
_POLL_STATUS_MAP = {
    86101: "waiting",   # 未扫码
    86090: "scanned",   # 已扫码未确认
    0: "success",       # 扫码成功
    86038: "expired",   # 二维码已过期
}


def get_data_dir() -> str:
    """数据目录：Windows 下 %APPDATA%/GiftThanks/，无 APPDATA 回退主目录下 .giftthanks/"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        data_dir = os.path.join(appdata, "GiftThanks")
    else:
        data_dir = os.path.join(os.path.expanduser("~"), ".giftthanks")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _cred_path() -> str:
    return os.path.join(get_data_dir(), "cred.bin")


def _machine_id() -> str:
    """获取机器标识：Windows 读注册表 MachineGuid，Linux/mac 读 /etc/machine-id，
    最后回退 uuid.getnode()。任何一步失败都继续尝试下一方案，保证不抛异常。"""
    # Windows：注册表 MachineGuid
    if sys.platform.startswith("win"):
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            )
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            winreg.CloseKey(key)
            if value:
                return str(value)
        except Exception as exc:  # noqa: BLE001 - 容错继续回退
            logger.warning("读取注册表 MachineGuid 失败：%s", exc)
    # Linux/mac：/etc/machine-id 或 /var/lib/dbus/machine-id
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    value = f.read().strip()
                if value:
                    return value
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 %s 失败：%s", path, exc)
    # 最终回退：网卡 MAC 地址
    try:
        return str(uuid.getnode())
    except Exception as exc:  # noqa: BLE001
        logger.warning("uuid.getnode() 失败：%s，使用固定兜底值", exc)
        return "unknown-machine"


def _fernet_key() -> bytes:
    """密钥 = urlsafe_b64encode(SHA256(机器标识 + 固定盐).digest())"""
    digest = hashlib.sha256((_machine_id() + _SALT).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def generate_qr() -> tuple[str, str]:
    """申请登录二维码。

    返回 (qrcode_key, 二维码PNG本地路径)。
    网络异常直接抛出（由调用方 GUI 捕获提示重试），成功时保证返回有效图片路径。
    """
    resp = requests.get(
        _QR_GENERATE_URL,
        headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"申请二维码失败：{payload.get('message')}")
    data = payload["data"]
    qrcode_key = data["qrcode_key"]
    qr_url = data["url"]

    # 本地生成二维码 PNG
    png_path = os.path.join(get_data_dir(), "login_qr.png")
    img = qrcode.make(qr_url)
    img.save(png_path)
    logger.info("二维码已生成：%s", png_path)
    return qrcode_key, png_path


def poll_qr(qrcode_key: str) -> tuple[str, dict | None]:
    """轮询扫码状态。

    返回 ('waiting'|'scanned'|'success'|'expired', cookies)。
    成功时 cookies 为 Set-Cookie 解析出的 dict（含 SESSDATA 等）。
    网络异常时返回 ('waiting', None)，保证轮询循环不崩溃。
    """
    try:
        resp = requests.get(
            _QR_POLL_URL,
            params={"qrcode_key": qrcode_key},
            headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - 网络抖动视为仍在等待
        logger.warning("轮询扫码状态网络异常：%s", exc)
        return "waiting", None

    if payload.get("code") != 0:
        # 顶层非 0 一般是限流等，视为等待中继续轮询
        logger.warning("轮询扫码状态返回异常：%s", payload.get("message"))
        return "waiting", None

    data = payload.get("data") or {}
    inner_code = data.get("code")
    status = _POLL_STATUS_MAP.get(inner_code)
    if status is None:
        logger.warning("未知扫码状态码：%s（%s）", inner_code, data.get("message"))
        return "waiting", None

    if status == "success":
        # 从 Set-Cookie 中解析出 cookie dict
        cookies = {c.name: c.value for c in resp.cookies}
        return "success", cookies
    return status, None


def save_cookies(cookies: dict) -> None:
    """Fernet 加密保存 cookie 到 {数据目录}/cred.bin"""
    plaintext = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
    token = Fernet(_fernet_key()).encrypt(plaintext)
    path = _cred_path()
    with open(path, "wb") as f:
        f.write(token)
    # cookie 属敏感信息，绝不写日志内容
    logger.info("cookie 已加密保存到 %s", path)


def load_cookies() -> dict | None:
    """读取并解密 cred.bin；文件不存在或解密失败返回 None"""
    path = _cred_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            token = f.read()
        plaintext = Fernet(_fernet_key()).decrypt(token)
        cookies = json.loads(plaintext.decode("utf-8"))
        if not isinstance(cookies, dict):
            return None
        return cookies
    except InvalidToken:
        logger.warning("cred.bin 解密失败（可能换了机器或文件损坏）")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 cred.bin 失败：%s", exc)
        return None


def get_sessdata() -> str | None:
    """取已保存 cookie 中的 SESSDATA；未登录/解密失败返回 None"""
    cookies = load_cookies()
    if not cookies:
        return None
    return cookies.get("SESSDATA")
