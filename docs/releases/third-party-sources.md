# 1.0.1 第三方源码与替换说明

本版沿用 1.0.0 的固定 OCR 输入及 Python 组件版本。各组件许可与声明保留在发行包 `licenses/`；本版额外保留 Python 组件的 `METADATA`，便于核对实际构建输入。

- OCR、GNU/MinGW 运行库和图像依赖的完整 MSYS2 源码包、配方及补丁：同一 Release 的 `LockLingo-1.0.1-ocr-sources.zip`，逐包摘要见 [OCR 源码映射](ocr-msys2-sources.json)。映射中的历史资产名保留，归档内容逐项匹配。
- LGPL 托盘组件 pystray 0.19.5 的上游源码、许可及构建工具源码：同一 Release 的 `LockLingo-1.0.1-third-party-sources.zip`。归档内记录来源 URL、版本和 SHA-256；上游代码未修改。
- 应用原创源码、构建脚本与冻结规范：[v1.0.1 标签](https://github.com/piaobo2233/deadlock-translator/tree/v1.0.1)。OCR worker 由同一公开源码构建；涉及 GPL 的 OCR 组合按包内 `OCR-RUNTIME.txt` 及相应原文条款提供，原创 MIT 声明保留。

pystray 的 Windows 实现在 `_internal/pystray/` 中以普通 `.py` 文件提供，保留其依赖 six。退出所有程序后，可在自有副本中替换该目录并运行；修改后的目录摘要会与原包不同，自动更新器可能拒绝覆盖，需先自行保存修改，再手动安装。没有对自有副本中的调试或替换加技术锁。

PyInstaller 的引导程序按上游 GPL 与 Bootloader Exception 分发，完整条款见 `licenses/pyinstaller/`；Inno Setup 安装器使用原版编译器，许可见其上游来源。归档提供构建材料不等于逐字节重建上游二进制的保证。

1.0.0 的历史附件、元数据缺失和材料边界保留在[历史复核](v1.0.0-material-review.md)；本说明不替旧包补作新结论。
