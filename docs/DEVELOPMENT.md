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

在 64 位 Windows 上安装运行依赖与 PyInstaller，下载 `scripts/build_portable.py` 中指定的 Tesseract OCR 安装器，并核对固定 SHA-256：

```powershell
pip install pyinstaller
python scripts/build_portable.py --ocr-installer "C:\Downloads\tesseract-ocr-w64-setup-5.5.3.20260724.exe" --installer-sha256 bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4
```

将示例路径替换为实际下载位置。工具从允许的源码与资源构建，排除用户数据；结果写入 `dist/`，已有同名 ZIP 时拒绝覆盖。源码 VERSION 为 1.0.0，默认测试包版本为 1.0.0-test.1。实际发布 ZIP 的检查入口为 `verify/portable_smoke.py`，参数见 `--help`。

## 反馈与数据

请通过 Issues 报告可复现的问题。设置页可导出反馈 ZIP，提交前自行检查；不要提交 API Key、完整 `data/`、私人聊天或未经脱敏的截图。必要的模型与第三方许可随源码保留，见 [第三方声明](THIRD-PARTY-NOTICES.md)。


## 识别、显示与计量

- OCR 需三个不同采样帧确认，并执行正文几何、视觉连续性和独立源图复核；缓存不增加确认票。源图汉字检查不依赖用户词表。
- 首选正文与独立源图读数一致时停止更多变体；短缩写保留英文后备策略。存在 `libtesseract-5.dll` 时使用可终止的常驻辅助进程，否则使用有超时保护的命令行路径，诊断中记录后备状态。
- `ocr_interval` 是最小处理周期，整轮 OCR 耗时不再叠加额外等待；普通启动读取该配置，确认票数至少为 3。
- 新字幕以首次候选帧为起点，API 等待及首次绘制均检查 4 秒截止时间；按时显示的字幕随原气泡寿命停留。候选帧可能晚于原文字出现，因此该截止时间不代表从原消息出现起的端到端上限。
- 手动翻译接口等待为 4.5 秒，不自动重试；预览不受屏幕气泡寿命限制。网络和服务商响应无法由本机保证，该配置不表示所有翻译能在 5 秒内成功。
- 手动翻译与屏幕翻译有独立请求身份，手动结果不写正式词库或屏幕待审核队列，费用记录沿用请求台账。后台旧结果或离开页面后的结果不进入新预览。
- 待审核要求消息有 Tk 绘制回执且通过质量检查。该回执不能证明游戏合成器最终呈现了像素；跨局边界通过用户点击“开始新对局”确定，切换前的迟到消息不进入新局。

`data/runtime.json` 的 `latency_drawn_ms` 保存本地/API 最近 512 个绘制样本。这些是成功显示样本，不能单独证明覆盖率。候选图、生命周期与分段耗时保留在本机私有 `backups/development/recognition-recent/`。

`tools/benchmark_receive.py --output 私有目录/ocr.json` 比较英文 CLI、五语言 CLI 与常驻路径，使用裁图和合成语言样本，不调用翻译 API；不替代真实对局测试。

整局验收使用 `verify/latency_acceptance.py labels.json --output report.json`。输入示例：

```json
{"messages":[{"id":"m1","eligible":true,"appeared_at":1.0,"drawn_at":2.0,"route":"api"}]}
```

时间统一使用原录像秒数，未显示记 `drawn_at:null`；`eligible` 依据原画面。原文无中文却被 OCR 误判跳过的消息仍计为符合条件且漏译。内部目标为 90% 在 2 秒内显示，目前未完成验收；计算时未译消息保留在分母中，同时报告超过 4 秒的显示。

## 请求结果不明时的处理

先向接口服务商核对请求状态，勿删除请求文件解除保护。以下命令仅处理记录，不自动发起翻译：

```powershell
python -m app --pending-requests
python -m app --release 请求编号
python -m app --abandon 请求编号
```

第一条查看记录；确认未计费后，第二条允许再次尝试；第三条放弃该请求。超时后的防重发记录不会因重启自动清除。

待审核诊断可使用 `python -m app --review-pending`。未显示、质量检查失败和缺少显示证据的旧记录保留供检查，不会推定为已显示。


## 1.0.0-test.2 验证

本版沿用实际测试的源码，未包含正常首字母大写短句的候选修复。源码 95 项相关回归、19 项真实 OCR 与辅助进程检查通过；实际 Tk/UI 检查 31 项中 30 项通过，关闭回调异常时未销毁窗口的用例仍失败。旧回归为 154/157，通过之外有 1 项跳过和 2 项失败（API 延迟记账断言、无窗口提示断言）。4 次真实 API 请求成功；静态真实截图至实际 Tk 字幕绘制和失位恢复检查通过，这不等同于整局游戏画面覆盖率验收。

公开源码不附私人截图、聊天记录或人工真值资料；依赖这些材料的完整开发测试不能直接在公开克隆中复现。包构建命令需显式传入 `--version 1.0.0-test.2`。测试结果及已知限制见[本版说明](releases/v1.0.0-test.2.md)。

## 未发布源码：导航与发言流程

当前 main 使用六页导航。接口草稿与发言原文/预览在本次窗口生命周期内保留；地址、模型和密钥统一点击保存。过期译文不可通过复制按钮使用，迟到的保存与连接结果也需核对当前配置。

开发目录的后台相关检查为 170 项：159 通过、10 因可见窗口/原生钩子边界跳过、1 条关闭失败回调用例未通过。该用例期望退出保存回调异常后仍销毁窗口，而当前实现保留窗口；该实现没有在本轮修改。语法检查通过。测试使用临时数据、模拟 API/输入和隐藏 Tk 窗口，没有真实网络请求、剪贴板或游戏输入操作。

原生默认/小窗口、DPI、焦点顺序与实际游戏回填、自动发送仍未验收。以上记录不能替代真实游戏结果，也不是标准全量检查通过声明。下载附件仍是 1.0.0-test.2，此次只同步源码，没有新标签或安装包。
