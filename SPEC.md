# SPEC.md — 礼物答谢姬（GiftThanks）

B站直播礼物监听 + 礼物卡片截图 + 下播感谢海报生成。Windows 桌面软件，Python 3.11。

## 总体架构

```
gift_thanks/
├── __init__.py
├── db.py          # SQLite 数据层（M1）
├── listener.py    # blivedm 弹幕监听（M1）
├── auth.py        # 扫码登录+cookie加密（M2）
├── userinfo.py    # 批量用户信息补全（M2）
├── giftcard.py    # 单个礼物→礼物卡片PNG（M3）
├── poster.py      # 场次聚合→感谢海报PNG（M3）
├── copypool.py    # 随机感谢文案池（M3）
├── templates/     # HTML模板与字体（M3）
│   ├── gift_card.html
│   ├── poster.html
│   └── fonts/
├── gui.py         # PySide6 主窗口（M4）
└── main.py        # 入口（M4）
requirements.txt   # M4 维护
```

## 数据层契约（M1: db.py）

SQLite 文件：`{数据目录}/giftthanks.db`。数据目录 = Windows 下 `%APPDATA%/GiftThanks/`（用 `os.environ['APPDATA']`，无则回退用户主目录）。

```python
# db.py
def init_db(db_path: str) -> None
def start_session(db_path: str, room_id: int, live_time: str) -> int  # 返回 session_id；若存在 end_ts IS NULL 且 live_time 相同的会话则复用
def end_session(db_path: str, session_id: int) -> None
def insert_gift(db_path: str, record: dict) -> int  # record 见下
def get_session_gifts(db_path: str, session_id: int) -> list[dict]
def get_latest_session(db_path: str, room_id: int) -> dict | None
def upsert_user(db_path: str, uid: int, uname: str, face_url: str) -> None
def get_users_missing_face(db_path: str, session_id: int) -> list[int]
def set_user_face_path(db_path: str, uid: int, face_path: str) -> None
```

表结构：
- `sessions(id INTEGER PRIMARY KEY, room_id INTEGER, live_time TEXT, start_ts REAL, end_ts REAL)`
- `gifts(id INTEGER PRIMARY KEY, session_id INTEGER, ts REAL, event_type TEXT,  # 'gift'|'sc'|'guard'
  uid INTEGER, uname TEXT, face_url TEXT, gift_name TEXT, gift_id INTEGER, num INTEGER,
  total_coin INTEGER, coin_type TEXT, rmb REAL, guard_level INTEGER, sc_message TEXT,
  gift_img_url TEXT, card_path TEXT)`
- `users(uid INTEGER PRIMARY KEY, uname TEXT, face_url TEXT, face_path TEXT, updated_ts REAL)`

金额规则：gift → rmb = total_coin/1000（coin_type='gold' 才计，silver 记 rmb=0）；sc → rmb = price（元）；guard → rmb = price/1000（198000=198元）。

## 监听层契约（M1: listener.py）

基于 blivedm（GitHub 安装：`git+https://github.com/xfgryujk/blivedm.git`）。监听 SEND_GIFT、SUPER_CHAT_MESSAGE、GUARD_BUY；**忽略 COMBO_SEND**（防重复计数）。

```python
class GiftListener:
    def __init__(self, room_id: int, sessdata: str, db_path: str, data_dir: str,
                 on_event=None):  # on_event(kind:str, payload:dict) 供GUI刷新；kind ∈ 'gift'|'sc'|'guard'|'live_start'|'live_end'|'error'|'status'
    async def start(self) -> None   # 内部：连接、循环、断线重连（指数退避，最多60s）
    async def stop(self) -> None
```

- 收到 LIVE → start_session（用 room_init 拿的 live_time 作场次键）；收到 PREPARING/下播 → 延迟10分钟确认仍未开播再 end_session（轮询 room/v1/Room/get_info）。
- 每条礼物事件先 insert_gift，再调 giftcard.render_gift_card 生成卡片图，回填 card_path（卡片渲染失败不阻塞监听，card_path 留 NULL）。
- uid/uname/face_url 顺便 upsert_user。

## 登录层契约（M2: auth.py）

