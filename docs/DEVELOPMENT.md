# 开发说明

## 源码运行

使用 Windows 64 位、Python 3.10 以上及已安装的 Tesseract OCR，在仓库根目录执行：

```powershell
pip install -r requirements.txt
python -m app
```

也可双击 `启动.vbs`；它优先使用本机可选的 `.venv-signed/Scripts/pythonw.exe`，然后是 `.venv/Scripts/pythonw.exe`，再查 PATH。目录名不代表签名有效：创建环境后须核验实际文件。业务在 `app/`，界面在 `gui/`，冻结入口为 `scripts/run.py`。用户数据保持 `<程序目录>/data`，改品牌不迁移或清空数据。源码版需要自备 Python/OCR；内置 OCR 的候选安装包尚未公开下载。

发行版以 `LockLingo.exe` 直接启动 GUI，`DeadlockTranslate.exe` 保留诊断与 OCR 管道协议。两者共享同一冻结依赖目录，不再通过 VBS 隐藏控制台；无控制台打包不等于有效代码签名。

## 公开源码基础检查

在 Python 3.13 中执行，无需应用依赖和 OCR：

```powershell
python scripts/check_public.py --syntax
python -m unittest discover -s tests -p test_portable_build.py -v
python -m unittest discover -s tests -p test_installer_build.py -v
python -m unittest discover -s tests -p test_public_export.py -v
python scripts/check_public.py --links
```

现有 CI 只验证语法、便携包隐私边界和本地文档文件目标；安装器输入与公开导出的额外用例已在本地运行。Windows/Tk、热键、真实 API、OCR 与整局效果需另验，详见[测试状态](TESTING.md)。公开树不包含私人截图、完整人工真值、data 或开发历史，因此不能复现所有本机测试。

## 内置 OCR 与当前用户安装器

在 64 位 Windows 准备运行依赖、PyInstaller、pefile、7-Zip 与 Inno Setup 6.7+。工具只用于构建，不把开发 Python 环境或用户配置直接复制进包。

```powershell
# 从固定官方安装器解出运行时；来源和摘要见脚本及第三方清单
python scripts/ocr_runtime.py prepare --installer "C:\Downloads\tesseract-ocr-w64-setup-5.5.3.20260724.exe" --sevenzip "C:\Program Files\7-Zip\7z.exe" --output .temp/ocr-runtime
python scripts/ocr_runtime.py verify
python scripts/build_portable.py --version 1.0.0-test.4-rc.2
python scripts/build_installer.py --archive dist/LockLingo-1.0.0-test.4-rc.2-windows-x64.zip --version 1.0.0-test.4-rc.2
```

`scripts/build.bat <版本>` 依次运行校验、ZIP 与安装器构建，失败立即停止并保留退出码。工具拒绝覆盖同名产物；OCR prepare 拒绝覆盖已有目录。VERSION 当前为源码候选号，正式构建请显式指定未占用版本。

冻结目录包含 Python、品牌、模型和按 35 文件清单校验的 OCR；`BUILD-SOURCE.json` 记录实际输入摘要。安装器按当前用户安装，默认 `%LOCALAPPDATA%/Programs/LockLingo`，保留 `data`，运行中的独立面板/助手会阻止覆盖。具体操作见[安装与首次使用](guides/安装与首次使用.md)。

这仍是候选流程：普通用户安装/升级/卸载、真实快捷方式与干净 Windows 验收尚未完成；OCR 传递 DLL 的许可材料仍待核对，当前不公开发布新二进制。来源、版本及许可边界见[第三方清单](THIRD-PARTY-NOTICES.md)。

## 定向验证与公开导出

`verify/portable_smoke.py` 检查冻结目录清单/凭据、C 图标、无系统 OCR 搜索路径时的 CLI/常驻合成识别、首次引导和退出；可用 `--setup` 验证运行中拒绝安装。`verify/installer_smoke.py` 准备安装生命周期的夹具数据对照，遇到现有产品安装或快捷方式会拒绝覆盖；本轮只实际执行到环境权限导致的安装失败，后续成功分支仍待验。

`scripts/prepare_public_source.py --public-base <干净公开克隆> --destination <新目录> --report <私有证据路径>` 以公开文件白名单合入当前源码；拒绝覆盖目标、不复制 Git 历史，检查真实密钥与本机路径。报告必须位于导出目录外。不要将私人开发仓库整体推送。

## 数据与反馈

首次引导支持 DeepSeek 预设和自定义兼容接口，保存不联网，可跳过；只有主动测试连接或开启联网翻译才发送请求。测试连接可能计费。修改接口后区分保存与运行应用确认。

请勿提交 API Key、完整 data 或私人聊天/截图。通过 Issues 提交版本、显示环境和复现步骤；设置页导出的反馈 ZIP 需先检查。真实游戏发送、整局覆盖率/延迟、多屏与完整 DPI 适配仍待验收。
