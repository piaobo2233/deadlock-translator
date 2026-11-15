# 2026-11-14 候选许可迁移审计

状态：材料清点完成初稿，GPL 切换尚未批准通过。当前根 LICENSE 仍为 MIT。

本次功能候选复用现有组件，没有新增第三方依赖、游戏素材或在线服务。审计针对当前开发树；实际冻结包的版本、组合关系和完整对应源码仍须针对最终候选核对。

| 材料 | 固定来源与现有证据 | 许可与组合检查 | 状态及处置 |
| --- | --- | --- | --- |
| 项目原创源码及文档 | 根 LICENSE；本地提交历史；本次改动前源码摘要 | 现有 MIT 版权和许可保留；不能仅凭单一提交者认定所有代码版权归属 | 已清点，仍需权利人确认原创和外部贡献的再许可权限 |
| 引用的 ValveResourceFormat 解码实现 | `app/game_data/valve.py` 头部固定提交；`docs/licenses/ValveResourceFormat/LICENSE` | MIT 原文和署名继续保留；实现许可不授予游戏内容权利 | 已有来源，候选继续保留原许可 |
| OCR 二进制及传递运行库 | OCR manifest、MSYS2 包锁定、源码映射和逐文件审计 | Apache、BSD、GPL/LGPL 等及例外逐组件核对，不能把整个目录概括为 Apache | 现有审计可复用；最终包组合及完整源码提供仍待复建核对 |
| 九份 tessdata_fast 模型 | `app/recognition/models/provenance.json` 固定提交和 SHA-256；同目录 LICENSE | Apache-2.0，保留来源及许可 | 核对本次输入摘要；真实包内收集另验 |
| Python/Tcl/Tk 与 Python 依赖 | requirements 锁定、构建脚本收集的 METADATA 与许可证 | 版本清单与实际冻结输入比对；pystray LGPL、PyInstaller 引导程序例外分别记录 | 本机元数据留证，不能据此宣称最终包义务全部完成 |
| Bootstrap Icons 字体 | 随 ttkbootstrap 分发，现有第三方说明记载 MIT | 当前说明仍缺精确字体版本及来源映射 | 保留原文；正式发行前补齐或移除未确认字体输入 |
| 品牌图标 | `docs/assets/locklingo/design-brief.md` 及导出索引 | 生成时参考 Steam 官方图标；没有单独的第三方权利闭合证明 | 不宣称独占；正式发行前明确处理参考素材边界或替换 |
| 背景及截图 | `app/assets/ui/README.md`、项目素材说明 | 生成背景与真实 UI 截图分别记录；真值和私人截图不导出 | 按现有公开导出排除规则核对，不复制私人数据 |
| 本机游戏派生资料 | 游戏资料审计及本地生成路径 | 新发行不携带历史 `game_terms.json` / `quick_chat.json` | 复用现有排除和本机生成规则，实际候选 gate 重验 |
| 安装器与构建材料 | Inno Setup 语言文件、PyInstaller 配置、构建依赖和脚本 | 工具本身、分发引导程序、语言文件分别核对 | 沿用现有来源；最终包/源码材料分别留证 |

## 切换与发布条件

1. 固定干净候选的文件清单、SHA-256、实际依赖版本和来源；不将当前大量既有未提交修改自动归为本次成果。
2. 闭合原创及外部贡献的再许可权限；按本次确认的 GPL-3.0-only 方向固定生效版本，后续锁语软件版本继续沿用 GPL 第 3 版。
3. 处理上表未决的字体和品牌素材输入，核对各组件组合范围及源码/NOTICE 提供方式。
4. 从提供的对应源码与构建材料复建候选，核对便携包、安装包、许可文件和公开模板一致。
5. 用户验收后再执行发布；不修改旧标签或旧附件，不追溯收回历史 MIT 授权。

本记录没有将材料身份核验等同于最终法律结论。未解决项仍阻止相关发行许可切换，不阻止用隔离数据验收本次软件行为。

## 依据

- [现有第三方清单](../THIRD-PARTY-NOTICES.md)
- [项目与素材来源](../PROJECT-NOTICE.md)
- [品牌识别说明](../BRAND.md)
- [GPLv3 原文镜像](https://opensource.org/license/gpl-3.0)
- [MIT 原文](https://opensource.org/license/mit)
