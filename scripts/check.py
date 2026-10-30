"""Single local validation entry point; full subprocess logs stay in .temp/check/."""

import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys
import json
import re

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PUBLIC_TESTS = (
    'test_maintenance', 'test_realtime_scheduling',
    'test_updates', 'test_rollback', 'test_source_launcher', 'test_installer_build', 'test_portable_build',
    'test_public_export', 'test_check_runner', 'test_check_scope', 'test_tray_lifecycle',
    'test_close_preferences', 'test_game_autostart', 'test_fullscreen_settings', 'test_overlay_layout_work', 'test_workspace_ui', 'test_onboarding',
    'test_onboarding_ui', 'test_usage_dashboard', 'test_panel_instance', 'test_receive_send',
    'test_latency_acceptance', 'test_release_gate', 'test_terms_optional',
    'test_game_resources', 'test_msys2_ocr',
    'test_region_calibration', 'test_region_editor',
)


def check_syntax():
    paths = list(ROOT.glob("*.py"))
    for folder in ("app", "gui", "tests", "scripts", "verify", "tools"):
        paths.extend((ROOT / folder).rglob("*.py"))
    for path in paths:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    print(f"PASS syntax: {len(paths)} Python files", flush=True)


def run_suite(name, args, timeout=360):
    log_dir = ROOT / ".temp" / "check"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    env = dict(os.environ, PYTHONUTF8="1", DEEPSEEK_API_KEY="")
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run([sys.executable, "-X", "utf8", *args], cwd=ROOT,
                                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
            print(f"FAIL {name}: timed out after {timeout}s; details: {log_path}", flush=True)
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        if line.startswith(("Ran ", "OK", "FAILED", "通过 ")):
            print(line, flush=True)
    # A Tk background callback failure otherwise escapes unittest's result count.
    callback_error = any(any(marker in line for marker in (
        'invalid command name', 'Exception in Tkinter callback',
        'Tcl_AsyncDelete:', 'main thread is not in main loop')) for line in lines)
    success = code == 0 and not callback_error
    unit_count = re.search(r'Ran (\d+) tests?', '\n'.join(lines))
    regression_count = re.search(r'通过 (\d+) / (\d+)\s+跳过 (\d+)', '\n'.join(lines))
    skipped = re.search(r'^(?:OK|FAILED) \([^\n]*skipped=(\d+)', '\n'.join(lines), re.MULTILINE)
    summary = dict(name=name, code=code, successful=success, callback_error=callback_error,
        timed_out=timed_out, timeout_seconds=timeout,
        tests=int(unit_count[1]) if unit_count else (int(regression_count[2]) if regression_count else None),
        passed=int(regression_count[1]) if regression_count else None,
        skipped=int(regression_count[3]) if regression_count else (int(skipped[1]) if skipped else 0),
        skipped_items=[line for line in lines if 'SKIP' in line or 'skipped ' in line], log=str(log_path))
    result_path = log_dir / 'suites.json'
    summaries = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
    summaries[name] = summary
    result_path.write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"{'PASS' if success else 'FAIL'} {name}: {log_path}", flush=True)
    if not success:
        print("\n".join(lines[-65:]), flush=True)
    return success


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-only", action="store_true", help="Skip legacy OCR/Tk regression suite")
    parser.add_argument('--windows-public', action='store_true',
                        help='Windows/Tk offline subset requiring no private screenshots or system OCR')
    parser.add_argument('--suite-timeout', type=int, default=360,
                        help='Maximum seconds per suite (default: 360)')
    parser.add_argument('--isolated',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--keep',action='store_true',help='Keep the isolated source checkout for follow-up checks')
    parser.add_argument('--keep-failures',type=int,default=3,help='Keep this many managed failed checkouts (default: 3)')
    args = parser.parse_args()
    if args.keep_failures < 0: parser.error('--keep-failures must be nonnegative')
    if args.suite_timeout <= 0: parser.error('--suite-timeout must be positive')
    if args.windows_public and os.name != 'nt': parser.error('--windows-public requires Windows')
    if not args.isolated:
        from check_scope import create_scope, finish_scope
        output,source,guard,env=create_scope(ROOT)
        print(f'[ISOLATED] 当前源码测试根：{source}；真实数据/Key 不复制，真实网络已拒绝',flush=True)
        command=[sys.executable,'-X','utf8',str(source/'scripts/check.py'),'--isolated',
                 '--suite-timeout',str(args.suite_timeout)]
        if args.unit_only: command.append('--unit-only')
        if args.windows_public: command.append('--windows-public')
        result=subprocess.run(command,cwd=source,env=env)
        summary=dict(code=result.returncode,source=str(source),guard=str(guard),
                     output=str(output),scope='source-only with inherited network denial',
                     real_game='unverified',real_api='not_run')
        suites=source/'.temp/check/suites.json'
        if suites.exists(): summary['suites']=json.loads(suites.read_text(encoding='utf-8'))
        report=finish_scope(output,summary,args.report,args.keep,args.keep_failures)
        print(f'[REPORT] {report}',flush=True)
        return result.returncode
    if os.environ.get('DEADLOCK_CHECK_ISOLATED') != '1' or os.environ.get('DEADLOCK_TEST_NO_NETWORK') != '1':
        parser.error('Internal check requires source-only scope and inherited network-denial fixture')
    check_syntax()
    if args.windows_public:
        return 0 if run_suite('windows-public', ['-m', 'unittest', '-v',
            *('tests.' + name for name in WINDOWS_PUBLIC_TESTS)], args.suite_timeout) else 1
    good = run_suite("unit", ["-m", "unittest", "discover", "-s", "tests", "-v"], args.suite_timeout)
    if not args.unit_only:
        good = run_suite("regression", ["verify/test_pipeline.py", "-v"], args.suite_timeout) and good
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
