# 第三方组件、技术来源与许可

感谢各项目的作者和贡献者。本文以公开源码与 **1.0.0-test.4** 构建产物为核对对象（2026-10-17）；项目原创代码的 [MIT](../LICENSE) 不替代第三方许可。技术用途摘要见[技术致谢](ACKNOWLEDGEMENTS.md)，游戏及截图来源见[项目与素材说明](PROJECT-NOTICE.md)。已发布的 test.1/test.2/test.3 旧包内附的旧说明仍是历史版本。

## 核对依据

- [依赖声明](../requirements.txt) 是安装下限，不是 test.4 的锁定版本清单。
- 本轮发布包的构建版本以构建日志 `.temp/build-evidence/` 与本机构建环境实测为准；下表「test.4 构建实际版本」列只写能直接确认的版本。
- [构建脚本](../scripts/build_portable.py) 使用 PyInstaller 目录模式，并把**已校验的内置 OCR 运行时**、品牌资源和许可一起放进冻结目录；测试 ZIP 附 `THIRD-PARTY-NOTICES.md`、`licenses/`、模型许可、OCR 运行时来源说明（`OCR-RUNTIME.txt`）。包内许可全文与 NOTICE 应保留，不能只留下本页摘要。
- 当前用户安装器由 [Inno Setup](https://jrsoftware.org/isinfo.php) 6.7.3 编译（仅构建期工具，不进入包内业务代码）；简体中文界面使用 Inno Setup 官方仓库的 `ChineseSimplified.isl`，随源码放在 `scripts/installer/`。
- 本页不能替代对 OCR 运行时全部传递 DLL 的逐项审计；未完成部分在下面明确标出。

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

**Tesseract 程序（test.4 起随安装包分发）**：[官方项目](https://github.com/tesseract-ocr/tesseract)，Apache-2.0。分发版内置 `ocr/` 运行时：`tesseract.exe`、`libtesseract-5.dll`、按 PE 导入表求出的传递 DLL 闭包，以及 `tessdata/eng.traineddata`（来自项目自己的模型副本）。用户不再需要安装第二个 OCR 安装器。

- 运行时代码来源：官方 [5.5.3 Windows 发布安装器](https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe)，SHA-256 `bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4`。
- 选择方式与逐文件字节数 / SHA-256：`scripts/ocr-runtime-manifest.json`；生成工具 `scripts/ocr_runtime.py`（开发机一次性动作，构建时只校验并复制，不联网）。
- 随包许可：`licenses/tesseract/LICENSE`（Apache-2.0 全文）、`AUTHORS`、`LEPTONICA-LICENSE.txt`（Leptonica，BSD-2-Clause）与上游 README。
- **未完成边界**：安装器内的其余 MinGW 依赖 DLL（GCC runtime、libjpeg、libpng、libtiff、libarchive、zlib、libwebp、openjpeg 等）需按上游各自许可核对。本轮固定了来源安装器与逐文件摘要，仍未完成这些二进制所对应的许可全文、版本及再分发依据核对。test.4 候选包仅供本地验收；公开分发前必须补齐。包内 `licenses/tesseract/runtime-manifest.json` 列出实际 35 个文件，不能用某个库的大类许可覆盖全部 DLL。该运行时未做代码修改。

源码运行（不打包分发）时，程序仍可自动探测用户自装的 Tesseract；源码树本身不附 Tesseract 可执行文件/DLL。

**OCR 模型**：来自 [tessdata_fast 固定提交](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd)，Apache-2.0。同时用于随包 OCR 运行时的 `tessdata/eng.traineddata`（同一份字节，构建时校验与 `provenance.json` 一致）。

公开源码保留九个模型文件（简体中文及英、西、葡、法、德、日、韩、俄）、[完整许可](../app/recognition/models/LICENSE) 和 [provenance.json](../app/recognition/models/provenance.json)，包含固定提交、逐文件大小与 SHA-256；模型未修改。冻结包对应路径为 `_internal/app/recognition/models/`。模型范围属于实现记录，合成素材验证不代表真实对局效果。

**品牌图形**：用户选定的 C 版「圆眼徽章 + 双气泡」图标，由项目内置 imagegen 生成并做确定性尺寸/格式导出，未重绘；总览见[品牌素材包](assets/locklingo/README.md)。品牌图形不改变 Tesseract 或游戏素材的权利归属。

## 构建与可选源码依赖

test.4 构建实际版本（2026-10-17 本机构建环境，随 `licenses/` 收集许可全文）：

| 组件 | test.4 构建实际版本 | 许可与随包位置 |
| --- | --- | --- |
| Python | 3.13.14 | PSF 许可；`licenses/Python-Tcl-Tk/` |
| Tcl / Tk | Tcl 8.6.12（构建环境实测） | 上游原许可；另附 `[licenses/Tcl-8.6.12.txt](licenses/Tcl-8.6.12.txt)` |
| NumPy | 2.5.4 | BSD-3-Clause 及包内第三方声明；`licenses/numpy/` |
| opencv-python | 5.0.0.93 | Python 包装层 MIT + 随附第三方声明；`licenses/opencv-python/` |
| Pillow | 12.3.0 | MIT-CMU；`licenses/pillow/` |
| pytesseract | 0.3.13 | Apache-2.0；`licenses/pytesseract/` |
| ttkbootstrap / Bootstrap Icons | 2.2.3 / 图标版本未确认 | MIT；`licenses/ttkbootstrap/` |
| packaging | 26.3 | Apache-2.0 或 BSD；`licenses/packaging/` |
| PyInstaller | 6.22.3（构建工具） | GPL-2.0-or-later + Bootloader Exception；`licenses/pyinstaller/` |
| pystray | 0.19.5（源码可选依赖；2026-10-18 起构建包含 Windows 托盘） | LGPL-3.0；新构建以可替换的 `.py` 模块置于 `_internal/pystray/`，保留上游声明及 `licenses/pystray/` 中的 COPYING、COPYING.LGPL；已生成的 test.4 旧包仍不含此组件 |
| six | 构建时以实际包元数据为准 | MIT；随 pystray 使用，原许可全文放入 `licenses/six/` |

以上版本来自本轮构建环境的包元数据；安装包内的实际文件以包内 `manifest.json` 与 `licenses/` 为准。旧版 test.1/test.2/test.3 的版本记录见历史交付报告，不能与本表混写。

基础 CI 使用 GitHub 官方 [checkout](https://github.com/actions/checkout) 与 [setup-python](https://github.com/actions/setup-python)，工作流固定提交摘要；它们用于仓库检查，不进入助手下载包。

## 游戏、图片与字体

`app/translation/game_terms.json` 来自本机安装的 Valve 游戏本地化文件配对整理，包含来源摘要，并随 test.3 分发；其单独再分发授权依据尚未核实。具体边界见[项目与素材说明](PROJECT-NOTICE.md)，不能概括成“不包含任何游戏数据表”或“全部内容均属项目 MIT”。

当前公开截图是新版助手界面的真实截图，没有伪造译文或使用 AI 插画。源码未附游戏字体；运行时可本机私有加载游戏字体。Windows 包另附 Bootstrap Icons 图标字体及许可。私人游戏截图和人工真值材料未公开。

## 更新清单

新增或升级依赖、引用外部代码、模型、字体或图片时，应记录：实际版本/固定提交、原作者及官方来源、用途、修改情况、许可/NOTICE 原文与包内位置、再分发依据。将源码依赖、构建工具、外部服务和实际打包组件分别核对；无法确认的项目保持待核实状态，并在发布前处理。详见[贡献说明](CONTRIBUTING.md)与[发布规范](releases/README.md)。
