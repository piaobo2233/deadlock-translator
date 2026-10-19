"""Prepare actual local game data with the frozen CLI in a new private extraction.

No game assets, API keys or generated cache are written into the distribution ZIP.
Retains the isolated extraction and report as development evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.storage import store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--game-directory', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    parent = ROOT / '.temp/game-resources-smoke'; parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='frozen-', dir=parent))
    with zipfile.ZipFile(args.archive) as archive:
        for member in archive.infolist():
            path = Path(member.filename)
            if path.is_absolute() or '..' in path.parts or ':' in member.filename or '\\' in member.filename:
                raise ValueError('Unsafe archive member')
        roots = {Path(name).parts[0] for name in archive.namelist()}
        if len(roots) != 1:
            raise ValueError('Archive must contain one bundle')
        archive.extractall(scratch)
    bundle = scratch / roots.pop()
    for row in json.loads((bundle / 'manifest.json').read_text(encoding='utf-8')):
        body = (bundle / row['path']).read_bytes()
        assert len(body) == row['bytes'] and hashlib.sha256(body).hexdigest() == row['sha256']
    assert not (bundle / 'data').exists()
    for relative in ('app/translation/game_terms.json', 'app/recognition/quick_chat.json'):
        assert not (bundle / '_internal' / relative).exists()
    env = dict(os.environ, DEEPSEEK_API_KEY='', PYTHONPATH='', PYTHONHOME='',
               PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
    operation = subprocess.run([str(bundle / 'DeadlockTranslate.exe'), '--prepare-game-resources',
                                str(args.game_directory.resolve())], cwd=scratch, env=env,
                               capture_output=True, text=True, encoding='utf-8', timeout=60,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert operation.returncode == 0, operation.stderr
    response = json.loads(operation.stdout)
    cache = store.read_json(bundle / 'data/game-resources.json', None)
    assert cache['snapshot']['build_id'] == response['build_id']
    assert cache['quick_chat']['build_id'] == cache['terms']['build']['build_id'] == response['build_id']
    assert len(cache['terms']['terms']) == response['terms'] > 0
    assert len(cache['quick_chat']['messages']) == response['messages'] > 0
    assert response['restart_required'] is True
    report = dict(result='pass', archive_sha256=hashlib.sha256(args.archive.read_bytes()).hexdigest(),
                  build_id=response['build_id'], terms=response['terms'], messages=response['messages'],
                  developer_catalogues_excluded=True, python_not_on_path=True,
                  source='actual installed game; frozen local generation, not in-game OCR acceptance',
                  scratch=str(scratch), retained=True, real_api_calls=0)
    assert store.atomic_write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
