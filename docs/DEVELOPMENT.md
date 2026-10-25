# 开发与构建

## 源码运行

使用 Windows x64、Python 3.10 以上和可用的 Tesseract OCR，在仓库根目录执行：

```powershell
pip install -r requirements.txt
python -m app
```

也可双击 `启动.vbs`，按 `.venv-signed`、`.venv`、PATH 的顺序查找解释器；目录名不代表已签名。业务实现位于 `app/`，界面位于 `gui/`，源码入口为 `python -m app` 或 `scripts/run.py`。用户数据路径为 `<程序目录>/data`。

1.0.0 安装包和便携包已内置 Python 与 OCR。`LockLingo.exe` 打开 GUI，`DeadlockTranslate.exe` 用于诊断及 OCR 辅助进程协议。下载入口见[发布说明](releases/v1.0.0.md)。

## 公开基础检查

在 Python 3.13 环境执行：

```powershell
python scripts/check_public.py --syntax --links
python -m unittest discover -s tests -p test_portable_build.py -v
python -m unittest discover -s tests -p test_installer_build.py -v
python -m unittest discover -s tests -p test_public_export.py -v
```

公开 CI 另有 Windows 离线子集，覆盖 Tk、更新、存储、安装输入和启动器；不请求真实 API，也不执行真实安装。完整开发检查为 `python -X utf8 scripts/check.py`；公开树不含私人真值，不能复现全部私人素材回归。各项证据的范围见[测试状态](TESTING.md)。

## 固定构建输入

1.0.0 构建使用 Windows x64、CPython 3.13.14，以及 [requirements-build.txt](../requirements-build.txt) 的精确版本。运行依赖的最低版本声明不能代替发行锁定；版本固定也不代表可逐字节复现。

当前 OCR 输入是按摘要锁定的 MSYS2 包，默认运行库目录为 `.temp/ocr-msys2-runtime`。历史 `ocr_runtime.py prepare` 的官方安装器路线不适用于当前 MSYS2 字节，不能将旧安装器解出结果混入新清单。公开源码不附这些二进制输入；需要按[包锁定](releases/ocr-msys2-packages.lock.json)、[源码映射](releases/ocr-msys2-sources.json)和[运行库清单](../scripts/ocr-runtime-manifest.json)重建或取得完全匹配的输入，再验证：

```powershell
python -m pip install -r requirements-build.txt
python -m pip check
python scripts/ocr_runtime.py verify --runtime .temp/ocr-msys2-runtime
```

安装器使用 Inno Setup 6.7.3。输入准备完整后，使用与源码 VERSION 一致、且目标文件不存在的版本构建：

```powershell
python scripts/build_portable.py --version <与VERSION一致的新版本>
python scripts/build_installer.py --archive dist/LockLingo-<版本>-windows-x64.zip --version <版本>
```

若独立源码副本路径较深，给安装器构建命令增加 `--work-dir <可写的短路径>`；Inno 编译器读取深层许可文件可能遇到路径长度上限。1.0.1 发行包另保留 `licenses/<组件>/METADATA`，对应源码及替换方式见[源码说明](releases/third-party-sources.md)。

构建会执行材料门槛；不要绕过检查或覆盖已发布产物。`BUILD-SOURCE.json`记录源码输入，`manifest.json`记录包内字节。许可和源码提供的待核实项见[材料复核](releases/v1.0.0-material-review.md)，这些材料在后续再分发前须处理。

## 验证与导出

冻结 OCR、GUI/CLI、隐私清单和自有进程退出由 `verify/portable_smoke.py` 检查；真实安装、升级及失败恢复需独立环境验证，不在日常使用目录直接试装。具体范围见[安装说明](guides/安装与首次使用.md)。

`scripts/prepare_public_source.py` 在公开基线之上合入当前本地源码与指定模板；这是源码导出，会带入业务变更。只修订文档时应从核对后的公开提交编辑，并审查逐文件差异。不得将私人开发仓库整体推送。

连接测试和开启联网翻译可能发送请求并计费；保存设置本身不联网。反馈只提交脱敏的必要材料，勿上传 API Key、完整 `data` 或私人聊天。