```python
# auth.py
def generate_qr() -> tuple[str, str]      # (qrcode_key, 二维码PNG本地路径)；调 passport.bilibili.com/x/passport-login/web/qrcode/generate，本地用 qrcode 库生成图片
def poll_qr(qrcode_key: str) -> tuple[str, dict|None]  # ('waiting'|'scanned'|'success'|'expired', cookies)
def save_cookies(cookies: dict) -> None   # Fernet 加密存 {APPDATA}/GiftThanks/cred.bin；密钥=SHA256(机器GUID+固定盐)前32字节urlsafe
def load_cookies() -> dict | None         # 解密失败/不存在返回 None
def get_sessdata() -> str | None
```

# userinfo.py
async def fill_missing_faces(db_path: str, session_id: int, sessdata: str) -> None
# 对 get_users_missing_face 的 uid，用 https://api.vc.bilibili.com/account/v1/user/cards?uids= 批量（≤50/次）查 face，
# 下载头像到 {data_dir}/faces/{uid}.jpg（空Referer，UA伪装浏览器，超时10s，失败用内置默认头像），set_user_face_path 回填。
# 需 Wbi 签名；请求间隔≥1秒，-352/412 时退避重试最多3次。

## 海报层契约（M3）

```python
# copypool.py
SHORT_LINES: list[str]   # ≥30条，含 {name} 占位符，一句感谢语
LONG_PARAS: list[str]    # ≥15段，每段约100字，含 {name}（主播名）占位符
def pick_short(name: str) -> str
def pick_long(streamer_name: str) -> str

# giftcard.py
def render_gift_card(data_dir: str, gift_row: dict) -> str  # 返回PNG路径 {data_dir}/cards/{gift_id_pk}.png
# 用 templates/gift_card.html 渲染：送礼人头像+昵称+礼物图(gift_img_url，加载失败用占位)+礼物名×数量+金额+时间
# Playwright chromium，viewport 800x200，device_scale_factor=2

# poster.py
def generate_poster(db_path: str, data_dir: str, session_id: int,
                    streamer_name: str, out_dir: str) -> str  # 返回海报PNG路径
# 聚合 get_session_gifts：按 uid 汇总 rmb（gold礼物+sc+guard），排序取榜一/二/三；
# 榜1-3卡片：头像(本地face_path)+昵称+UID+送礼明细(礼物名×数量 列表)+rmb总额+对应礼物卡片缩略图+pick_short
# 其余送礼人名单区：头像+昵称+总额，多列网格
# 底部：pick_long 约100字短文 + 日期时间（下播时间，格式 2026年10月13日 22:30）
# 免费礼物（rmb=0）单独计"人气榜"按数量，不进金额榜
# templates/poster.html 宽1080px，full_page截图，2x分辨率；头像等图片用 base64 data URI 内联
# 字体：templates/fonts/ 内打包开源中文字体（思源黑体），@font-face 引用
```

## GUI 契约（M4）

```python
# gui.py  class MainWindow(QMainWindow)
# 界面元素（简体中文）：
#  - 顶部：扫码区（二维码图片+状态文字"请用B站App扫码"/"已登录：xxx"）、「重新扫码」按钮
#  - 房间号输入框 + 「开始监听」/「停止监听」按钮
#  - 状态区：当前场次状态、本场礼物件数/总金额、最近5条礼物滚动列表
#  - 底部：「生成感谢海报」按钮（对最近一场已结束会话，或手动停止后的当前场）、「打开海报文件夹」按钮
#  - 首次启动无cookie→自动弹扫码；登录成功显示当前用户昵称与头像
#  - listener 跑在独立 QThread（asyncio 事件循环），通过 signal 回调主线程刷新UI
# main.py: 创建数据目录、init_db、启动 QApplication + MainWindow
```

## requirements.txt（M4 维护）
```
blivedm @ git+https://github.com/xfgryujk/blivedm.git
PySide6>=6.7
playwright>=1.45
cryptography
qrcode[pil]
Pillow
aiohttp
Jinja2
```
打包时需在构建机执行 `playwright install chromium` 并将浏览器打进安装包（集成阶段处理）。

## 全局约定
- 全程中文注释、中文界面文案；日志写 {data_dir}/app.log（logging，RotatingFileHandler）
- 任何网络失败都不能让程序崩溃；卡片/头像失败用占位图兜底
- 敏感：cookie 只写本地加密文件，绝不上传、绝不进日志
- 所有模块不写打包逻辑（PyInstaller/Inno/Actions 由主代理集成阶段统一做）
