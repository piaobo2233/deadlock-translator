# 技术致谢

感谢以下项目的作者、维护者和贡献者。本页说明技术用途；第三方原许可、已发布包版本及核对边界见[第三方清单](THIRD-PARTY-NOTICES.md)。

| 项目 | 在本工具中的作用 |
| --- | --- |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | 本机文字识别；常驻辅助进程使用已安装的 C API，另有命令行后备路径 |
| [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) | 随源码和测试包提供的 OCR 模型，保留固定提交与逐文件摘要 |
| [pytesseract](https://github.com/madmaze/pytesseract) | Python 侧 Tesseract 接口 |
| [OpenCV / opencv-python](https://github.com/opencv/opencv-python) | 气泡与正文检测、图像预处理 |
| [NumPy](https://numpy.org/) | 图像数组与数值运算 |
| [Pillow](https://python-pillow.org/) | 截图采集与图像处理 |
| [Python](https://www.python.org/)、[Tcl](https://github.com/tcltk/tcl)、[Tk](https://github.com/tcltk/tk) | 运行环境、Tkinter 界面与外部浮窗 |
| [ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap) | 可选界面主题与控件样式 |
| [Bootstrap Icons](https://github.com/twbs/icons) | ttkbootstrap 随包附带的图标字体资源 |
| [PyInstaller](https://pyinstaller.org/) | Windows 目录模式打包 |
| [packaging](https://github.com/pypa/packaging) | 所收集依赖使用的版本处理组件 |
| [pystray](https://github.com/moses-palmer/pystray) | 可选源码托盘路径；test.3 下载包未打包此组件 |

感谢反馈可复现问题的玩家。引用外部代码、算法或素材时，应提供能定位到原作者、原文或原始提交的链接，并保留其声明；本页不将依赖项目或服务商列为本工具的合作方。

自带 API 预设仅用于连接用户自行选择的服务，不代表服务商对本项目的背书。本项目主要介绍英文聊天场景，不用未实测语言或未来性能目标作为效果宣传。
