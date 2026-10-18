# 锁语 · LockLingo

LockLingo · 尝试帮你看懂英文聊天，也方便用中文准备回复。

[![公开源码基础检查](https://github.com/piaobo2233/deadlock-translator/actions/workflows/public-checks.yml/badge.svg?branch=main)](https://github.com/piaobo2233/deadlock-translator/actions/workflows/public-checks.yml)

**Windows 10/11 · 64 位**　|　**1.0.0-test.4-rc.1 源码候选**　|　[MIT 原创源码](LICENSE)

**[已发布版本](https://github.com/piaobo2233/deadlock-translator/releases)** · [安装与首次使用](docs/guides/安装与首次使用.md) · [反馈问题](https://github.com/piaobo2233/deadlock-translator/issues/new/choose)

> main 已同步新版源码与 C 版品牌。test.4-rc.1 安装包目前仅作本地验收，干净 Windows、安装生命周期及 OCR 传递组件许可材料尚待完成；公开下载仍为 test.3，需要手动安装 OCR。

通过聊天区截图和本机 OCR 识别英文，结合本地词库、记忆、缓存与可选 API 翻译，在外部浮窗显示中文。发言页可以输入中文、检查英文预览，再复制到游戏。安装包已内置 Python 运行环境和 OCR，**不需要另外安装 Python 或 Tesseract**；安装按当前用户进行，默认装到 `%LOCALAPPDATA%\Programs\LockLingo`。

## 界面预览

<img src="docs/assets/app-overview.png" width="900" alt="test.3 实际接收界面：六页导航、空配置，未启动实时识别">

*test.3 新包实际界面，使用临时空配置；没有启动实时识别。这是界面展示。*

<details>
<summary>查看中文发言界面</summary>

![test.3 实际发言页：仅填写示例原文，译文为空](docs/assets/app-compose.png)

仅填写示例中文，未发起 API 请求，未模拟成功回填或发送。

</details>

## 三步开始

1. **安装**：运行 `LockLingo-<版本>-setup.exe`，按当前用户安装到默认目录并创建桌面快捷方式。**没有第二个 OCR 安装步骤**；也提供免安装测试 ZIP（解压后双击 `启动.vbs`）。
2. **首次设置**：第一次启动会打开「首次设置」，选择翻译服务商、填写自己的 API Key（DeepSeek 预设带出地址与模型，自定义兼容接口可改）。可先跳过，只用离线词库；保存本身不联网。
3. **进入游戏后使用**：需要在线翻译时在接收页打开「联网翻译」。服务商可能收费。中文回复先在发言页预览、检查，再复制发送。关闭主面板默认退出。

升级直接运行新安装包，`data` 中的个人数据默认保留；从旧解压包迁移见[安装与首次使用](docs/guides/安装与首次使用.md)。ZIP / 安装包与 SHA-256 见[版本页](https://github.com/piaobo2233/deadlock-translator/releases)。

> 界面预览图仍为 `test.3` 实际截图（品牌接入前），新一轮品牌界面与首次设置页的截图在补充前不代替实际包内界面。

## 功能与当前状态

| 能力 | 当前边界 |
| --- | --- |
| 英文聊天 → 简体中文 | 已实现屏幕识别与翻译链；可能漏译、误识别或超时，整局效果仍待验收 |
| 中文输入 → 英文预览 → 复制 | 已实现；需要自行检查译文，联网翻译需自备 API |
| 词库、记忆与缓存 | 优先查找本地有效结果，未命中且允许联网时请求配置接口 |
| 六页导航与草稿 | 切页保留本次窗口期间的草稿，接口显式保存，过期预览禁用复制 |
| 游戏内快捷键回填 / 自动发送 | **测试中，真实游戏流程仍待验收**；自动发送默认关闭 |

主要面向**主屏 16:9 全屏 / 无边框**。副屏、超宽屏、窗口化、完整 DPI 适配与性能尚未充分验证；不承诺覆盖率、延迟或帧率。测试包未签名。

## 技术路线

```text
主屏聊天区域 → 气泡/正文检测 → 本机 Tesseract OCR 与多帧筛选
           → 词库 → 记忆 → 缓存 → 按需请求 API → 外部浮窗
中文回复   → 翻译预览 → 人工检查 → 复制
```

接收端以屏幕识别为入口；OCR 在本机完成，启用 API 后传输待译文字。浮窗随原气泡有效性显示。具体机制见[开发说明](docs/DEVELOPMENT.md)，验证范围见[测试状态](docs/TESTING.md)。CI 徽章仅代表公开源码基础检查。

## 隐私与反馈

`data/` 保存个人配置、密钥和记录。分享请使用原始 ZIP；提交问题前遮挡昵称并检查日志，勿上传密钥、私人聊天或完整 `data`。游戏输入流程会短暂使用剪贴板，详见[收发说明](docs/guides/接收与中文转英文.md)。

- [报告问题 / 提出功能建议](https://github.com/piaobo2233/deadlock-translator/issues/new/choose)
- [贡献说明](docs/CONTRIBUTING.md) · [安全问题私密报告](.github/SECURITY.md)
- [文档目录](docs/README.md) · [更新日志](docs/CHANGELOG.md)

## 感谢与版权

感谢 [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)、[tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)、[OpenCV](https://github.com/opencv/opencv-python)、[NumPy](https://numpy.org/)、[Pillow](https://python-pillow.org/)、[pytesseract](https://github.com/madmaze/pytesseract)、[Python / Tcl/Tk](https://www.python.org/)、[ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap)、[Bootstrap Icons](https://github.com/twbs/icons) 与 [PyInstaller](https://pyinstaller.org/) 的作者和贡献者。

用途与技术来源见[技术致谢](docs/ACKNOWLEDGEMENTS.md)；实际版本、许可、随包声明及待核实项见[第三方清单](docs/THIRD-PARTY-NOTICES.md)。项目原创源码使用 MIT，第三方组件、游戏文本与素材保留各自的权利归属。

《异锁》（Deadlock）及相关游戏内容归原权利人。本项目为独立社区工具，与 Valve 无关联，不代表其认可或赞助。术语数据和截图来源见[项目与素材说明](docs/PROJECT-NOTICE.md)。
