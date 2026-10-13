# 礼物答谢姬（GiftThanks）

给 **B站主播** 用的 Windows 桌面小工具：直播时自动记录每一件礼物，下播后一键生成一张好看的 **感谢海报**（礼物榜、人气榜、醒目留言），方便发到动态或粉丝群表达谢意。

不需要任何技术基础：双击安装、扫码登录、填房间号，剩下的交给它。

## 下载安装

1. 打开 [Releases 页面](../../releases)，下载最新版的 `GiftThanks-Setup-1.0.0.exe`。
2. 双击运行，一路「下一步」即可（安装在自己用户目录下，**不需要管理员权限**）。
3. 桌面会出现「礼物答谢姬」快捷方式，双击打开就能用。

> 首次生成海报时，如果电脑里没有可用的浏览器组件，程序会自动下载一次渲染组件（约 170MB），只需下载这一次，之后离线也能生成海报。

## 功能

- **扫码登录 B 站**：用 B 站 App 扫二维码，和网页版登录一样安全。
- **礼物监听**：填写直播间房间号后，自动记录礼物、醒目留言（SC）、大航海，实时显示件数和总金额。
- **礼物卡片**：每件礼物即时生成一张精美卡片图。
- **感谢海报**：下播后一键生成整场直播的感谢海报（金额榜 + 人气榜 + 随机感谢文案）。
- **断线重连**：网络抖动自动重连，礼物记录不丢失（本地 SQLite 存储）。

## 隐私说明

- 登录凭证（cookie）**只保存在你自己电脑上**，经加密后存放于 `%APPDATA%\GiftThanks\cred.bin`，不会上传到任何服务器。
- 所有礼物数据都存在本地数据库，随时可以整个删掉 `%APPDATA%\GiftThanks\` 目录清除。
- 本项目完全开源，以上每一条都可以直接查证源代码。

## 使用方法

1. 打开软件，用 **B 站 App** 扫二维码登录。
2. 输入你的 **直播间房间号**，点「开始监听」。
3. 直播结束后点「停止监听」→「生成感谢海报」。
4. 点「打开海报文件夹」就能拿到图片。

## 开发者本地运行

环境：Python 3.11+，Windows / macOS / Linux 均可开发调试。

```bash
git clone <本仓库地址>
cd gift-thanks-app

# 装依赖（blivedm 从 GitHub 安装，需要本机有 git）
pip install -r requirements.txt

# 打包/测试用字体（约16MB，不入库）
# 下载 https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf
# 保存为 gift_thanks/templates/fonts/NotoSansSC-Regular.otf

# 浏览器渲染组件（海报/卡片截图用）
python -m playwright install chromium

# 运行
python -m gift_thanks.main

# 跑测试
python -m pytest tests/ -q
```

## 打包安装包（Windows）

推一个 `v*` 开头的 tag，GitHub Actions 会自动构建并发布 Release：

```bash
git tag v1.0.0
git push origin v1.0.0
```

本地手动打包流程见 `.github/workflows/build.yml`（PyInstaller spec 在 `packaging/gift_thanks.spec`，Inno Setup 脚本在 `packaging/installer.iss`）。

## 技术栈

Python 3.11 · PySide6（界面） · blivedm（B站直播弹幕协议） · Playwright（HTML 渲染截图） · Jinja2（模板） · SQLite（本地存储）
