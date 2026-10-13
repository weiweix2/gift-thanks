"""blivedm 弹幕监听（M1）。

监听 SEND_GIFT / SUPER_CHAT_MESSAGE / GUARD_BUY，忽略 COMBO_SEND（防止连击重复计数）。

消息解析逻辑全部写成纯函数（入参是原始 dict），便于脱离网络做单元测试；
网络连接、断线重连、开播/下播场次管理都在 GiftListener 内。

blivedm 安装：pip install git+https://github.com/xfgryujk/blivedm.git
字段名以 blivedm 源码 blivedm/models/web.py 为准：
- GiftMessage:        data['uid']/['uname']/['face']/['giftId']/['giftName']/['num']
                      /['coin_type']/['total_coin']，礼物图在 data['gift_info']['img_basic']
- SuperChatMessage:   data['uid']/['price']（元）/['message']，昵称头像在 data['user_info']['uname'/'face']
- GuardBuyMessage:    data['uid']/['username']/['guard_level']/['price']（金瓜子，198000=198元）
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Any, Callable, Optional

from gift_thanks import db as _db

logger = logging.getLogger(__name__)

# blivedm / aiohttp 属于运行期依赖，测试环境可能没有网络安装，导入失败只影响连接不影响纯函数
try:
    import aiohttp

    import blivedm
    import blivedm.models.web as web_models

    _HAS_BLIVEDM = True
except ImportError:  # pragma: no cover - 无网络环境
    aiohttp = None
    blivedm = None
    web_models = None
    _HAS_BLIVEDM = False
    logger.warning("blivedm/aiohttp 未安装，GiftListener 无法连接直播间（解析纯函数仍可用）")

# B站接口
ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 未开播时 room_init 返回的占位 live_time
_NOT_LIVE_TIME = "0000-00-00 00:00:00"

# 下播后延迟确认秒数（10分钟），确认期间若复播则保持场次
END_CONFIRM_DELAY = 600.0
# 确认时若仍在播，每隔该秒数再查一次，最多再查 END_CONFIRM_MAX_RECHECK 次
END_CONFIRM_RECHECK_INTERVAL = 60.0
END_CONFIRM_MAX_RECHECK = 5

# 断线重连：指数退避，1s 起步封顶 60s
RECONNECT_BASE_INTERVAL = 1.0
RECONNECT_MAX_INTERVAL = 60.0

# 礼物卡片渲染专用线程：playwright 同步 API 不能在 asyncio 事件循环线程里
# 直接调用（会抛 "Sync API inside the asyncio loop"），单线程执行器保证
# 所有卡片渲染都在同一线程，配合 giftcard 的按线程浏览器实例可安全复用。
_CARD_RENDER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="gift-card-render")


# ---------------------------------------------------------------------------
# 纯函数：原始消息 dict -> 入库 record dict
# ---------------------------------------------------------------------------

def _new_record(event_type: str) -> dict:
    """按 gifts 表列生成默认值记录"""
    return {
        "event_type": event_type,
        "uid": 0,
        "uname": "",
        "face_url": "",
        "gift_name": "",
        "gift_id": 0,
        "num": 0,
        "total_coin": 0,
        "coin_type": "",
        "rmb": 0.0,
        "guard_level": 0,
        "sc_message": "",
        "gift_img_url": "",
        "card_path": None,
    }


def parse_send_gift(data: dict) -> dict | None:
    """解析 SEND_GIFT 的 data 字段。金瓜子礼物 rmb=total_coin/1000；银瓜子（免费）rmb=0"""
    try:
        record = _new_record("gift")
        record["uid"] = int(data.get("uid") or 0)
        record["uname"] = str(data.get("uname") or "")
        record["face_url"] = str(data.get("face") or "")
        record["gift_name"] = str(data.get("giftName") or "")
        record["gift_id"] = int(data.get("giftId") or 0)
        record["num"] = int(data.get("num") or 0)
        record["coin_type"] = str(data.get("coin_type") or "")
        record["total_coin"] = int(data.get("total_coin") or 0)
        # 礼物图：blivedm 取 data['gift_info']['img_basic']；兜底 data 顶层 img 字段
        gift_info = data.get("gift_info") or {}
        record["gift_img_url"] = str(gift_info.get("img_basic") or data.get("img") or "")
        # 金额换算：只有 gold（金瓜子）计入金额，1000 金瓜子 = 1 元；silver 免费礼物 rmb=0
        if record["coin_type"] == "gold":
            record["rmb"] = record["total_coin"] / 1000.0
        else:
            record["rmb"] = 0.0
        return record
    except (TypeError, ValueError, KeyError):
        logger.exception("解析 SEND_GIFT 失败")
        return None


def parse_super_chat(data: dict) -> dict | None:
    """解析 SUPER_CHAT_MESSAGE 的 data 字段。price 单位是人民币元"""
    try:
        user_info = data.get("user_info") or {}
        record = _new_record("sc")
        record["uid"] = int(data.get("uid") or 0)
        record["uname"] = str(user_info.get("uname") or "")
        record["face_url"] = str(user_info.get("face") or "")
        record["rmb"] = float(data.get("price") or 0)
        record["sc_message"] = str(data.get("message") or "")
        record["guard_level"] = int(user_info.get("guard_level") or 0)
        return record
    except (TypeError, ValueError, KeyError):
        logger.exception("解析 SUPER_CHAT_MESSAGE 失败")
        return None


def parse_guard_buy(data: dict) -> dict | None:
    """解析 GUARD_BUY 的 data 字段。price 单位是金瓜子，198000 = 198 元；guard_level 1总督/2提督/3舰长"""
    try:
        record = _new_record("guard")
        record["uid"] = int(data.get("uid") or 0)
        record["uname"] = str(data.get("username") or "")
        record["face_url"] = ""  # GUARD_BUY 不带头像，留给 M2 userinfo 补全
        record["guard_level"] = int(data.get("guard_level") or 0)
        record["num"] = int(data.get("num") or 1)
        record["rmb"] = float(data.get("price") or 0) / 1000.0
        # 用 B站自带的礼物名/ID，便于海报明细展示
        record["gift_name"] = str(data.get("gift_name") or "")
        record["gift_id"] = int(data.get("gift_id") or 0)
        return record
    except (TypeError, ValueError, KeyError):
        logger.exception("解析 GUARD_BUY 失败")
        return None


# cmd -> 解析函数；COMBO_SEND 故意不在这里（连击消息忽略，防止重复计数）
_PARSERS: dict[str, Callable[[dict], dict | None]] = {
    "SEND_GIFT": parse_send_gift,
    "SUPER_CHAT_MESSAGE": parse_super_chat,
    "GUARD_BUY": parse_guard_buy,
}


def parse_command(command: dict) -> tuple[str, dict] | None:
    """把一条原始弹幕命令 dict 解析成 (kind, record)；不关心的命令（含 COMBO_SEND）返回 None。

    kind ∈ 'gift' | 'sc' | 'guard'，与 record['event_type'] 一致。
    """
    cmd = str(command.get("cmd") or "")
    # 2019-5-29 B站弹幕升级后 cmd 可能带 ':xxx' 后缀，去掉再匹配
    pos = cmd.find(":")
    if pos != -1:
        cmd = cmd[:pos]
    parser = _PARSERS.get(cmd)
    if parser is None:
        return None
    data = command.get("data")
    if not isinstance(data, dict):
        return None
    record = parser(data)
    if record is None:
        return None
    return record["event_type"], record


# ---------------------------------------------------------------------------
# 监听器
# ---------------------------------------------------------------------------

class GiftListener:
    """直播间礼物监听器。

    :param room_id: 直播间房间号（可用短号）
    :param sessdata: 登录 SESSDATA cookie（游客连接昵称会打星号、UID 归 0）
    :param db_path: SQLite 文件路径
    :param data_dir: 数据目录（礼物卡片/头像等落地位置）
    :param on_event: 事件回调 on_event(kind: str, payload: dict)，
                     kind ∈ 'gift'|'sc'|'guard'|'live_start'|'live_end'|'error'|'status'
    """

    def __init__(self, room_id: int, sessdata: str, db_path: str, data_dir: str,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        self.room_id = int(room_id)
        self.sessdata = sessdata
        self.db_path = db_path
        self.data_dir = data_dir
        self.on_event = on_event

        self._session: Any = None  # aiohttp.ClientSession
        self._client: Any = None  # blivedm.clients.BLiveClient
        self._session_id: Optional[int] = None  # 当前场次
        self._live_time: str = ""  # 当前场次键（room_init 返回的 live_time）
        self._end_confirm_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ------------------------------------------------------------------ 回调

    def _emit(self, kind: str, payload: dict) -> None:
        """回调 GUI；回调异常绝不能让监听崩溃"""
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:
            logger.exception("on_event 回调异常 kind=%s", kind)

    # ------------------------------------------------------------------ HTTP

    def _make_session(self):
        """带 SESSDATA 登录态的 aiohttp 会话"""
        jar_cookies = {"SESSDATA": self.sessdata} if self.sessdata else {}
        return aiohttp.ClientSession(
            headers={"User-Agent": _USER_AGENT},
            cookies=jar_cookies,
            timeout=aiohttp.ClientTimeout(total=10),
        )

    async def _fetch_room_init(self) -> dict:
        """room_init：拿真实 room_id 与 live_time（场次键）。失败返回空 dict"""
        try:
            async with self._session.get(ROOM_INIT_URL, params={"id": self.room_id}) as res:
                data = await res.json(content_type=None)
            if data.get("code") == 0:
                return data.get("data") or {}
            logger.warning("room_init 失败: %s", data.get("message"))
        except Exception:
            logger.exception("room_init 请求异常")
        return {}

    async def _fetch_live_status(self) -> Optional[int]:
        """get_info 的 live_status：1 直播中，0/2 未播/轮播；失败返回 None"""
        try:
            async with self._session.get(ROOM_INFO_URL, params={"room_id": self.room_id}) as res:
                data = await res.json(content_type=None)
            if data.get("code") == 0:
                return int((data.get("data") or {}).get("live_status", 0))
            logger.warning("get_info 失败: %s", data.get("message"))
        except Exception:
            logger.exception("get_info 请求异常")
        return None

    # ------------------------------------------------------------------ 场次

    def _start_session(self, live_time: str) -> None:
        """开播：以 live_time 为场次键 start_session（复用未结束场次）"""
        if not live_time or live_time == _NOT_LIVE_TIME:
            # 拿不到场次键时用当前时间兜底，保证礼物仍有归属场次
            live_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self._live_time = live_time
        self._session_id = _db.start_session(self.db_path, self.room_id, live_time)
        logger.info("场次开始/复用 session_id=%s live_time=%s", self._session_id, live_time)
        self._emit("live_start", {"session_id": self._session_id, "live_time": live_time})

    def _on_preparing(self) -> None:
        """收到 PREPARING/下播：延迟10分钟确认没复播再 end_session"""
        if self._session_id is None:
            return
        if self._end_confirm_task is not None and not self._end_confirm_task.done():
            return  # 已有确认任务在跑
        self._end_confirm_task = asyncio.create_task(self._confirm_end_later())

    async def _confirm_end_later(self) -> None:
        """延迟10分钟后轮询 live_status，确认没复播才结束场次"""
        try:
            await asyncio.sleep(END_CONFIRM_DELAY)
            for _ in range(END_CONFIRM_MAX_RECHECK + 1):
                if self._stopping:
                    return
                status = await self._fetch_live_status()
                if status != 1:  # None（查询失败）也按未复播处理，结束场次
                    break
                # 复播了，过一会儿再确认
                await asyncio.sleep(END_CONFIRM_RECHECK_INTERVAL)
            else:
                # 一直复播中，保持场次不结束
                return
            if status == 1:
                return
            if self._session_id is not None:
                _db.end_session(self.db_path, self._session_id)
                logger.info("场次结束 session_id=%s", self._session_id)
                self._emit("live_end", {"session_id": self._session_id})
                self._session_id = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("下播确认任务异常")

    # ------------------------------------------------------------------ 礼物

    async def _handle_gift_record(self, kind: str, record: dict) -> None:
        """入库 -> 渲染礼物卡片回填 card_path -> upsert_user -> 回调 GUI"""
        if self._session_id is None:
            # 未收到 LIVE 也在收礼物（中场接入）：用 room_init 的 live_time 开一场
            init_data = await self._fetch_room_init()
            self._start_session(str(init_data.get("live_time") or ""))
        record["session_id"] = self._session_id

        gift_pk = _db.insert_gift(self.db_path, record)

        # 生成礼物卡片 PNG，失败不阻塞监听，card_path 留 NULL。
        # 渲染放到专用线程：同步 playwright 不能在 asyncio 事件循环线程里跑。
        try:
            from gift_thanks.giftcard import render_gift_card

            gift_row = dict(record)
            gift_row["id"] = gift_pk
            loop = asyncio.get_running_loop()
            card_path = await loop.run_in_executor(
                _CARD_RENDER_EXECUTOR, render_gift_card, self.data_dir, gift_row)
            if card_path:
                _db.set_gift_card_path(self.db_path, gift_pk, card_path)
        except Exception:
            logger.exception("礼物卡片渲染失败 gift_pk=%s（已忽略，不影响监听）", gift_pk)

        # 顺便维护用户表（uname/face_url；face_path 由 M2 补全）
        if record.get("uid"):
            try:
                _db.upsert_user(self.db_path, record["uid"],
                                record.get("uname") or "", record.get("face_url") or "")
            except Exception:
                logger.exception("upsert_user 失败 uid=%s", record.get("uid"))

        payload = dict(record)
        payload["gift_pk"] = gift_pk
        self._emit(kind, payload)

    # ------------------------------------------------------------------ 主流程

    def _build_handler(self):
        """把原始命令 dict 分发到场次/礼物处理；COMBO_SEND 在这里被忽略"""
        listener = self

        class _RawHandler:
            """实现 blivedm HandlerInterface：handle 收原始 command dict"""

            def handle(self, client, command: dict):
                cmd = str(command.get("cmd") or "")
                pos = cmd.find(":")
                if pos != -1:
                    cmd = cmd[:pos]
                if cmd == "COMBO_SEND":
                    return  # 连击消息忽略，只计 SEND_GIFT，否则重复计数
                if cmd == "LIVE":
                    listener._start_session(listener._live_time)
                    return
                if cmd in ("PREPARING", "STOP_LIVE_ROOM_LIST"):
                    listener._on_preparing()
                    return
                parsed = parse_command(command)
                if parsed is None:
                    return
                kind, record = parsed
                # 入库+渲染卡片是 IO，丢进独立协程，不阻塞弹幕接收
                asyncio.create_task(listener._handle_gift_record(kind, record))

            def on_client_stopped(self, client, exception):
                if exception is not None:
                    logger.warning("弹幕连接断开: %r", exception)
                    listener._emit("error", {"message": f"弹幕连接断开: {exception!r}"})
                else:
                    listener._emit("status", {"message": "弹幕连接已停止"})

        return _RawHandler()

    async def start(self) -> None:
        """连接直播间并阻塞运行；断线由 blivedm 自动重连（指数退避，封顶60s）"""
        if not _HAS_BLIVEDM:
            raise RuntimeError("blivedm/aiohttp 未安装，无法启动监听")
        self._stopping = False
        self._session = self._make_session()

        # 连接前先用 room_init 拿场次键；已在播则立即开场（中途启动也能归场）
        init_data = await self._fetch_room_init()
        self._live_time = str(init_data.get("live_time") or "")
        if self._live_time and self._live_time != _NOT_LIVE_TIME:
            self._start_session(self._live_time)

        self._client = blivedm.clients.BLiveClient(self.room_id, session=self._session)
        self._client.set_handler(self._build_handler())

        def _reconnect_policy(retry_count: int, total_retry_count: int) -> float:
            # 指数退避：1s, 2s, 4s ... 封顶 60s
            interval = min(RECONNECT_BASE_INTERVAL * (2 ** max(retry_count - 1, 0)),
                           RECONNECT_MAX_INTERVAL)
            self._emit("status", {"message": f"第{total_retry_count}次重连，{interval:.0f}秒后重试"})
            return interval

        self._client.set_reconnect_policy(_reconnect_policy)

        self._emit("status", {"message": "正在连接直播间"})
        self._client.start()
        try:
            await self._client.join()
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        """停止监听；当前未结束场次立即 end_session（手动停止即视为本场结束）"""
        self._stopping = True
        if self._end_confirm_task is not None and not self._end_confirm_task.done():
            self._end_confirm_task.cancel()
            try:
                await self._end_confirm_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session_id is not None:
            _db.end_session(self.db_path, self._session_id)
            self._emit("live_end", {"session_id": self._session_id})
            self._session_id = None
        if self._client is not None and self._client.is_running:
            self._client.stop()

    async def _cleanup(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop_and_close()
            except Exception:
                logger.exception("关闭弹幕客户端异常")
            self._client = None
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                logger.exception("关闭HTTP会话异常")
            self._session = None
