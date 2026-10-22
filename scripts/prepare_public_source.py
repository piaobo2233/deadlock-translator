"""Export current source onto a clean public baseline without private history.

Requires a new destination and a clean public checkout. Curated public documents
are retained unless replaced by an explicit public template. Neither Git index
is modified. Reports belong outside the exported tree.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config.repository import ConfigRepository
from app.storage import store
from scripts.public_documents import TEMPLATES

PRIVATE_PARTS = {'data', 'backups', '.git', '.temp', 'truth', 'truth_candidates', 'archive',
                 'plans', 'reviews', '__pycache__'}


def git(base, *arguments):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + Path(base).as_posix(), '-C', str(base),
                                    *arguments])


def public_files(base):
    if git(base, 'status', '--porcelain').strip():
        raise ValueError('Public baseline has local changes; preserve/review them first')
    names = git(base, 'ls-files', '-z').decode('utf-8').strip('\0').split('\0')
    selected = {name: base / name for name in names if name}
    for name in tuple(selected):
        if name.startswith(('app/', 'gui/', 'tests/', 'scripts/')):
            current = ROOT / name
            if current.is_file():
                selected[name] = current
            else:
                del selected[name]
    for folder in ('app', 'gui', 'tests', 'scripts'):
        for source in (ROOT / folder).rglob('*.py'):
            selected[source.relative_to(ROOT).as_posix()] = source
    for folder in ('app/assets/brand', 'app/assets/ui', 'app/recognition/models', 'docs/assets/locklingo', 'docs/assets/deadlock-ui',
                   'docs/licenses', 'scripts/installer', '.github'):
        for source in (ROOT / folder).rglob('*'):
            if source.is_file():
                selected[source.relative_to(ROOT).as_posix()] = source
    for name in (
        'LICENSE', 'VERSION', 'requirements.txt', 'requirements-build.txt', 'pyproject.toml', '.gitignore', '.gitattributes',
        '启动.vbs', 'scripts/build.bat', 'scripts/ocr-runtime-manifest.json',
        'docs/THIRD-PARTY-NOTICES.md', 'docs/guides/安装与首次使用.md',
        'docs/releases/distribution-audit.md', 'docs/releases/ocr-runtime-audit.json',
        'docs/releases/game-data-audit.json',
        'docs/guides/本机游戏资料.md', 'docs/releases/v1.0.0-beta.1.md',
        'docs/releases/ocr-msys2-packages.lock.json', 'docs/releases/ocr-msys2-sources.json',
        'docs/PROJECT-NOTICE.md', 'docs/ACKNOWLEDGEMENTS.md',
        'docs/releases/third-party-history.md', 'docs/releases/v1.0.0-material-review.md',
        'docs/releases/distribution-audit-history.md',
        'docs/releases/README.md', 'tools/extract_game_terms.py',
        'docs/releases/game-data-validation.md',
        'verify/game_resources_smoke.py',
        'docs/guides/首版测试与分享.md', 'docs/guides/接收与中文转英文.md',
        'docs/guides/软件更新.md', 'docs/releases/v1.0.0-test.4-rc.4.md',
        'docs/guides/个人识别区域.md', 'verify/calibration_coverage.py',
        'docs/releases/v1.0.0.md', 'docs/releases/v1.0.0-features.md',
        'docs/releases/v1.0.0-test.4-rc.1.md', 'docs/releases/v1.0.0-test.4-rc.2.md',
        'docs/releases/v1.0.0-test.4-rc.3.md',
        'verify/portable_smoke.py',
        'verify/installer_smoke.py', 'verify/latency_acceptance.py',
        'verify/source_launcher_smoke.py',
    ):
        selected[name] = ROOT / name
    from scripts.release_gate import GAME_FILES, bundled_game_files
    for name in GAME_FILES:
        selected.pop(name, None)
    for name in bundled_game_files(ROOT):
        if (ROOT / name).is_file():
            selected[name] = ROOT / name
    for target, name in TEMPLATES.items():
        template = ROOT / 'docs/releases' / name
        if template.is_file():
            selected[target] = template
    return selected


def checked_payloads(selected):
    config = ConfigRepository(store.data_path(ROOT, 'config.json'), log=lambda *args: None).load(environment={})
    secrets = [value.encode() for value in (config.get('api_key', ''),
                                           os.environ.get('DEEPSEEK_API_KEY', '')) if len(value) > 8]
    private_paths = [str(path).replace('\\', '/').lower().encode() for path in (ROOT, Path.home())]
    payloads = {}
    for name, source in sorted(selected.items()):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or ':' in name:
            raise ValueError('Unsafe public path: ' + name)
        if any(part.casefold() in PRIVATE_PARTS for part in relative.parts):
            raise ValueError('Private/development path selected: ' + name)
        if source.resolve() != source.absolute():
            raise ValueError('Linked source file: ' + name)
        body = source.read_bytes()
        # Audit proofs and upstream notices are byte-bound by SHA-256. Preserve
        # JSON and those verbatim documents instead of changing their line endings.
        pinned_document = (name.startswith('docs/licenses/')
                           or name == 'docs/releases/game-data-validation.md')
        if not pinned_document and relative.suffix in {'.py', '.md', '.toml', '.yml', '.html', '.svg'}:
            body = body.replace(b'\r\n', b'\n')
        if any(secret in body for secret in secrets):
            raise ValueError('Actual credential found in: ' + name)
        normalized = re.sub(rb'[/\\]+', b'/', body.lower())
        if any(path in normalized for path in private_paths):
            raise ValueError('Private machine path found in: ' + name)
        payloads[name] = body
    return payloads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-base', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    from scripts.release_gate import validate_game_data
    validate_game_data(ROOT)
    base, destination = args.public_base.resolve(), args.destination.absolute()
    if destination.exists():
        parser.error('Destination already exists; preserve it and select a new path')
    if any(args.report.absolute().is_relative_to(path) for path in (destination, base)):
        parser.error('Private report must be outside the exported tree and public baseline')
    payloads = checked_payloads(public_files(base))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if not store.atomic_write_json(args.report, dict(status='preparing', destination=str(destination))):
        raise OSError('Cannot save private export evidence; destination was not created')
    rows = []
    for name, body in payloads.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        rows.append(dict(path=name, bytes=len(body), sha256=hashlib.sha256(body).hexdigest()))
    report = dict(status='complete', destination=str(destination), public_base=str(base),
                  base_commit=git(base, 'rev-parse', 'HEAD').decode().strip(), files=rows,
                  credential_scan='pass', private_path_scan='pass', user_data='not_copied')
    if not store.atomic_write_json(args.report, report):
        raise OSError('Cannot save private export evidence')
    print(json.dumps(dict(destination=str(destination), files=len(rows),
                          bytes=sum(row['bytes'] for row in rows), report=str(args.report)),
                     ensure_ascii=False))


if __name__=='__main__':main()
