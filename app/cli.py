"""Command-line parsing and auxiliary commands, dispatched before worker startup."""
from app import brand
from app.storage import store
from app.translation.builtins import DEFAULT_GLOSSARY
from app.translation.learning import LearnedGlossary
from app.translation.query import QueryKey
import app.recognition.static as static_positions
import app.runtime.input_scope as input_scope
import app.runtime.reporting as reporting
import app.translation.desktop_engine as desktop_translation
import argparse
import json
import os
import time

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=f"{brand.PRODUCT}（{brand.SUBTITLE}：队友发言 → 中文气泡）")
    parser.add_argument("--ocr-debug", action="store_true",
                        help="把每次识别的全部原始变体都记进 run.log（采集真实数据用，日志会长）")
    parser.add_argument("--simulate", help="用假聊天文件当输入源")
    parser.add_argument("--sandbox-dir", default="", help="模拟/回放输出根目录，必须是新的或空目录")
    parser.add_argument("--offline", action="store_true", help="不调 API，只测抓取和浮窗")
    parser.add_argument("--headless", action="store_true", help="不弹浮窗，只打终端")
    parser.add_argument("--once", action="store_true", help="配合 --simulate：跑完一遍就退出")
    parser.add_argument("--replay", metavar="VIDEO",
                        help="把录像文件当作抓屏来源，走**同一条识别链**（隐含 --headless --once）。"
                             "北极星验收线用它：真实录像 → 生产入口 → 产出中文")
    parser.add_argument("--replay-start", type=float, default=0.0,
                        help="配合 --replay：从录像第几秒开始")
    parser.add_argument("--replay-seconds", type=float, default=0.0,
                        help="配合 --replay：只回放多少秒（0 = 全片）")
    parser.add_argument("--replay-fps", type=float, default=0.0,
                        help="配合 --replay：每秒取几帧（0 = 按 config 的 ocr_interval 推导）。"
                             "**必须与实机采样率一致**，否则会系统性低估假阳性："
                             "实机 0.25s 一帧（4Hz），瞬态 HUD 元素更容易被连续读到 3 票，"
                             "而 1Hz 回放会漏掉这些瞬态。")
    parser.add_argument("--replay-visual", action="store_true",
                        help="配合 --replay：打开视觉寿命门（生产默认开启）。"
                             "关掉时回放看不到那道门的丢弃；打开才能量出它的丢弃率。"
                             "注意：回放里时间是压缩的，视觉租期语义只能近似复现")
    parser.add_argument('--replay-realtime', action='store_true',
                        help='按原速推进录像，启用实机视觉寿命/活动调度；默认解码 10Hz（时间量化 0.1 秒）')
    parser.add_argument("--stats", action="store_true",
                        help="打印用量台账（真实 token / 成本 / 本地命中率）后退出")
    parser.add_argument("--dashboard", action="store_true",
                        help="打开数据中心窗口（独立进程，只读数据文件）")
    parser.add_argument("--manage", action="store_true",
                        help="打开数据管理窗口：词库 / 翻译记忆 / 待审核")
    parser.add_argument("--settings", action="store_true",
                        help="打开设置中心（独立进程；改动会立刻生效）")
    parser.add_argument("--page", default="",
                        help="配合 --manage：先显示哪一页（glossary/memory/data）")
    parser.add_argument("--check", action="store_true",
                        help="跑一遍系统体检（依赖/Tesseract/游戏/Key/词库/记忆/目录）后退出")
    parser.add_argument("--panel", action="store_true",
                        help="打开主控制窗口（小窗口：运行状态 + 三个入口）")
    parser.add_argument("--panel-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="仅供附属窗口使用：父进程 PID。父进程一退，本窗口跟着退 —— "
                             "避免父进程退出后留下关不掉的孤儿窗口")
    parser.add_argument("--no-panel", action="store_true",
                        help="启动时**不要**弹出主控制窗口（只留托盘图标）")
    parser.add_argument("--backup", action="store_true",
                        help="立刻备份一次数据（词库/记忆/设置/统计/留档）后退出")
    parser.add_argument('--review-pending',action='store_true',help='列出待审核来源、质量证据及并发校验 token')
    parser.add_argument('--review-decision',choices=('accept','edit','reject'))
    parser.add_argument('--review-key',default='')
    parser.add_argument('--review-token',default='')
    parser.add_argument('--review-formal-token',default='')
    parser.add_argument('--review-translation',default='')
    parser.add_argument('--learning-recovery',action='store_true',help='只列出后台学习恢复审计记录，不自动导入')
    parser.add_argument('--recover-learning',metavar='REQUEST_ID',help='显式重检并保存该学习日志结果到学习缓存；零 HTTP')
    parser.add_argument("--pending-requests", action="store_true",
                        help="列出卡在「结果未知」的未决请求（它们不会自动重发）")
    parser.add_argument("--release", metavar="REQUEST_ID",
                        help="配合 --pending-requests：已核对**未计费**，允许该句再发一次")
    parser.add_argument("--abandon", metavar="REQUEST_ID",
                        help="配合 --pending-requests：可能已计费，永久放弃该句")
    parser.add_argument("--static-ui", action="store_true",
                        help="列出静态位置「确认名单」与程序自动学到的候选（候选不参与抑制）")
    parser.add_argument("--promote-static", metavar="列,行",
                        help="把某个自动候选提升为确认（此后该位置不再当聊天识别）")
    parser.add_argument("--forget-static", metavar="列,行",
                        help="从确认名单移除某个位置（解除该位置的聊天抑制）")
    # [!] `--safe-mode`（§四十二）属于阶段 8，这里先不占位 ——
    #     放一个"接受参数但什么都不做"的开关比没有更糟：用户会以为它在保护数据。
    args = parser.parse_args(argv)
    if args.replay:
        # Sequential diagnostics retain reliable delivery. Real-time replay uses
        # production deadlines/queues so slow work cannot stretch the movie.
        args.headless = True
        args.once = True

    return args


