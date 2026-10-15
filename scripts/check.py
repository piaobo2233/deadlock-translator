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


def check_syntax():
    paths = list(ROOT.glob("*.py"))
    for folder in ("app", "gui", "tests", "scripts", "verify", "tools"):
        paths.extend((ROOT / folder).rglob("*.py"))
    for path in paths:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    print(f"PASS syntax: {len(paths)} Python files", flush=True)


def run_suite(name, args):
    log_dir = ROOT / ".temp" / "check"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    env = dict(os.environ, PYTHONUTF8="1", DEEPSEEK_API_KEY="")
    with log_path.open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run([sys.executable, "-X", "utf8", *args], cwd=ROOT,
                                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
            code = result.returncode
        except subprocess.TimeoutExpired:
            print(f"FAIL {name}: timed out; details: {log_path}", flush=True)
            return False
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        if line.startswith(("Ran ", "OK", "FAILED", "通过 ")):
            print(line, flush=True)
    # A Tk background callback failure otherwise escapes unittest's result count.
    callback_error = any("invalid command name" in line or "Exception in Tkinter callback" in line
                         for line in lines)
    success = code == 0 and not callback_error
    unit_count = re.search(r'Ran (\d+) tests?', '\n'.join(lines))
    regression_count = re.search(r'通过 (\d+) / (\d+)\s+跳过 (\d+)', '\n'.join(lines))
    skipped = re.search(r'OK \(skipped=(\d+)\)', '\n'.join(lines))
    summary = dict(name=name, code=code, successful=success, callback_error=callback_error,
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
    parser.add_argument('--isolated',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--keep',action='store_true',help='Keep the isolated source checkout for follow-up checks')
    parser.add_argument('--keep-failures',type=int,default=3,help='Keep this many managed failed checkouts (default: 3)')
    args = parser.parse_args()
    if args.keep_failures < 0: parser.error('--keep-failures must be nonnegative')
    if not args.isolated:
        from check_scope import create_scope, finish_scope
        output,source,guard,env=create_scope(ROOT)
        print(f'[ISOLATED] 当前源码测试根：{source}；真实数据/Key 不复制，真实网络已拒绝',flush=True)
        command=[sys.executable,'-X','utf8',str(source/'scripts/check.py'),'--isolated']
        if args.unit_only: command.append('--unit-only')
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
    good = run_suite("unit", ["-m", "unittest", "discover", "-s", "tests", "-v"])
    if not args.unit_only:
        good = run_suite("regression", ["verify/test_pipeline.py", "-v"]) and good
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
