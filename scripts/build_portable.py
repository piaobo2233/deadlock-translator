"""Build an allowlisted Windows test ZIP with the bundled OCR runtime.

The ZIP is the input for the per-user installer (scripts/build_installer.py) and
for isolated smoke checks. No user's data, credentials or development artifacts
are copied: only `app/`, `gui/`, `scripts/run.py`, the pinned OCR runtime and
license/notice files.
"""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ocr_runtime  # noqa: E402  (sibling build helper)
import release_gate

EXE_NAME = "DeadlockTranslate"
GUI_NAME = "LockLingo"
BRAND_ICON = ROOT / "app" / "assets" / "brand" / "locklingo.ico"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def copy_licenses(destination):
    shared = ROOT / 'docs/licenses/ValveResourceFormat'
    if shared.is_dir():
        shutil.copytree(shared, destination / 'ValveResourceFormat', dirs_exist_ok=True)
    packages = ('numpy', 'opencv-python', 'pillow', 'pytesseract', 'ttkbootstrap',
                'packaging', 'defusedxml', 'darkdetect', 'pyinstaller', 'pystray', 'six')
    for name in packages:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        package_metadata = distribution.read_text('METADATA')
        if package_metadata:
            target = destination / name / 'METADATA'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(package_metadata, encoding='utf-8')
        for entry in distribution.files or ():
            if not any(word in entry.name.lower() for word in ('license', 'copying', 'notice')):
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            target = destination / name / Path(*entry.parts[-2:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    runtime = Path(sys.base_prefix)
    for source in [runtime / 'LICENSE.txt', *runtime.glob('tcl/*/license.terms')]:
        if source.is_file():
            target = destination / 'Python-Tcl-Tk' / source.parent.name / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    from tkinter import Tcl
    version = Tcl().eval('info patchlevel')
    license_path = ROOT / 'docs/licenses' / f'Tcl-{version}.txt'
    if not license_path.is_file():
        raise ValueError('Matching Tcl license must be provided before distribution')
    shutil.copy2(license_path, destination / 'Python-Tcl-Tk' / license_path.name)


def copy_ocr_licenses(bundle, distribution='local_test_only', *, version=None):
    """随包 OCR 运行时的来源、摘要与许可：不只放一句「来自 Tesseract」。"""
    target = bundle / 'licenses' / 'tesseract'
    target.mkdir(parents=True, exist_ok=True)
    for name in ('LICENSE', 'AUTHORS', 'LEPTONICA-LICENSE.txt', 'TESSERACT-README.md'):
        source = ROOT / 'docs/licenses/tesseract' / name
        if source.is_file():
            shutil.copy2(source, target / name)
    if distribution == 'materials_verified':
        audit = json.loads((ROOT / 'docs/releases/ocr-runtime-audit.json').read_text(encoding='utf-8'))
        for row in audit['files']:
            for evidence in row['evidence']:
                if evidence['role'] in {'license', 'notice', 'source_build', 'package_metadata', 'binary_source'}:
                    source = ROOT / evidence['path']
                    destination = target / 'components' / evidence['path']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
        shutil.copy2(ROOT / 'docs/releases/ocr-runtime-audit.json', target / 'distribution-audit.json')
    manifest = ocr_runtime.load_manifest()
    shutil.copy2(ocr_runtime.MANIFEST_PATH, target / 'runtime-manifest.json')
    identity = ocr_runtime.source_identity(manifest)
    version = version or (ROOT / 'VERSION').read_text().strip()
    lines = [
        f"随包 OCR 运行时：{manifest['runtime']}",
        f"来源：{identity['url']}",
        f"来源锁定 SHA-256：{identity['sha256']}",
        f"文件选择：{manifest['selection']}",
        "",
        "逐文件 SHA-256 见 licenses/tesseract/runtime-manifest.json 与包内 manifest.json。",
        "各组件的原始许可、NOTICE、GCC runtime exception 与构建元数据均保留。",
        "MSYS2 二进制包原样提取，未修改；Tesseract 原始许可 Apache-2.0 不覆盖其余 DLL。",
        "GPL 适用的 OCR 组合按 GPL-3.0-or-later 条款提供，原创 MIT 声明保留。",
        "完整对应源码、配方和补丁与本版本二进制一同免费提供：",
        f"https://github.com/piaobo2233/deadlock-translator/releases/tag/v{version}",
        f"附件名：LockLingo-{version}-ocr-sources.zip；精确逐包目录见包内 source evidence。",
        ("本候选包只作本地验收；公开分发前必须补齐传递组件的许可与再分发依据。"
         if distribution == 'local_test_only' else "本构建材料核对记录见 licenses/tesseract/distribution-audit.json。"),
        "",
    ]
    (bundle / 'OCR-RUNTIME.txt').write_text("\n".join(lines), encoding='utf-8')


def check_contents(bundle):
    forbidden_roots = {'data', 'backups', '.git', '.temp', 'verify', 'tests'}
    forbidden_names = {'config.json', 'runtime.json', 'run.log', 'chatlog.jsonl', '.env'}
    for path in bundle.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked release artifact')
        relative = path.relative_to(bundle)
        if (relative.parts[0].casefold() in forbidden_roots
                or any(part.casefold() in forbidden_names for part in relative.parts)):
            raise ValueError('Unexpected private/development artifact: ' + str(relative))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr-runtime', type=Path, default=ocr_runtime.DEFAULT_RUNTIME,
                        help='已按 scripts/ocr-runtime-manifest.json 校验过的 OCR 运行时目录')
    parser.add_argument('--version', default=(ROOT / 'VERSION').read_text().strip())
    parser.add_argument('--no-zip', action='store_true', help='只构建 allowlisted 目录，不打包 ZIP')
    args = parser.parse_args()
    if os.name != 'nt' or sys.maxsize <= 2**32:
        parser.error('Build on 64-bit Windows')
    if not args.version or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-'
                               for c in args.version):
        parser.error('Invalid version')
    distribution = release_gate.validate(ROOT, args.version)
    if not BRAND_ICON.is_file():
        parser.error('Missing brand icon: ' + str(BRAND_ICON))
    manifest = ocr_runtime.validate(args.ocr_runtime)
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    artifact_name = f'LockLingo-{args.version}-windows-x64'
    archive = output / (artifact_name + '.zip')
    if archive.exists():
        parser.error('Output already exists; preserve it or choose another test version')
    scratch = ROOT / '.temp/portable-build'
    scratch.mkdir(parents=True, exist_ok=True)
    bundle_out = output / artifact_name
    if bundle_out.exists():
        parser.error('Unpacked bundle already exists; preserve it or choose another test version')
    with tempfile.TemporaryDirectory(prefix='build-', dir=scratch) as temporary:
        work = Path(temporary)
        source = work / 'source'
        source.mkdir()
        from release_gate import GAME_FILES, bundled_game_files
        excluded_game_files = set(GAME_FILES) - set(bundled_game_files(ROOT))
        for folder in ('app', 'gui'):
            shutil.copytree(ROOT / folder, source / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc',
                                *(Path(name).name for name in excluded_game_files)))
        (source / 'scripts').mkdir()
        shutil.copytree(ROOT / 'scripts/installer/hooks', work / 'hooks')
        shutil.copy2(ROOT / 'scripts/run.py', source / 'scripts/run.py')
        (source / 'VERSION').write_text(args.version + '\n', encoding='utf-8')
        source_manifest = [dict(path=path.relative_to(source).as_posix(), sha256=digest(path))
                           for path in sorted(source.rglob('*')) if path.is_file()]
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm',
                   '--distpath', str(work / 'dist'), '--workpath', str(work / 'build'),
                   str(ROOT / 'scripts/installer/locklingo.spec')]
        env = dict(os.environ, DEEPSEEK_API_KEY='', LOCKLINGO_BUILD_SOURCE=str(source))
        # 构建日志是可再生产物，不写进用户私有证据目录（也可能没有写权限）。
        log = ROOT / '.temp/build-evidence' / args.version / 'portable-build.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('w', encoding='utf-8') as stream:
            subprocess.run(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
        bundle = work / 'dist' / EXE_NAME
        shutil.copy2(ROOT / 'LICENSE', bundle / 'LICENSE')
        shutil.copy2(ROOT / 'docs/THIRD-PARTY-NOTICES.md', bundle / 'THIRD-PARTY-NOTICES.md')
        sources_notice = ROOT / 'docs/releases/third-party-sources.md'
        if sources_notice.is_file():
            shutil.copy2(sources_notice, bundle / 'THIRD-PARTY-SOURCES.md')
        shutil.copy2(ROOT / 'docs/guides/首版测试与分享.md', bundle / '先读我.md')
        (bundle / 'VERSION').write_text(args.version + '\n', encoding='utf-8')
        (bundle / 'DISTRIBUTION.json').write_text(json.dumps(distribution, indent=2) + '\n', encoding='utf-8')
        (bundle / 'BUILD-SOURCE.json').write_text(json.dumps(
            dict(version=args.version, python=sys.version.split()[0], files=source_manifest),
            ensure_ascii=False, indent=2), encoding='utf-8')
        ocr_runtime.copy_into(bundle, args.ocr_runtime, manifest)
        copy_ocr_licenses(bundle, distribution['distribution'], version=args.version)
        copy_licenses(bundle / 'licenses')
        check_contents(bundle)
        files = sorted(path for path in bundle.rglob('*') if path.is_file())
        manifest_rows = [dict(path=path.relative_to(bundle).as_posix(), bytes=path.stat().st_size,
                              sha256=digest(path)) for path in files]
        (bundle / 'manifest.json').write_text(
            json.dumps(manifest_rows, ensure_ascii=False, indent=2), encoding='utf-8')
        if args.no_zip:
            shutil.copytree(bundle, bundle_out)
            print(json.dumps(dict(bundle=str(bundle_out), files=len(manifest_rows) + 1),
                             ensure_ascii=False))
            return 0
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    target.write(path, Path(artifact_name) / path.relative_to(bundle))
        checksum = digest(archive)
        (output / (archive.name + '.sha256')).write_text(checksum + '  ' + archive.name + '\n',
                                                         encoding='ascii')
        print(json.dumps(dict(archive=str(archive), bytes=archive.stat().st_size,
                              sha256=checksum, files=len(manifest_rows) + 1), ensure_ascii=False))


if __name__ == '__main__':
    main()
