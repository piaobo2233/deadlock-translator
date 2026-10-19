# 候选版分发材料审计（2026-10-19）

## 1.0.0-beta.1 当前选择

历史官方安装器路线已替换。当前实际运行库是 MSYS2 固定包的 40 个 PE 文件及一个固定模型；34 个二进制包对应 32 个完整源码包，所有 PKGBUILD 原始字节摘要均匹配二进制 BUILDINFO。实际许可、构建元数据和逐文件映射已归档；对应源码、配方和补丁作为同版本 Release 附件免费提供。当前依据为 [OCR 审计](ocr-runtime-audit.json)、[锁文件](ocr-msys2-packages.lock.json)、[源码目录](ocr-msys2-sources.json)和[许可说明](../THIRD-PARTY-NOTICES.md)。下文 rc.4 的 31 个 Open DLL 是旧字节历史，不作为本次替代字节的结论。

两份游戏派生数据不随新公开源码和新包分发，改为本机生成；旧开发文件和公开历史保留。验证范围见[游戏资料路线](game-data-validation.md)。Q13 允许 Beta 首发后补完整游戏和跨环境验收；材料身份、许可附带及隐私边界仍需通过。

本次核对了本地 rc.4 候选使用的 OCR 安装器摘要、35 文件运行时清单、可读取的 PE 版本资源与运行时版本输出。逐文件大小、SHA-256、版本证据和材料状态见 [机器可读清单](ocr-runtime-audit.json)，原固定输入见 [构建清单](../../scripts/ocr-runtime-manifest.json)。35 文件摘要全部匹配；摘要一致只能确认输入身份。

安装器来自 [Tesseract 官方 5.5.3 Release](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3)。原安装器解出目录中收录的是 Tesseract 的 LICENSE、AUTHORS、README，没有找到覆盖所有传递 DLL 的逐组件许可集合。

## 已有材料与缺口

| 范围 | 文件数 | 当前证据 | 正式分发前仍需处理 |
| --- | ---: | --- | --- |
| Tesseract EXE / DLL | 2 | 固定官方安装器、Apache-2.0、AUTHORS | 连同其依赖一起完成材料核对 |
| Leptonica DLL | 1 | 运行时自报 1.87.0，已有许可文本 | 二进制与对应源码构建的关联 |
| 英文模型 | 1 | tessdata_fast 固定提交、逐文件摘要、Apache-2.0 | 保留来源和许可 |
| 其他传递 DLL | 31 | 固定安装器、逐文件摘要；部分版本资源 | 对应源码/包构建、完整许可及再分发义务 |

31 个未闭合文件包括压缩、图像、网络及 GNU/MinGW 运行库；具体文件在 JSON 中逐一列出，不能将 Tesseract 的 Apache-2.0 套用于这些组件。PE 资源中的 `libintl 1.0`、`winpthread 1,0,0,0` 可能是通用资源值，不能作为精确包版本。版本输出同时出现 zlib 1.3.1 与 1.3.2，需按实际动态文件和构建记录继续核实，不能任选一个填入许可结论。

继续收口需要从该安装器的可追溯构建环境取得依赖包版本、源码归档和许可集合；若无法取得，则应评估自行构建一个依赖来源明确的 OCR 运行时。不能仅下载各项目当前 LICENSE，便宣称已匹配这些 DLL。

## 游戏派生术语

`game_terms.json` 的来源、摘要和整理方式已记录，单独再分发依据仍未确认。[Steam 订户协议](https://store.steampowered.com/subscriber_agreement/) 第 2 节区分个人使用、工具、同人作品及复制/分发限制；目前没有据此核实本术语文件适用的授权。该事实不因项目开源而改变，也不在本审计中作具体法律结论。保留现有内容，后续需确认依据或由项目负责人选择替代的分发方式，详见 [项目说明](../PROJECT-NOTICE.md)。

## 当前发行结论

本次完成材料盘点，正式二进制的材料门槛仍未通过。未公开新安装包，也未将来源说明改写为授权证明。干净 Windows 安装、真实升级/卸载和游戏验收另见 [测试状态](../TESTING.md)。

## 2026-10-19 执行阶段补充

重新从原安装器解出目录读取普通及延迟 PE 导入表：34 个 EXE/DLL 的实际闭包，加同一份英文模型，共 35 文件，与固定清单完全对应，实际运行时摘要全部匹配。私有依赖图及源文件摘要保存在本机 `backups/development/release-1.0/`，不含用户密钥或聊天。

官方 tag `5.5.3` 的 [安装器工作流](https://github.com/tesseract-ocr/tesseract/blob/5.5.3/.github/workflows/installer-for-windows.yml)、[构建脚本](https://github.com/tesseract-ocr/tesseract/blob/5.5.3/nsis/build.sh)和[依赖收集器](https://github.com/tesseract-ocr/tesseract/blob/5.5.3/nsis/find_deps.py)已归档摘要。脚本安装滚动 MSYS2 依赖，并从 Ubuntu MinGW 工具链替换 GCC/标准 C++ 运行库；不能把所有 DLL 都对应到当前 MSYS2 包。按安装器日期查询未取得对应公开运行记录，尚无该次构建的完整精确包清单，原 31 个 Open 项继续保持 Open。

[MSYS2 当前 Tesseract 包](https://packages.msys2.org/packages/mingw-w64-x86_64-tesseract-ocr)提供包摘要、源码包和依赖入口，可作为 A2 的候选来源；它的库名/构建与现有安装器不同，不能用其许可或版本记录为旧字节直接签收，尚未替换当前运行时。

两套游戏资料都尚未取得单独再分发依据，执行已确认的本机生成备选路线。当前安装 manifest build 为 `26020302`；已有快捷交流库为 `25950029`，其主文本和技能名称来源摘要均与当前安装不符。旧 VPK 引用清单不能冒充同安装快照的新输入。新增 [机器可读资料审计](game-data-audit.json)，状态为 `local_generation_pending`；旧资料、公开历史及用户数据保留，尚未删除 JSON 或宣称本机生成已验收。

`scripts/release_gate.py` 已接入稳定版 ZIP/setup 构建及新公开源码导出：稳定版本须与源码 VERSION 一致；OCR 清单、逐文件审计、许可/构建证据摘要必须匹配。游戏资料须分别有来源与分发依据，或本机生成路线完成指定验证。未知、缺记录、过期摘要和空证据会拒绝；测试候选显式标记 `local_test_only`。这只是自动阻断措施，不能代替人工材料审查或正式发布前签收。
