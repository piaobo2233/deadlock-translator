"""Build an allowlisted Windows test ZIP without copying any user's data."""
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
INSTALLER = 'tesseract-ocr-w64-setup-5.5.3.20260724.exe'
INSTALLER_URL = 'https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/' + INSTALLER
INSTALLER_SHA256 = 'bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_licenses(destination):
    packages = ('numpy', 'opencv-python', 'pillow', 'pytesseract', 'ttkbootstrap',
                'packaging', 'defusedxml', 'darkdetect', 'pyinstaller')
    for name in packages:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
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
    for source in [runtime/'LICENSE.txt', *runtime.glob('tcl/*/license.terms')]:
        if source.is_file():
            target = destination/'Python-Tcl-Tk'/source.parent.name/source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    from tkinter import Tcl
    version = Tcl().eval('info patchlevel')
    license_path = ROOT/'docs/licenses'/f'Tcl-{version}.txt'
    if not license_path.is_file():
        raise ValueError('Matching Tcl license must be provided before distribution')
    shutil.copy2(license_path, destination/'Python-Tcl-Tk'/license_path.name)


def check_contents(bundle):
    forbidden_roots = {'data', 'backups', '.git', '.temp', 'verify', 'tests'}
    forbidden_names = {'config.json', 'runtime.json', 'run.log', 'chatlog.jsonl', '.env', 'pystray'}
    for path in bundle.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked release artifact')
        relative = path.relative_to(bundle)
        if (relative.parts[0].casefold() in forbidden_roots
                or any(part.casefold() in forbidden_names for part in relative.parts)):
            raise ValueError('Unexpected private/development artifact: '+str(relative))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr-installer', type=Path, required=True)
    parser.add_argument('--installer-sha256', required=True)
    parser.add_argument('--version', default=(ROOT/'VERSION').read_text().strip()+'-test.1')
    args = parser.parse_args()
    if os.name != 'nt' or sys.maxsize <= 2**32:
        parser.error('Build on 64-bit Windows')
    if not args.version or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in args.version):
        parser.error('Invalid version')
    if args.installer_sha256.lower() != INSTALLER_SHA256 or digest(args.ocr_installer) != INSTALLER_SHA256:
        parser.error('OCR installer checksum mismatch')
    output = ROOT/'dist'
    output.mkdir(exist_ok=True)
    artifact_name = f'DeadlockTranslator-{args.version}-windows-x64'
    archive = output/(artifact_name+'.zip')
    if archive.exists():
        parser.error('Output already exists; preserve it or choose another test version')
    scratch = ROOT/'.temp/portable-build'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='build-', dir=scratch) as temporary:
        work = Path(temporary)
        source = work/'source'
        source.mkdir()
        for folder in ('app', 'gui'):
            shutil.copytree(ROOT/folder, source/folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (source/'scripts').mkdir()
        shutil.copy2(ROOT/'scripts/run.py', source/'scripts/run.py')
        (source/'VERSION').write_text(args.version+'\n', encoding='utf-8')
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--console',
            '--noupx', '--name', 'DeadlockTranslate', '--paths', str(source),
            '--distpath', str(work/'dist'), '--workpath', str(work/'build'), '--specpath', str(work),
            '--add-data', f'{source / "VERSION"};.',
            '--add-data', f'{source / "app/translation/game_terms.json"};app/translation',
            '--add-data', f'{source / "app/recognition/quick_chat.json"};app/recognition',
            '--add-data', f'{source / "app/recognition/models"};app/recognition/models',
            '--collect-data', 'ttkbootstrap']
        for name in ('pystray', 'pandas', 'scipy', 'matplotlib', 'IPython', 'pytest'):
            command.extend(['--exclude-module', name])
        command.append(str(source/'scripts/run.py'))
        env = dict(os.environ, DEEPSEEK_API_KEY='')
        log = ROOT/'backups/development/first-test-20261015/build.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('w', encoding='utf-8') as stream:
            subprocess.run(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
        bundle = work/'dist/DeadlockTranslate'
        shutil.copy2(ROOT/'LICENSE', bundle/'LICENSE')
        shutil.copy2(ROOT/'docs/THIRD-PARTY-NOTICES.md', bundle/'THIRD-PARTY-NOTICES.md')
        shutil.copy2(ROOT/'docs/guides/首版测试与分享.md', bundle/'先读我.md')
        (bundle/'VERSION').write_text(args.version+'\n', encoding='utf-8')
        (bundle/'启动.vbs').write_text('Option Explicit\nDim s,f,r\nSet s=CreateObject("WScript.Shell")\n'
            'Set f=CreateObject("Scripting.FileSystemObject")\nr=f.GetParentFolderName(WScript.ScriptFullName)\n'
            's.CurrentDirectory=r\ns.Run Chr(34) & r & "\\DeadlockTranslate.exe" & Chr(34),0,False\n',
            encoding='ascii')
        installer_dir = bundle/'首次安装OCR'
        installer_dir.mkdir()
        shutil.copy2(args.ocr_installer, installer_dir/INSTALLER)
        (installer_dir/'来源与校验.txt').write_text(
            INSTALLER_URL+'\nSHA-256: '+digest(args.ocr_installer)+'\n原始安装器，未修改。由测试者自行安装。\n',
            encoding='utf-8')
        copy_licenses(bundle/'licenses')
        check_contents(bundle)
        files = sorted(path for path in bundle.rglob('*') if path.is_file())
        manifest = [dict(path=path.relative_to(bundle).as_posix(), bytes=path.stat().st_size,
                         sha256=digest(path)) for path in files]
        (bundle/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    target.write(path, Path(artifact_name)/path.relative_to(bundle))
        checksum = digest(archive)
        (output/(archive.name+'.sha256')).write_text(checksum+'  '+archive.name+'\n', encoding='ascii')
        print(json.dumps(dict(archive=str(archive), bytes=archive.stat().st_size,
                              sha256=checksum, files=len(manifest)+1), ensure_ascii=False))


if __name__ == '__main__':
    main()