def dispatch(args, config):
    if args.stats:
        # 只打报表，什么都不动（不建窗口、不抓屏、不碰任何数据文件）
        return reporting.print_usage_report(config)
    if args.review_pending or args.review_decision:
        from app.storage.review_queue import ReviewQueue
        review = ReviewQueue(input_scope.PENDING_PATH,input_scope.GLOSSARY_PATH)
        if args.review_decision:
            result = review.decide(args.review_key,args.review_decision,token=args.review_token,
                formal_token=args.review_formal_token,translation=args.review_translation)
            print(json.dumps(dict(saved=result.saved,reason=result.reason),ensure_ascii=False))
            return 0 if result.saved else 1
        try:
            print(json.dumps(review.items(),ensure_ascii=False,indent=2))
            return 0
        except ValueError:
            print('[warn] 审核记录不可读取，未修改文件')
            return 1
    if args.learning_recovery or args.recover_learning:
        from app.storage.requests import JournalError
        manager = desktop_translation.Translator('',config['endpoint'],config['model'],True,hard_offline=True,
            learning=LearnedGlossary(input_scope.GLOSSARY_PATH,DEFAULT_GLOSSARY),allow_popup=False,
            builtins_enabled=config.get('builtins_enabled',True))
        if args.recover_learning:
            receipt = manager.recover_learning(args.recover_learning)
            print(json.dumps(dict(saved=receipt.saved,reason=receipt.reason,destination=receipt.destination),ensure_ascii=False))
            return 0 if receipt.saved else 1
        try:
            records = manager.journal.learning_recovery_records()
            print(json.dumps([dict(request_id=r['request_id'],original=r['original'],state=r['state'],
                                  purpose=r['purpose'],quality_checks=r.get('quality_checks') or '历史证据缺失') for r in records],ensure_ascii=False,indent=2))
            return 0
        except JournalError:
            print('[warn] 学习请求日志读取失败，未修改文件')
            return 1
    if args.check:
        # 系统体检（§二）：**只读**，不改任何东西。
        import app.platform.diagnostics as diagnostics
        return diagnostics.cli_report(config, input_scope.APP_DIR)
    if args.backup:
        # 立即备份（§二十二）。写到数据目录下的 backups\。
        import app.platform.diagnostics as diagnostics
        note = (f"{brand.PRODUCT} 备份\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"版本：{diagnostics.VERSION}\n")
        path = store.backup(input_scope.APP_DIR, note=note)
        if path:
            print(f"[ok] 备份完成：{path}")
            return 0
        print("[warn] 备份失败：没有可备份的文件，或者目录写不进去（见 run.log）")
        return 1
    if args.pending_requests or args.release or args.abandon:
        # 未决请求的人工恢复入口（§7.3）。默认只列，不改任何东西。
        action = "release" if args.release else "abandon" if args.abandon else "list"
        return pending_requests_cli(action, args.release or args.abandon or "")
    if args.static_ui or args.promote_static or args.forget_static:
        # 静态位置名单的人工审查入口。默认只列，不改任何东西。
        action = ("promote" if args.promote_static
                  else "forget" if args.forget_static else "list")
        return static_positions.static_ui_cli(action, args.promote_static or args.forget_static or "")
    if args.panel or args.manage or args.settings or args.dashboard:
        # [!] 2026-10-11：附属窗口**必须跟着父进程一起死**。
        #     博士报障「点快捷方式后台出两个窗口，有一个还关不掉」——查出来是
        #     上一个实例退出后，它的 `--panel` 子进程变成孤儿还挂在屏幕上：没有托盘、
        #     没有翻译器，点 X 弹的对话框还说「要让翻译器彻底退出：托盘右键」（对孤儿毫无意义）。
        #     成因：`Overlay.open_window` 用 Popen 起独立进程，父子之间**没有生命期绑定**。
        # ============ 全部窗口入口统一到**一个** App ============
        # [!] 这里是本次 UI 重构的核心收敛点。
        #     以前有**三套**并行的窗口体系：--dashboard / --manage / --panel，
        #     再加一个左侧 5 页导航的旧容器，于是「点新入口打开旧页面」。
        #     主面板由 gui/app.py 与各页面模块构建。
        #
        # [!] 旧命令行参数**保留成别名**，不删 —— 启动器.bat / 热键 / 托盘
        #     都还在用它们，删了就断链。它们现在只是"打开某一页"的快捷方式：
        #         --panel              → 首页
        #         --panel --page X     → 指定页
        #         --settings           → 设置页
        #         --manage             → 词库页
        #         --dashboard          → 消耗页（原「数据中心」已并入消耗）
        try:
            from gui import app as app_mod
        except Exception as exc:
            print(f"[error] 窗口打不开：{exc}")
            return 1
        page = args.page.strip()
        if not page:
            if args.settings:
                page = "settings"
            elif args.manage:
                page = "dictionary"
            elif args.dashboard:
                page = "cost"
            else:
                page = "home"
        return app_mod.run(input_scope.APP_DIR, initial=page, parent_pid=args.parent_pid)

    return None


def pending_requests_cli(action="list", request_id="", note="", journal=None):
    """未决请求（结果未知）的**人工恢复入口**。

    [!] 为什么必须有这个入口：`outcome_unknown` 原来是死路 —— 不能重发、不能终结、
        `gate()` 永久封锁，想恢复只能手工删掉 journal 文件。那不是保守，是死路。
        这里把它变成一条**有记录、可审计**的路：

          列出   ：哪些句子卡住了、试过几次、费用状态是什么
          release：已核对未计费 → 允许再发一次（仍受重试次数上限约束）
          abandon：可能已计费 → 永久放弃该句，但状态与原因可查

        它不改变「绝不自动重发」：必须由人明确选一个结论。
    `journal` 可注入，便于用临时目录做端到端测试，不去碰真实数据。
    """
    from app.storage.requests import JournalError, RequestJournal

    if journal is None:
        journal = RequestJournal(os.path.join(os.path.dirname(input_scope.GLOSSARY_PATH), "requests"))
    records = journal.stuck()
    if action == "list":
        if not records:
            print("[ok] 没有未决请求：没有句子卡在「结果未知」")
            return 0
        print(f"[warn] 有 {len(records)} 条未决请求（不会自动重发）：")
        for record in records:
            attempt = record["attempts"][-1]
            print(f"  {record['request_id']}  {record['original']!r}  "
                  f"已试 {len(record['attempts'])} 次  费用状态={attempt.get('cost_status')}"
                  f"  {record.get('reason', '')}")
        print()
        print("  已核对未计费 → --release <request_id>")
        print("  可能已计费   → --abandon <request_id>")
        return 0
    matches = [row for row in records if row["request_id"].startswith(request_id)]
    if len(matches) != 1:
        print(f"[error] 需要唯一的 request_id 前缀，匹配到 {len(matches)} 条")
        return 2
    record = matches[0]
    if QueryKey.from_dict(record['query_key']) != journal.query_key(record['original']):
        scope = {key:record['query_key'][key] for key in ('source_language','target_language','domain')}
        journal = RequestJournal(journal.directory, now=journal._now, query_scope=scope)
    try:
        journal.resolve(record["original"], record["request_id"],
                        released=(action == "release"), note=note or "CLI 人工核对")
    except JournalError as exc:
        print(f"[error] 处理失败：{exc.reason}")
        return 1
    print(f"[ok] 已{'释放（允许再发一次）' if action == 'release' else '放弃'}："
          f"{record['original']!r}（{record['request_id']}）")
    return 0
