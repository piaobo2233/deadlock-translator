# 第三方组件、技术来源与许可

本页适用于 **1.0.0**，复核日期为 2026-10-22。项目原创源码采用 [MIT](../LICENSE)；第三方代码、运行库、模型、字体和游戏内容分别遵循其适用条款。感谢相关项目的作者、维护者和贡献者。

复核以同版本公开附件为依据：六个附件均已匿名完整下载，SHA-256 与 GitHub 记录一致；便携包 1,330 项清单、41 个 OCR 文件及 32 个源码归档的摘要匹配。材料身份核对不等同于对全部组合关系和分发义务的最终确认。具体证据与待核实项见[1.0.0 材料复核](releases/v1.0.0-material-review.md)。

## 运行与界面组件

以下路径相对于便携包根目录。复合许可内的版权声明、历史许可和例外均应按随包原文保留。

| 组件与来源 | 版本证据及用途 | 随包许可位置 |
| --- | --- | --- |
| [Python](https://www.python.org/)、[Tcl/Tk](https://www.tcl-lang.org/) | 包内 DLL 资源：Python 3.13.14、Tcl/Tk 8.6.12；Tkinter 界面与运行环境 | `licenses/Python-Tcl-Tk/` |
| [NumPy](https://github.com/numpy/numpy) | 包内 METADATA：2.5.4；图像数组运算。BSD-3-Clause 及随附数学库声明 | `licenses/numpy/` |
| [OpenCV / opencv-python](https://github.com/opencv/opencv-python) | 图像检测和预处理；构建锁定 5.0.0.93，包内无独立 METADATA 供本次确认。包装层 MIT，内部组件见原文 | `licenses/opencv-python/` |
| [Pillow](https://github.com/python-pillow/Pillow) | 截图与图像处理；构建锁定 12.3.0。MIT-CMU 及随附组件条款 | `licenses/pillow/` |
| [pytesseract](https://github.com/madmaze/pytesseract) | Python OCR 接口；许可目录标识 0.3.13，Apache-2.0 | `licenses/pytesseract/` |
| [ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap)、[Bootstrap Icons](https://github.com/twbs/icons) | 可选主题与图标字体；构建锁定 ttkbootstrap 2.2.3，图标字体精确版本待核实，MIT | `licenses/ttkbootstrap/` |
| [packaging](https://github.com/pypa/packaging) | 版本处理；构建锁定 26.3，Apache-2.0 或 BSD | `licenses/packaging/` |
| [pystray](https://github.com/moses-palmer/pystray) | Windows 托盘；包内含可修改的 `.py` 模块，许可目录标识 0.19.5，LGPL-3.0 | `licenses/pystray/`、`_internal/pystray/` |
| [six](https://github.com/benjaminp/six) | pystray 的兼容依赖；构建锁定 1.17.0，MIT | `licenses/six/` |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 冻结工具和引导程序；构建锁定 6.22.3，GPL-2.0-or-later 与 Bootloader Exception | `licenses/pyinstaller/` |

[构建锁定](../requirements-build.txt)描述构建输入，不能替代包内版本证据；本页不将缺少元数据的版本标为已独立确认。详细许可及版权人以各目录原文为准。

## OCR 程序、传递运行库与模型

1.0.0 使用 MSYS2 mingw64 固定包：40 个 EXE/DLL 和一个英文模型。34 个二进制包的锁定记录、32 个对应源码归档及逐文件映射见[包锁定](releases/ocr-msys2-packages.lock.json)、[源码映射](releases/ocr-msys2-sources.json)和[OCR 审计](releases/ocr-runtime-audit.json)。原始许可与构建元数据保存在 `docs/licenses/msys2/`，随包位于 `licenses/tesseract/components/`。本项目未修改这些上游二进制。

[Tesseract](https://github.com/tesseract-ocr/tesseract) 本体采用 Apache-2.0；Leptonica、压缩及图像库、GNU/MinGW 运行库分别适用其原许可和例外。现有材料记录对涉及 GPL 的 OCR 组合选择 GPL-3.0-or-later 路径；组合范围和完整对应源码义务仍需结合实际加载与构建关系复核，不能将整个 OCR 目录概括为 Apache-2.0，也不能仅凭目录独立或 DLL 可替换判定完成。

对应源码：[LockLingo-1.0.0-ocr-sources.zip](https://github.com/piaobo2233/deadlock-translator/releases/download/v1.0.0/LockLingo-1.0.0-ocr-sources.zip)，附[SHA-256](https://github.com/piaobo2233/deadlock-translator/releases/download/v1.0.0/LockLingo-1.0.0-ocr-sources.zip.sha256)。归档内包含 MSYS2 源码包，其配方和补丁位于各 `.src.tar.zst` 内；不表示覆盖了 Python 包、引导程序和其他组件的所有源码提供义务。公开应用源码见 [v1.0.0 标签](https://github.com/piaobo2233/deadlock-translator/tree/v1.0.0)。

[tessdata_fast 固定提交](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd) 的九个模型按 Apache-2.0 分发，未修改；[provenance.json](../app/recognition/models/provenance.json)记录逐文件摘要，[模型许可](../app/recognition/models/LICENSE)保留原文。随包英文 OCR 模型与该来源对应；模型数量不代表真实对局语言效果已经验收。

## 引用实现与构建材料

本机游戏资料解析 `app/game_data/valve.py` 参考并适配 [ValveResourceFormat 固定提交](https://github.com/ValveResourceFormat/ValveResourceFormat/tree/73f5ee5a02531f946b5f3e83dc79b264097cf8d3)，源码头部记录修改来源；MIT 原文及署名见[上游许可](licenses/ValveResourceFormat/LICENSE)，随包保留 `licenses/ValveResourceFormat/LICENSE`。

[Inno Setup](https://jrsoftware.org/isinfo.php) 6.7.3 用于构建当前用户安装器；语言文件见 `scripts/installer/`，须按其原始条款保留来源。GitHub Actions 的 checkout、setup-python 用于仓库检查。构建工具与实际分发代码的许可范围分别核对。

品牌图、截图、系统字体引用和游戏派生数据的来源与边界见[项目与素材说明](PROJECT-NOTICE.md)。[技术致谢](ACKNOWLEDGEMENTS.md)说明各项目用途，不能替代许可、NOTICE 或对应源码。

## 历史版本

test.3、test.4 的原运行库及 beta.1 记录见[历史材料](releases/third-party-history.md)。当前材料不为旧附件补作结论。旧标签及附件保持原字节；网页修订不会改变包内说明。
