# 参与贡献

本项目由个人维护，欢迎提供可复现的问题、文档修正和范围明确的改动。开始前请阅读[使用说明](guides/首版测试与分享.md)与[测试状态](TESTING.md)。

## 提交反馈

使用 [问题与功能建议入口](https://github.com/piaobo2233/deadlock-translator/issues/new/choose)。报告问题时附版本、Windows/分辨率/缩放、游戏显示模式、运行方式、复现步骤、预期和实际结果。仅粘贴检查后的必要日志，遮挡昵称；不要上传 API Key、完整 data 或私人聊天。

真实密钥泄露与安全漏洞请按[安全说明](../.github/SECURITY.md)处理，勿公开敏感内容。

## 修改与验证

1. 从 `main` 建分支，一次解决一个问题；先读相关实现和调用方，保留既有修改。
2. 在有 Python 3.13 的公开克隆中执行：

   ```powershell
   python scripts/check_public.py --syntax
   python -m unittest discover -s tests -p test_portable_build.py -v
   python scripts/check_public.py --links
   ```

3. 行为修改再执行相应定向测试。Windows 界面、Tesseract OCR、钩子和真实游戏验证需单独记录；公开基础 CI 不能替代这些检查。私人真值材料不随仓库公开。
4. PR 说明问题、最终变化、验证结果与未验证范围。新增依赖或引用代码/素材时，补原作者、来源链接、版本和许可，更新[第三方清单](THIRD-PARTY-NOTICES.md)。

## 修改边界

- 业务实现放 `app/`，界面放 `gui/`，运行/检查入口放 `scripts/`，用户文档放 `docs/`。
- OCR 阈值、候选筛选、翻译链顺序、字幕生命周期及持久化格式的变化，请先开 Issue 讨论范围与复现证据。保持 `词库 → 记忆 → 缓存 → API` 的顺序。
- 用户数据路径和格式需保持稳定；持久化沿用 `app/storage/store.py`。不要清空请求台账或用户数据来解除保护。
- ttkbootstrap / pystray 应继续作为可选增强；避免将它们改成启动所必需。
- 不提交密钥、聊天日志、完整 data、私人截图、构建缓存或无关改动；新增工具应有实际复用价值。

项目源码许可见 [MIT](../LICENSE)，第三方及素材归属见[项目说明](PROJECT-NOTICE.md)。
