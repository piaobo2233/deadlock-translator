"""Prepare a clean, credential-checked public snapshot; leave original Git untouched."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config.repository import ConfigRepository
from app.storage import store


def main():
    destination=ROOT/'.temp/public-source-1.0.0-test.1'
    if destination.exists():raise FileExistsError('Review/preserve the existing public snapshot before preparing another')
    selected=set()
    for name in ('LICENSE','VERSION','requirements.txt','pyproject.toml','.gitignore','.gitattributes','启动.vbs'):
        selected.add(ROOT/name)
    for folder in ('app','gui','tests','scripts'):
        selected.update((ROOT/folder).rglob('*.py'))
    selected.update((ROOT/'app/recognition/models').glob('*'))
    selected.add(ROOT/'app/translation/game_terms.json')
    selected.update((ROOT/'docs/promo').rglob('*'))
    selected.update((ROOT/'docs/licenses').glob('*'))
    for name in ('docs/THIRD-PARTY-NOTICES.md','docs/releases/v1.0.0-test.1.md','docs/releases/README-public.md',
                 'docs/guides/接收与中文转英文.md','docs/guides/首版测试与分享.md',
                 'tools/create_promo_assets.py','tools/render_promo.py','tools/benchmark_receive.py',
                 'verify/portable_smoke.py','verify/latency_acceptance.py',
                 '.github/ISSUE_TEMPLATE/bug_report.md'):
        selected.add(ROOT/name)
    selected={p for p in selected if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    config=ConfigRepository(store.data_path(ROOT,'config.json'),log=lambda *args:None).load()
    secret=config.get('api_key','').encode()
    rows=[]
    for source in sorted(selected):
        relative=source.relative_to(ROOT)
        if source.is_symlink() or (hasattr(source,'is_junction') and source.is_junction()):
            raise ValueError('Linked source file')
        body=source.read_bytes()
        if len(secret)>8 and secret in body:raise ValueError('Actual credential found in '+str(relative))
        if any(part.casefold() in {'data','backups','.git','.temp','truth_candidates','archive'} for part in relative.parts):
            raise ValueError('Unapproved source path: '+str(relative))
        target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
        rows.append(dict(path=relative.as_posix(),bytes=len(body),sha256=hashlib.sha256(body).hexdigest()))
    body=(ROOT/'docs/releases/README-public.md').read_bytes()
    (destination/'README.md').write_bytes(body)
    rows.append(dict(path='README.md',bytes=len(body),sha256=hashlib.sha256(body).hexdigest()))
    (destination/'PUBLIC-SOURCE-MANIFEST.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    report=dict(destination=str(destination),files=len(rows)+1,bytes=sum(row['bytes'] for row in rows),
                credential_scan='pass',private_history='not_copied',private_game_frames='not_copied')
    store.atomic_write_json(ROOT/'backups/development/first-test-20261015/public-source.json',report)
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
