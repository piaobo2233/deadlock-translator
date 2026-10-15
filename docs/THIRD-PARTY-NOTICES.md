# 第三方组件与许可证

本文件列出《异锁》翻译助手实际使用到的第三方组件及其许可证。

首个 Windows 测试 ZIP 另附 Tesseract 官方项目发布的原始 Windows 安装器，由测试者首次自行安装；该安装器及其包含组件不是嵌入助手 exe 的代码。安装器来源与 SHA-256 放在包内 `首次安装OCR/来源与校验.txt`，安装和许可说明保留上游原样。下文“外部程序”指助手调用的已安装程序。

测试 ZIP 使用 PyInstaller 的目录模式，随附 Python、Tcl/Tk、所收集 Python 包和识别模型的许可文本。Tcl 8.6.15 的本机发行目录未带单独许可文件，补充自 [Tcl 官方对应版本](https://github.com/tcltk/tcl/blob/core-8-6-15/license.terms)，源码副本位于 `docs/licenses/Tcl-8.6.15.txt`。

随程序附带的 `app/recognition/models/` 中 `chi_sim`、`eng`、`spa`、`por`、`fra`、`deu` 六份 `.traineddata` 来自 [Tesseract 官方 tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)，按 Apache-2.0 分发。模型未修改，完整许可证见该目录 `LICENSE`；固定提交、大小及 SHA-256 见 `provenance.json`。六份模型合计约 13.5 MB，用于本机识别，源图不发送给模型提供方。常驻识别使用用户已安装的 Tesseract C API，不随源码分发 DLL。

> **本程序自身**以 [MIT](../LICENSE) 发布，与 Valve Corporation 无关联。

---

## 一、Python 依赖（`requirements.txt`）

| 包 | 用途 | 许可证 | 版权 |
|---|---|---|---|
| [opencv-python](https://github.com/opencv/opencv-python) | 截图分析、气泡检测、图像预处理 | **Apache-2.0** | Copyright (c) OpenCV Foundation / Intel Corporation |
| [numpy](https://numpy.org/) | 数组运算 | **BSD-3-Clause** | Copyright (c) 2005-2025, NumPy Developers |
| [Pillow](https://python-pillow.org/) | 截屏抓取（`PIL.ImageGrab`） | **MIT-CMU**（HPND） | Copyright © 2010-2025 by Jeffrey A. Clark and contributors |
| [pytesseract](https://github.com/madmaze/pytesseract) | Tesseract 的 Python 绑定 | **Apache-2.0** | Copyright (c) 2014-2024 Samuel Hoffstaetter, Matthias Lee |
| [ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap)（可选） | 控制面板观感 | **MIT AND (Apache-2.0 OR BSD-2-Clause)** | Copyright (c) 2019 Israel Dryer；含 Bootswatch 主题（Apache-2.0 / BSD-2-Clause） |
| [pystray](https://github.com/moses-palmer/pystray)（可选） | 系统托盘图标 | **LGPL-3.0** | Copyright (c) 2016 Moses Palmér |
| [pyinstaller](https://pyinstaller.org/)（仅打包用） | 生成单文件 exe | **GPL-2.0-or-later，附例外条款** | Copyright (c) 2010-2025, PyInstaller Development Team |

### 关于 LGPL 的 `pystray`

`pystray` 是**可选**依赖：没装它程序照常工作，只少了右下角的托盘图标
（见 `tray.py` 的降级逻辑，有回归测试守着）。

因此本项目的**分发策略**是：

- **分发的 exe 里不打包 `pystray`**。托盘作为「可选增强」，想要的人自己
  `pip install pystray` 即可。这样既不用把 LGPL 组件捆进单文件可执行体，
  也不损失任何翻译功能。
- 如果将来确实要把它打进 exe，必须同时提供该组件可替换的形式
  （例如附上 `pystray` 的源码与构建脚本），并在本文件里附 LGPL-3.0 全文。

### 关于 Apache-2.0

`opencv-python` 与 `pytesseract` 采用 Apache-2.0。**只要只是源码依赖（由用户自己
`pip install`），本仓库不承担再分发义务**；但一旦本项目**打包并分发 exe**
（PyInstaller 会把它们嵌进可执行体），就属于再分发，需要：

1. 随发布包附上 Apache-2.0 许可证全文；
2. 保留上游的版权、专利、商标与归属声明。

**发布清单里已列为阶段 6 的必办项。**

---

## 二、外部识别程序

| 组件 | 用途 | 许可证 | 说明 |
|---|---|---|---|
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | 屏幕文字识别 | **Apache-2.0** | 用户自行安装；首个测试 ZIP 附上游未修改安装器，来源与摘要见包内说明。源码不包含 `tesseract.exe` / `*.dll` |
| [tessdata](https://github.com/tesseract-ocr/tessdata)（`eng.traineddata`） | 英文语言包 | **Apache-2.0** | 用户安装 Tesseract 时勾选 English 一并装好 |

OCR 安装器包含的其他组件及相应许可由上游安装器原样提供；测试 ZIP 保留安装器，不从作者的安装目录抽取或改装 DLL。

---

## 三、字体

本仓库**不包含任何字体文件**（`*.ttf` / `*.otf` 数量为 0）。

程序在运行时从**用户本机的游戏目录**加载 `notosanssc-*.ttf` 用于浮窗渲染
（`AddFontResourceExW` + `FR_PRIVATE`：进程内私有加载，**不安装到系统、
不复制、不修改**游戏文件）。

界面本身使用 Windows 自带的 `Bahnschrift` / `Microsoft YaHei UI`，
只引用字体族名，不涉及字体文件分发。

---

## 四、游戏相关内容

仓库里**保留**以下与游戏有关的文件，均为本项目的测试素材或说明配图：

| 文件 | 说明 |
|---|---|
| `verify/truth/match_bubble.png` | 合成素材（文本是 `test789 push mid now`），回归测试用 |
| `verify/truth/bright_screen.png`<br>`verify/truth/wait2.png`<br>`verify/truth/long_chat.png` | 真实对局截图，**已脱敏**：玩家昵称与比赛 ID 已抹平，仅保留回归测试需要的画面与坐标 |
| `可视化/*.png` | 项目自己的示意图与流程图（游戏画面已涂黑或为合成内容） |

**不包含**任何游戏本体文件、可执行文件或数据表。

《异锁》（Deadlock）及相关名称、素材是 Valve Corporation 的商标或版权财产。
本项目与 Valve Corporation 无关联，未获得其授权、认可或赞助。

---

## 五、如何取得完整的许可证文本

- **Python 包**：每个包的发行版里都带着自己的 `LICENSE` 文件。装完之后可以这样找：

  ```powershell
  python -c "import importlib.metadata as m; print(m.metadata('opencv-python')['License'])"
  ```

  或直接访问上表里的上游仓库链接。

- **完整文本随发布包提供**：正式发布的 zip 里会附本文件，并在打包时把
  Apache-2.0 全文一并放进发布目录。

---

## 六、贡献者请注意

如果你提交的代码引入了新的第三方依赖，请**同时更新本文件**（包名、用途、许可证、版权）。
许可证是 `GPL` / `AGPL` 这类强传染性的库，本项目不能接受。
