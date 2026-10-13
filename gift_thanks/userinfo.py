"""userinfo.py — 批量用户信息/头像补全（M2）

契约（见 SPEC.md 登录层契约）：
    async def fill_missing_faces(db_path: str, session_id: int, sessdata: str) -> None

- 对 get_users_missing_face 返回的 uid，用
  https://api.vc.bilibili.com/account/v1/user/cards?uids=1,2,3 批量（≤50/次）查 face
- 接口需要 SESSDATA cookie + Wbi 签名
- 头像下载到 {data_dir}/faces/{uid}.jpg（空 Referer、浏览器 UA、超时 10s，
  失败用内置默认头像），set_user_face_path 回填
- 请求间隔 ≥1 秒；-352/412 风控时退避重试最多 3 次
"""

import asyncio
import hashlib
import logging
import os
import time
import urllib.parse
from functools import reduce

import aiohttp

# 注意：db 模块（M1）的 import 放在 fill_missing_faces 内部延迟加载，
# 以便本模块在无 db.py 的环境（如独立单测）中也能导入纯逻辑函数。

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_CARDS_URL = "https://api.vc.bilibili.com/account/v1/user/cards"

_BATCH_SIZE = 50          # 批量接口单次最多 50 个 uid
_REQUEST_INTERVAL = 1.0   # 请求间隔 ≥1 秒
_MAX_RETRY = 3            # 风控退避重试最多 3 次
_HTTP_TIMEOUT = 10        # 头像下载超时 10 秒

# Wbi 混淆表（公开实现，与 bilibili-api-python / blivedm 一致）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _get_mixin_key(orig: str) -> str:
    """按混淆表重排取前 32 字符得到 mixin_key"""
    return reduce(lambda s, i: s + orig[i], _MIXIN_KEY_ENC_TAB, "")[:32]


def _enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """Wbi 签名：参数加 wts 后排序、过滤非法字符，md5 得 w_rid"""
    mixin_key = _get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    # 按 key 排序
    params = dict(sorted(params.items()))
    # 过滤 value 中的 !'()* 字符
    params = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params


def _chunk_uids(uids: list[int], size: int = _BATCH_SIZE) -> list[list[int]]:
    """把 uid 列表按 size 分组（批量接口 ≤50/次）"""
    return [uids[i : i + size] for i in range(0, len(uids), size)]


def _default_avatar_path() -> str:
    """内置默认占位头像；文件缺失时用 PIL 现场生成一张纯色图兜底"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "default_avatar.png")
    if not os.path.exists(path):
        try:
            from PIL import Image, ImageDraw

            os.makedirs(os.path.dirname(path), exist_ok=True)
            img = Image.new("RGB", (200, 200), (191, 191, 191))  # 灰色纯色底
            draw = ImageDraw.Draw(img)
            draw.ellipse((60, 45, 140, 125), fill=(140, 140, 140))    # 头
            draw.ellipse((35, 140, 165, 235), fill=(140, 140, 140))   # 肩
            img.save(path, "PNG")
        except Exception as exc:  # noqa: BLE001
            logger.warning("生成默认占位头像失败：%s", exc)
    return path


async def _get_wbi_keys(session: aiohttp.ClientSession) -> tuple[str, str] | None:
    """从 nav 接口取 img_key / sub_key（未登录也返回）。失败返回 None。"""
    try:
        async with session.get(_NAV_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            payload = await resp.json(content_type=None)
        wbi_img = payload["data"]["wbi_img"]
        img_key = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        return img_key, sub_key
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取 Wbi 密钥失败：%s", exc)
        return None


async def _fetch_user_cards(session: aiohttp.ClientSession,
                            wbi_keys: tuple[str, str],
                            uids: list[int]) -> dict[int, str]:
    """批量查询一组 uid 的 face URL，返回 {uid: face_url}。

    -352/412 风控时指数退避重试最多 _MAX_RETRY 次；其他失败返回空 dict。
    """
    img_key, sub_key = wbi_keys
    params = _enc_wbi({"uids": ",".join(str(u) for u in uids)}, img_key, sub_key)
    for attempt in range(1, _MAX_RETRY + 1):
        try:
            async with session.get(
                _CARDS_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                payload = await resp.json(content_type=None)
            code = payload.get("code")
            if code == 0:
                data = payload.get("data") or {}
                result = {}
                for uid in uids:
                    info = data.get(str(uid)) or {}
                    face = info.get("face")
                    if face:
                        result[uid] = face
                return result
            if code in (-352, 412):
                # 风控：退避后重试
                wait = 2 ** attempt
                logger.warning("批量用户信息接口被风控(code=%s)，%ds 后第 %d 次重试",
                               code, wait, attempt)
                await asyncio.sleep(wait)
                continue
            logger.warning("批量用户信息接口返回异常：code=%s message=%s",
                           code, payload.get("message"))
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量用户信息请求失败（第 %d 次）：%s", attempt, exc)
            await asyncio.sleep(2 ** attempt)
    return {}


async def _download_face(session: aiohttp.ClientSession, face_url: str,
                         save_path: str) -> bool:
    """下载头像（hdslb.com 图床空 Referer 即可直连），超时 10s。成功返回 True。"""
    try:
        async with session.get(
            face_url,
            headers={"User-Agent": _UA, "Referer": ""},  # 空 Referer
            timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("头像下载 HTTP %s：%s", resp.status, face_url)
                return False
            content = await resp.read()
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(content)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("头像下载失败 %s：%s", face_url, exc)
        return False


async def fill_missing_faces(db_path: str, session_id: int, sessdata: str) -> None:
    """对指定场次缺头像的用户批量补全 face_url 并下载头像，回填 face_path。

    任何网络失败都不抛异常：下载失败的用户用内置默认头像兜底。
    """
    from gift_thanks.db import get_users_missing_face, set_user_face_path

    try:
        uids = get_users_missing_face(db_path, session_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("查询缺头像用户失败：%s", exc)
        return
    if not uids:
        return

    # data_dir 由 db_path 推导（db_path = {data_dir}/giftthanks.db）
    data_dir = os.path.dirname(os.path.abspath(db_path))
    faces_dir = os.path.join(data_dir, "faces")
    os.makedirs(faces_dir, exist_ok=True)

    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Cookie": f"SESSDATA={sessdata}",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        wbi_keys = await _get_wbi_keys(session)
        if wbi_keys is None:
            logger.error("无法获取 Wbi 密钥，跳过用户信息补全")
            return

        # 收集所有 uid → face_url
        face_urls: dict[int, str] = {}
        batches = _chunk_uids(uids)
        for i, batch in enumerate(batches):
            if i > 0:
                await asyncio.sleep(_REQUEST_INTERVAL)  # 请求间隔 ≥1 秒
            face_urls.update(await _fetch_user_cards(session, wbi_keys, batch))

        # 逐个下载头像；失败用默认占位头像
        for uid in uids:
            save_path = os.path.join(faces_dir, f"{uid}.jpg")
            ok = False
            face_url = face_urls.get(uid)
            if face_url:
                ok = await _download_face(session, face_url, save_path)
            if not ok:
                default = _default_avatar_path()
                if os.path.exists(default):
                    save_path = default
                else:
                    logger.warning("uid=%s 头像下载失败且无默认头像，跳过回填", uid)
                    continue
            try:
                set_user_face_path(db_path, uid, save_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("回填 uid=%s 头像路径失败：%s", uid, exc)
    logger.info("头像补全完成：共 %d 个用户", len(uids))
