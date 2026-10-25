# -*- coding: utf-8 -*-
"""Build the per-user Windows installer from a verified portable ZIP.

Pipeline: scripts/build_portable.py -> dist/LockLingo-<ver>-windows-x64.zip
          -> scripts/build_installer.py -> dist/LockLingo-<ver>-setup.exe

The installer is compiled with Inno Setup 6.7+ (ISCC). It installs per user to
`%LOCALAPPDATA%\\Programs\\LockLingo`, keeps `<安装目录>\\data` on upgrade and on
uninstall (unless the user explicitly deletes it), and refuses to run while the
assistant is running. Tool location is discovered, not assumed: pass --iscc or
set INNO_SETUP_ISCC.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_gate
ISS = ROOT / "scripts" / "installer" / "locklingo.iss"
ICON = ROOT / "app" / "assets" / "brand" / "locklingo.ico"
LANGUAGE = ROOT / "scripts" / "installer" / "ChineseSimplified.isl"
EXE_NAME = "DeadlockTranslate.exe"
GUI_NAME = "LockLingo.exe"

APP_NAME = "锁语 · LockLingo"
PUBLISHER = "piaobo2233"
PUBLISHER_URL = "https://github.com/piaobo2233/deadlock-translator"
RUNNING_MESSAGE = (
    "锁语 · LockLingo 仍在运行。请先将设置中的「关闭窗口时」设为「退出助手」，"
    "再关闭主窗口；有托盘时也可右键退出。退出后点击「确定」继续，或「取消」退出安装。"
)
UNINSTALL_RUNNING_MESSAGE = RUNNING_MESSAGE
DIRECTORY_MESSAGE = ('请使用英文安装路径。每次安装均可修改目录；更换目录不会自动迁移个人数据。'
                     '请先备份旧目录中的整个 data 文件夹，退出助手后手动迁移。已有数据位置（如有）：')
KEEP_DATA_MESSAGE = (
    "是否保留个人数据（设置、API Key、词库、翻译记忆）？"
    "选择「是」保留（推荐）；选择「否」会删除这些数据，且无法恢复。"
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def find_iscc(explicit=""):
    candidates = [explicit, os.environ.get("INNO_SETUP_ISCC", ""),
                  str(ROOT / ".temp/tools/innosetup/ISCC.exe"),
                  str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe"),
                  r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                  r"C:\Program Files\Inno Setup 6\ISCC.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return ""


def numeric_version(version):
    """Inno VersionInfoVersion needs four numeric fields: 1.0.0-test.4 -> 1.0.0.4."""
    numbers = [int(part) for part in re.findall(r"\d+", version)[:4]]
    while len(numbers) < 4:
        numbers.append(0)
    return ".".join(str(value) for value in numbers)


def prepare_source(archive, work):
    archive = Path(archive)
    checksum_file = archive.with_name(archive.name + ".sha256")
    if checksum_file.is_file():
        expected = checksum_file.read_text(encoding="ascii").split()[0].strip()
        if digest(archive) != expected:
            raise ValueError("ZIP 校验失败：" + archive.name)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("ZIP 已损坏")
        roots = {Path(name).parts[0] for name in bundle.namelist()}
        if len(roots) != 1:
            raise ValueError("ZIP 顶层目录不唯一")
        prefix = roots.pop()
        for entry in bundle.infolist():
            path = Path(entry.filename)
            if path.is_absolute() or ".." in path.parts or ":" in entry.filename:
                raise ValueError("ZIP 内含不安全路径")
        bundle.extractall(work)
    source = Path(work) / prefix
    if not (source / EXE_NAME).is_file():
        raise ValueError("ZIP 缺少 " + EXE_NAME)
    if not (source / GUI_NAME).is_file():
        raise ValueError("ZIP 缺少无控制台启动入口 " + GUI_NAME)
    if (source / "data").exists():
        raise ValueError("ZIP 含用户数据目录，拒绝打包")
    from build_portable import check_contents
    check_contents(source)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    expected = {row["path"] for row in manifest}
    actual = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}
    if len(expected) != len(manifest) or actual != expected | {"manifest.json"}:
        raise ValueError("ZIP 文件列表与 manifest 不一致")
    for row in manifest:
        path = source / row["path"]
        if path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError("ZIP 文件摘要不一致：" + row["path"])
    for name in ("ocr/tesseract.exe", "_internal/app/assets/brand/locklingo.ico"):
        if not (source / name).is_file():
            raise ValueError("ZIP 缺少内置 OCR 或品牌资源：" + name)
    if not any((source / 'ocr' / name).is_file() for name in ('libtesseract-5.5.dll', 'libtesseract-5.dll')):
        raise ValueError('ZIP 缺少内置 Tesseract C API')
    return source


def launcher_text(setup_name):
    """Interactive fallback sets TEMP only for this process tree and keeps logs."""
    if not re.fullmatch(r'LockLingo-[a-zA-Z0-9.-]+-setup\.exe', setup_name):
        raise ValueError('Invalid setup filename')
    return '\r\n'.join([
        '@echo off', 'setlocal DisableDelayedExpansion',
        f'if not exist "%~dp0{setup_name}" exit /b 2',
        'set "TEMP=%~dp0.installer-temp\\%RANDOM%-%RANDOM%"', 'set "TMP=%TEMP%"',
        'mkdir "%TEMP%"', 'if errorlevel 1 exit /b 3',
        'echo Installer log: "%TEMP%\\setup.log"',
        f'start "" /wait "%~dp0{setup_name}" /LOG="%TEMP%\\setup.log"',
        'set "INSTALL_RESULT=%ERRORLEVEL%"',
        'if not "%INSTALL_RESULT%"=="0" pause', 'exit /b %INSTALL_RESULT%', ''])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", type=Path, required=True, help="build_portable.py 产出的 ZIP")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--iscc", default="", help="ISCC.exe 路径；默认从已知位置探测")
    parser.add_argument("--work-dir", type=Path, default=ROOT / '.temp/installer-build',
                        help="解包工作目录；深层源码副本可指定短路径以兼容编译器路径上限")
    args = parser.parse_args()
    if not args.version or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", args.version):
        parser.error("版本号只能含字母、数字、点和连字符")
    if os.name != "nt":
        parser.error("Build on Windows")
    release_gate.validate(ROOT, args.version)
    iscc = find_iscc(args.iscc)
    if not iscc:
        parser.error("找不到 ISCC.exe：请安装 Inno Setup 6.7+ 或传 --iscc")
    for required in (ISS, ICON, LANGUAGE):
        if not required.is_file():
            parser.error("缺少安装器输入：" + str(required))
    args.output.mkdir(parents=True, exist_ok=True)
    setup_name = f"LockLingo-{args.version}-setup"
    setup_path = args.output / (setup_name + ".exe")
    if setup_path.exists():
        parser.error("安装包已存在，保留旧证据并换一个版本号：" + str(setup_path))
    scratch = args.work_dir
    scratch.mkdir(parents=True, exist_ok=True)
    report = dict(archive=str(args.archive.resolve()), archive_sha256=digest(args.archive),
                  version=args.version, iscc=iscc)
    with tempfile.TemporaryDirectory(prefix="inno-", dir=scratch) as temporary:
        source = prepare_source(args.archive, temporary)
        release_gate.validate_bundle(ROOT, args.version, source)
        if (source / "VERSION").read_text(encoding="utf-8").strip() != args.version:
            raise ValueError("安装器版本与 ZIP 内 VERSION 不一致")
        files = [path for path in source.rglob("*") if path.is_file()]
        report["bundle_files"] = len(files)
        defines = {
            "AppName": APP_NAME,
            "AppVersion": args.version,
            "NumericVersion": numeric_version(args.version),
            "Publisher": PUBLISHER,
            "PublisherURL": PUBLISHER_URL,
            "SourceDir": str(source.resolve()),
            "OutputDir": str(args.output.resolve()),
            "OutputBase": setup_name,
            "IconFile": str(ICON.resolve()),
            "LanguageFile": str(LANGUAGE.resolve()),
            "RunningMessage": RUNNING_MESSAGE,
            "UninstallRunningMessage": UNINSTALL_RUNNING_MESSAGE,
            "KeepDataMessage": KEEP_DATA_MESSAGE,
            "LauncherFile": GUI_NAME,
            "DirectoryMessage": DIRECTORY_MESSAGE,
        }
        command = [iscc, "/Q"]
        # ISCC parses /DName=Value; keep Chinese text inside one argument.
        command += [f"/D{name}={value}" for name, value in defines.items()]
        command.append(str(ISS))
        # 构建日志是可再生产物，不写进用户私有证据目录（也可能没有写权限）。
        log = ROOT / ".temp/build-evidence" / args.version / "installer.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as stream:
            for item in command:
                stream.write(repr(item) + "\n")
            stream.flush()
            result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                    env=dict(os.environ, LANG="en_US.UTF-8"))
        if result.returncode != 0 or not setup_path.is_file():
            raise SystemExit(f"ISCC 编译失败（{result.returncode}），日志：{log}")
        checksum = digest(setup_path)
        (args.output / (setup_path.name + ".sha256")).write_text(
            checksum + "  " + setup_path.name + "\n", encoding="ascii")
        report.update(setup=str(setup_path.resolve()), setup_bytes=setup_path.stat().st_size,
                      setup_sha256=checksum, compile_log=str(log))
        launcher = args.output / f'Install-LockLingo-{args.version}.cmd'
        launcher.write_bytes(launcher_text(setup_path.name).encode('ascii'))
        report.update(interactive_temp_launcher=str(launcher), launcher_sha256=digest(launcher))
        evidence = log.parent / "installer.json"
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
