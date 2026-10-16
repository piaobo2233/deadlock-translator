# 第三方组件、技术来源与许可

感谢各项目的作者和贡献者。本文以公开源码与已发布 **1.0.0-test.3** ZIP 为核对对象（2026-10-17）；项目原创代码的 [MIT](../LICENSE) 不替代第三方许可。技术用途摘要见[技术致谢](ACKNOWLEDGEMENTS.md)，游戏及截图来源见[项目与素材说明](PROJECT-NOTICE.md)。

## 核对依据

- [依赖声明](../requirements.txt) 是安装下限，不是 test.3 的锁定版本清单。
- [构建脚本](../scripts/build_portable.py) 使用 PyInstaller 目录模式；测试 ZIP 附 `THIRD-PARTY-NOTICES.md`、`licenses/`、模型许可及原始 OCR 安装器。包内许可全文与 NOTICE 应保留，不能只留下本页摘要。
- 表内版本来自现有构建日志、ZIP 的 METADATA、许可目录名或冻结 PYZ 的静态版本常量；不能从这些材料确认的版本明确标为未确认。后续构建应另存完整依赖清单。
- 本次仅整理仓库说明，未修改已发布 ZIP。test.3 内附的旧说明仍是历史版本；本页纠正其字体、术语来源、可选依赖及许可概括等表述，不声称完成了全部传递组件或安装器内部审计。

## 运行与界面组件

以下许可位置均相对于解压后的 test.3 目录，路径以实际包为准。

