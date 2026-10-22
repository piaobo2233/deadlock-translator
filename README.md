# 锁语 · LockLingo

锁语是面向《异锁》（Deadlock）的 Windows 聊天翻译工具，通过本机 OCR 读取聊天；本地未命中时，可按用户设置调用翻译服务。中文译英由用户主动开启，译文写入剪贴板后自行粘贴和发送。

[![公开源码检查](https://github.com/piaobo2233/deadlock-translator/actions/workflows/public-checks.yml/badge.svg?branch=main)](https://github.com/piaobo2233/deadlock-translator/actions/workflows/public-checks.yml)

**1.0.0 · Windows 10/11 x64 · 内置 OCR**

[下载安装包](https://github.com/piaobo2233/deadlock-translator/releases/download/v1.0.0/LockLingo-1.0.0-setup.exe) · [全部下载与校验](https://github.com/piaobo2233/deadlock-translator/releases/tag/v1.0.0) · [使用说明](docs/guides/安装与首次使用.md) · [更新日志](docs/CHANGELOG.md) · [反馈问题](https://github.com/piaobo2233/deadlock-translator/issues/new/choose)

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 接收翻译 | 本机 OCR 读取聊天，默认译为简体中文，以外部字幕气泡显示；面板可阅读和复制全文。 |
| 游戏复制译英 | 主动开启后，在游戏复制中文，获取英文剪贴板结果，自行粘贴发送；切出游戏取消旧输出。 |
| 个人识别区域 | 不开游戏也可导入局内截图，拖框、放大、微调和本地测试，保存后在同尺寸画面启用。 |
| 本地优先 | 优先使用有效词库、记忆和缓存，未命中时按开关请求用户配置的 API。 |
| 词库与审核 | 编辑、导入导出、审核及重新审核；后台校验游戏资料，切页保留草稿。 |
| 配置与用量 | 自定义兼容接口、保存与生效状态、连接测试、请求明细与费用趋势。 |

完整功能及版本变化见[功能清单](docs/releases/v1.0.0-features.md)和[发布说明](docs/releases/v1.0.0.md)。

## 开始使用

1. 下载并运行安装包，选择**纯英文可写目录**，例如 `D:\LockLingo`。无需另外安装 Python 或 OCR；也可下载便携 ZIP，完整解压后运行 `LockLingo.exe`。
2. 首次设置填写自己的 API Key，或跳过并先使用本地词库。API 由所选服务商计费；保存设置本身不联网。
3. 按需准备本机游戏资料并重启，在接收页开启联网翻译。区域有偏差时，到“识别与显示”导入完整原始局内截图调整。
4. 需要游戏内中文译英时，在“发言翻译”主动开启，再自行复制、粘贴与发送。此功能默认关闭，助手不会自动按键或发送。

**beta.1 安装用户可在设置中“检查更新”升级至 1.0.0。** 下载校验后安全退出、备份，再启动安装；建议保持原目录。便携版、源码版和 test.3 等更早版本手动升级。更换目录需自行迁移 `data`，见[安装与数据保留](docs/guides/安装与首次使用.md)及[软件更新](docs/guides/软件更新.md)。

## 界面

<img src="docs/assets/deadlock-ui/receive.png" width="900" alt="锁语接收翻译页面">

历史版本的真实 Tk 界面，使用隔离演示数据；精确截图版本待核实。此图展示界面风格，当前操作以 1.0.0 说明为准。

## 数据与适用范围

OCR 在本机执行；启用 API 后，待翻译文字发送给用户配置的服务商。个人配置、密钥、词库及私有记录保存在程序目录的 `data/`，更新备份也仅保存在本机。请勿上传完整数据、备份或私人聊天。游戏复制译英成功后会覆盖剪贴板。

主要使用场景是主屏全屏/无边框。副屏、窗口化和黑边映射未支持；4K、超宽、各档 DPI、自然对局指标及长时间运行的完整矩阵尚未验收。可能漏译、误识别或超时，不承诺固定延迟、准确率或帧率；本版不提供自动 HUD 定位。安装包未数字签名，Windows 可能提示未知发布者。

[测试状态](docs/TESTING.md) · [个人区域操作](docs/guides/个人识别区域.md) · [收发说明](docs/guides/接收与中文转英文.md) · [开发与构建](docs/DEVELOPMENT.md)

## 许可、致谢与反馈

原创源码采用 [MIT](LICENSE)。感谢 Tesseract OCR、tessdata_fast、OpenCV、NumPy、Pillow、Python / Tcl/Tk、ttkbootstrap、pystray 与 PyInstaller 等项目。第三方组件、模型、字体和引用实现分别适用其原有条款；许可、对应源码及待核实项见[第三方清单](docs/THIRD-PARTY-NOTICES.md)和[技术致谢](docs/ACKNOWLEDGEMENTS.md)。

感谢所使用开源项目的作者、维护者、贡献者及反馈问题的玩家。本项目由社区独立维护，非 Valve 官方产品。《异锁》（Deadlock）及相关内容归原权利人，见[项目与素材说明](docs/PROJECT-NOTICE.md)。问题反馈请使用 [Issues](https://github.com/piaobo2233/deadlock-translator/issues/new/choose)，安全问题见[私密反馈说明](.github/SECURITY.md)。
