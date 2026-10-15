# 开发说明

## 源码运行

使用 Windows 64 位、Python 3.10 以上及 Tesseract OCR，在仓库根目录执行：

```powershell
pip install -r requirements.txt
python -m app
```

也可双击根目录 `启动.vbs`。业务实现位于 `app/`，界面位于 `gui/`，冻结程序入口为 `scripts/run.py`。OCR 模型保留在 `app/recognition/models/`。

## 针对性检查

以下检查不依赖私人对局素材：

```powershell
python -m unittest discover -s tests -p test_manual_waiting.py -v
python -m unittest discover -s tests -p test_portable_build.py -v
python -m unittest discover -s tests -p test_composer.py -v
```

首版发行时，24 项 Windows 定向检查通过，并验证冻结程序启动、常驻 OCR 与主窗口退出；公开源码中的上述 11 项检查通过。完整开发回归仍有 9 项失败、1 项错误，以及旧回归 156/157，与发行前基线一致。这些是首版验证记录，不表示之后每个提交都运行了全量测试。

部分识别回归依赖未公开的人工标注与私人截图，公开仓库不附这些素材，无法据此复现完整识别率与延迟验收。整局 90%/2 秒目标尚未验收。

## Windows 测试包构建

在 64 位 Windows 上安装运行依赖与 PyInstaller，下载 `scripts/build_portable.py` 中指定的官方 OCR 安装器，并核对固定 SHA-256：

```powershell
pip install pyinstaller
python scripts/build_portable.py --ocr-installer "C:\Downloads\tesseract-ocr-w64-setup-5.5.3.20260724.exe" --installer-sha256 bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4
```

将示例路径替换为实际下载位置。工具从允许的源码与资源构建，排除用户数据；结果写入 `dist/`，已有同名 ZIP 时拒绝覆盖。源码 VERSION 为 1.0.0，默认测试包版本为 1.0.0-test.1。实际发布 ZIP 的检查入口为 `verify/portable_smoke.py`，参数见 `--help`。

## 反馈与数据

请通过 Issues 报告可复现的问题。设置页可导出反馈 ZIP，提交前自行检查；不要提交 API Key、完整 `data/`、私人聊天或未经脱敏的截图。必要的模型与第三方许可随源码保留，见 [第三方声明](THIRD-PARTY-NOTICES.md)。