| 组件 / 官方来源 | test.3 可确认版本 | 许可与归属证据 | 随包位置 |
| --- | --- | --- | --- |
| [Python](https://www.python.org/) | 3.13.14（构建日志） | Python Software Foundation 及其附带历史许可 | `licenses/Python-Tcl-Tk/` 中的 `LICENSE.txt` |
| [Tcl](https://github.com/tcltk/tcl) / [Tk](https://github.com/tcltk/tk) | Tcl 8.6.15；Tk 精确补丁版本未确认 | 上游 Tcl/Tk 原许可；另有[对应 Tcl 许可副本](licenses/Tcl-8.6.15.txt) | `licenses/Python-Tcl-Tk/` |
| [NumPy](https://github.com/numpy/numpy) | 2.3.5（包内 METADATA） | NumPy Developers，主许可 BSD-3-Clause；原文件另含 OpenBLAS、LAPACK、GCC runtime 等声明及例外，不能全部简化为 BSD | `licenses/numpy/` |
| [opencv-python](https://github.com/opencv/opencv-python) | 4.13.0.92（许可目录） | Python 包装层 MIT，Olli-Pekka Heinisuo；OpenCV 与其余组件见随附第三方声明 | `licenses/opencv-python/` 的 `LICENSE.txt` 与 `LICENSE-3RD-PARTY.txt` |
| [Pillow / PIL](https://python-pillow.org/) | 12.2.0（冻结版本常量） | MIT-CMU；Secret Labs AB、Fredrik Lundh、Jeffrey ‘Alex’ Clark 及贡献者；原文另含 Brotli、FreeType 等声明 | `licenses/pillow/licenses/LICENSE` |
| [pytesseract](https://github.com/madmaze/pytesseract) | 0.3.13（许可目录） | Apache-2.0；具体作者声明以发行版与上游为准，不沿用未经核实的旧年份 | `licenses/pytesseract/` |
| [ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap) | 未确认 | 随包主许可 MIT，`Copyright (c) 2021–2026 Israel Dryer` | `licenses/ttkbootstrap/licenses/LICENSE` |
| [Bootstrap Icons](https://github.com/twbs/icons) | 精确图标版本未确认 | MIT，`2019–2024 The Bootstrap Authors`；[上游贡献者](https://github.com/twbs/icons/graphs/contributors) | `licenses/ttkbootstrap/icons/LICENSE`；资源为 `_internal/ttkbootstrap/assets/icons/bootstrap.ttf` |
| [packaging](https://github.com/pypa/packaging) | 26.1（冻结版本常量） | Apache-2.0 或 BSD；BSD 原文署名 Donald Stufft and individual contributors | `licenses/packaging/licenses/LICENSE`、`LICENSE.APACHE`、`LICENSE.BSD` |

ttkbootstrap 版本常量为 `unknown`，包内未保留其发行版 METADATA，不能据此补写精确版本。版本未确认不表示组件未使用，也不能用当前机器安装的新版填入旧包版本。随包复合许可中的署名、年份和例外按原文保留。

## OCR 程序与模型

**Tesseract 程序**：[官方项目](https://github.com/tesseract-ocr/tesseract)，Apache-2.0。助手调用用户已安装的程序或 C API。源码不附 Tesseract 可执行文件/DLL；test.3 ZIP 另附上游原始 Windows 安装器，用户自行安装，未从开发机安装目录抽取 DLL。

- 安装器：[5.5.3 上游发布文件](https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe)。
- SHA-256：`bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4`。
- 包内来源记录：`首次安装OCR/来源与校验.txt`。安装器内部可能含其他组件，本轮未逐项审计；安装器原有许可和来源说明不能被本页概括替代。

**OCR 模型**：来自 [tessdata_fast 固定提交](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd)，Apache-2.0。它们不是程序安装器的版本，也不能混写为泛指 tessdata 的来源。

公开源码保留六个模型文件、[完整许可](../app/recognition/models/LICENSE) 和 [provenance.json](../app/recognition/models/provenance.json)，包含固定提交、逐文件大小与 SHA-256；模型未修改。本轮重新核对文件摘要匹配。冻结包对应路径为 `_internal/app/recognition/models/`。模型范围属于实现记录，不构成其他语言已实测的声明。

## 构建与可选源码依赖

| 项目 | 状态 | 许可与依据 |
| --- | --- | --- |
| [PyInstaller](https://pyinstaller.org/) | 构建工具 6.20.0；contrib hooks 2026.4（构建日志） | GPL-2.0-or-later，附 Bootloader Exception；运行时 hooks 另有 Apache-2.0。完整例外与版权原文见包内 `licenses/pyinstaller/licenses/COPYING.txt`，不能省略例外或自行更改年份 |
| [pystray](https://github.com/moses-palmer/pystray) | 源码可选托盘依赖；test.3 构建明确排除 | 上游 LGPL-3.0；本轮未分发其源码。以后若纳入包，需另核对实际版本与分发材料 |
| [defusedxml](https://github.com/tiran/defusedxml)、[darkdetect](https://github.com/albertosottile/darkdetect) | 构建脚本列入可选许可收集候选；静态核对 test.3 冻结 PYZ 模块表与 ZIP 文件后未发现这两个组件 | 不列为 test.3 内置依赖；以后环境若收集这些组件，需同时记录实际版本和许可 |

基础 CI 使用 GitHub 官方 [checkout](https://github.com/actions/checkout) 与 [setup-python](https://github.com/actions/setup-python)，工作流固定提交摘要；它们用于仓库检查，不进入助手下载包。

## 游戏、图片与字体

`app/translation/game_terms.json` 来自本机安装的 Valve 游戏本地化文件配对整理，包含来源摘要，并随 test.3 分发；其单独再分发授权依据尚未核实。具体边界见[项目与素材说明](PROJECT-NOTICE.md)，不能概括成“不包含任何游戏数据表”或“全部内容均属项目 MIT”。

当前公开截图是新版助手界面的真实截图，没有伪造译文或使用 AI 插画。源码未附游戏字体；运行时可本机私有加载游戏字体。Windows 包另附 Bootstrap Icons 图标字体及许可。私人游戏截图和人工真值材料未公开。

## 更新清单

新增或升级依赖、引用外部代码、模型、字体或图片时，应记录：实际版本/固定提交、原作者及官方来源、用途、修改情况、许可/NOTICE 原文与包内位置、再分发依据。将源码依赖、构建工具、外部服务和实际打包组件分别核对；无法确认的项目保持待核实状态，并在发布前处理。详见[贡献说明](CONTRIBUTING.md)与[发布规范](releases/README.md)。
