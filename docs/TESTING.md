# 测试与验证范围

## 1.0.0

维护者已确认个人使用体验满足发布预期。源码、公开导出、冻结包与更新兼容检查分别记录；它们不代表所有系统或游戏场景都已通过。

- 源码隔离检查覆盖配置、存储、识别、翻译、界面、更新及生命周期；拒绝真实网络，不复制真实配置或 Key。
- 新个人区域测试覆盖原像素坐标、同尺寸匹配、损坏记录回退、真实工厂裁图/偏移契约、保存/取消、旧回执拒绝及迟到结果丢弃。
- Windows 公开 CI 包含区域编辑器、Tk、安装输入、更新和存储离线子集。完整成绩以该版本公开提交的 [Actions](https://github.com/piaobo2233/deadlock-translator/actions/workflows/public-checks.yml) 为准。
- 安装包另做冻结 OCR、GUI/CLI、隐私清单和自有进程退出检查；旧 beta.1 更新器单独验证 1.0.0 Release 的发现及安装包匹配。

## 已有实测与未完成项目

设置与词库响应已有维护者体验签收；游戏复制译英有本机受控成功和使用反馈。其它组件的原生、合成与回放证据各有范围，不混作自然对局成绩。

完整 Windows/DPI/显示配置矩阵、首次用户区域任务、自然聊天覆盖及译意计分、四小时耐久、干净系统首装/真实升级/失败恢复仍待补齐。没有固定延迟、准确率或性能保证；未签名程序的 Windows 下载提示也不能由 CI 签收。

适用范围、当前功能及升级注意事项见[发布说明](releases/v1.0.0.md)。历史 beta.1 说明保留在[历史发布记录](releases/v1.0.0-beta.1.md)。

## 复现

- `python -X utf8 scripts/check.py`：开发源码完整隔离检查；私有真值不在公开树中。
- `python -X utf8 scripts/check.py --windows-public`：公开 Windows 离线子集。
- `python scripts/check_public.py --syntax --links`：公开源码语法及本地文档目标。

构建依赖、入口与材料见[开发说明](DEVELOPMENT.md)。
