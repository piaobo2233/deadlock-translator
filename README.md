# 异锁聊天翻译助手 · Deadlock Translator

Windows 本地运行的《异锁》（Deadlock）聊天翻译工具，目前为公开测试版。

**[下载 Windows 测试包](https://github.com/piaobo2233/deadlock-translator/releases/tag/v1.0.0-test.1)** · [直接下载完整 ZIP](https://github.com/piaobo2233/deadlock-translator/releases/download/v1.0.0-test.1/DeadlockTranslator-1.0.0-test.1-windows-x64.zip) · [反馈问题](https://github.com/piaobo2233/deadlock-translator/issues)

## 功能

- 识别屏幕聊天气泡，将英语、西班牙语、葡萄牙语、法语和德语翻译为简体中文；正文含汉字时整条跳过。
- 输入中文，检查英文预览后复制，自行粘贴到游戏发送。
- 优先使用本地词库和有效结果，需要时调用你配置的在线 API。

![翻译主界面，使用演示数据，未连接游戏或 API](docs/assets/app-overview.png)

![中文转英文界面，使用演示数据，未调用真实 API](docs/assets/app-compose.png)

## 下载与使用

支持 Windows 10/11 64 位，无需安装 Python。

1. 下载 `DeadlockTranslator-1.0.0-test.1-windows-x64.zip`，完整解压，保留 exe 和 `_internal` 文件夹。
2. 首次运行包内 `首次安装OCR` 的官方安装器，保留默认位置和 English 组件；已有 Tesseract 可跳过。助手已附所需识别模型。
3. 双击 `启动.vbs`，在设置填写自己的 API Key、接口和模型，按需开启在线补充翻译。API 用量由服务商计费。
4. 打开游戏测试。默认只在游戏前台识别；关闭主面板默认退出助手。

详细说明：[首次使用与分享](docs/guides/首版测试与分享.md) · [接收与中文回复](docs/guides/接收与中文转英文.md)

## 已知限制与隐私

首版可能漏译、误识别或超时，不同分辨率、缩放和网络环境仍需验证；整局延迟与识别率尚未完成验收。手动翻译以 5 秒内完成为目标，实际取决于网络和服务商响应。

OCR 在本机完成，开启 API 后会向所配置的服务商发送待翻译文字。程序旁的 `data/` 保存配置、密钥和个人记录；分享时请使用初始完整 ZIP，勿重新压缩运行后的目录。

## 故障排查与反馈

无法启动时，先确认完整解压、OCR 已安装，再按下载页的 SHA-256 核对文件。测试包未做代码签名，不要关闭系统防护。

无法翻译时，检查游戏是否在前台，以及 API 设置、联网开关和网络状态。仍有问题请[提交 Issue](https://github.com/piaobo2233/deadlock-translator/issues)，附 Windows 版本、分辨率/缩放、游戏显示模式、代理情况和复现步骤。不要上传密钥或整个 `data`，截图请遮挡昵称。

## 开发与许可

源码运行、测试和构建方式见[开发说明](docs/DEVELOPMENT.md)。工具源码采用 [MIT](LICENSE)，第三方组件与模型许可见[第三方声明](docs/THIRD-PARTY-NOTICES.md)。本项目与 Valve 无关联，非官方产品。
