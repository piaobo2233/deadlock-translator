"""Safe source-only checkout and inherited real-network rejection for checks."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

NETWORK_GUARD = '''import os,socket,urllib.request
def reject(*args,**kwargs): raise AssertionError('Real network forbidden by check fixture')
socket.socket.connect=reject
socket.socket.connect_ex=reject
socket.create_connection=reject
urllib.request.urlopen=reject
os.environ['DEEPSEEK_API_KEY']=''
os.environ['DEADLOCK_TEST_NO_NETWORK']='1'
'''


def create_scope(root):
    parent=root/'.temp'/'check'
    parent.mkdir(parents=True,exist_ok=True)
    output=Path(tempfile.mkdtemp(prefix=datetime.now().strftime('isolated-%Y%m%d-%H%M%S-'),dir=parent))
    source=output/'source'
    source.mkdir()
    for path in root.iterdir():
        if path.is_file() and (path.suffix in {'.py','.md','.bat','.vbs','.toml','.txt'} or path.name in
                              {'.gitignore','.gitattributes','LICENSE','VERSION'}):
            if path.name not in {'tess_words.txt'}:
                shutil.copy2(path,source/path.name)
    ignore=shutil.ignore_patterns('__pycache__','.temp','.git','日志存档','*.key','.env','.env.*')
    for name in ('app','gui','tests','scripts','verify','tools','docs','.github'):
        if (root/name).is_dir(): shutil.copytree(root/name,source/name,ignore=ignore)
    guard=output/'guard'
    guard.mkdir()
    (guard/'sitecustomize.py').write_text(NETWORK_GUARD,encoding='utf-8')
    recovered=[]
    labels=source/'verify/truth_candidates/candidates.jsonl'
    if labels.exists():
        for line in labels.read_text(encoding='utf-8').splitlines():
            row=json.loads(line)
            relative=row['frame'].replace('\\','/')
            path=source/relative
            if path.exists(): continue
            # Recover only the known, existing human negative labels; never
            # restore deleted files in the source checkout or invent pixels.
            if row.get('reviewer') != '博士' or row.get('bubble_present') is not False:
                continue
            git=['git','-c','safe.directory='+root.as_posix(),'-C',str(root)]
            result=subprocess.run([*git,'rev-parse','HEAD'],capture_output=True)
            if result.returncode: continue
            commit=result.stdout.decode().strip()
            blob=subprocess.run([*git,'rev-parse',f'{commit}:{relative}'],capture_output=True)
            if blob.returncode: continue
            content=subprocess.run([*git,'cat-file','blob',blob.stdout.decode().strip()],capture_output=True)
            if content.returncode: continue
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content.stdout)
            recovered.append(dict(path=relative,commit=commit,blob=blob.stdout.decode().strip(),
                                  sha256=hashlib.sha256(content.stdout).hexdigest(),human_label=row))
    (output/'recovered_frames.json').write_text(json.dumps(recovered,ensure_ascii=False,indent=2),encoding='utf-8')
    env=dict(os.environ,DEEPSEEK_API_KEY='',DEADLOCK_CHECK_ISOLATED='1',
             PYTHONPATH=str(guard)+os.pathsep+str(source))
    _write_retention(output, status='active', explicit_keep=False)
    return output,source,guard,env


def _write_retention(output, **state):
    (output/'retention.json').write_text(json.dumps(dict(
        kind='source-only-check-v1', **state), indent=2), encoding='utf-8')


def _remove_scope(output):
    """Only a direct, readable, link-free isolated checkout may be removed."""
    output=Path(output).absolute()
    if (not output.name.startswith('isolated-') or output.parent.resolve() != output.parent
            or output.resolve() != output or output.resolve().parent != output.parent):
        raise ValueError('Unexpected isolated-check path')
    def reject_error(error):
        raise error
    for folder, dirs, files in os.walk(output, onerror=reject_error, followlinks=False):
        for path in (Path(folder), *(Path(folder)/name for name in dirs+files)):
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Linked check artifacts are preserved for manual inspection')
    marker=json.loads((output/'retention.json').read_text(encoding='utf-8'))
    if marker.get('kind') != 'source-only-check-v1' or marker.get('status') not in {'passed','failed'}:
        raise ValueError('Unknown or active check artifacts are preserved')
    shutil.rmtree(output)


def prune_failed_scopes(parent, keep_failures):
    """Old evidence, active checks and explicit --keep scopes never enter rotation."""
    if keep_failures < 0:
        raise ValueError('Failed scope retention must be nonnegative')
    failed=[]
    for output in Path(parent).glob('isolated-*'):
        if output.is_symlink() or (hasattr(output, 'is_junction') and output.is_junction()):
            continue
        try:
            marker=json.loads((output/'retention.json').read_text(encoding='utf-8'))
            if (marker.get('kind') == 'source-only-check-v1' and marker.get('status') == 'failed'
                    and marker.get('explicit_keep') is False and isinstance(marker.get('finished'), str)):
                failed.append((marker['finished'],output))
        except (OSError, ValueError):
            continue
    removed=[]
    for _, output in sorted(failed, key=lambda item:(item[0],item[1].name), reverse=True)[keep_failures:]:
        try:
            _remove_scope(output)
            removed.append(str(output))
            report=Path(parent)/'reports'/output.name/'summary.json'
            if report.is_file() and report.resolve() == report:
                summary=json.loads(report.read_text(encoding='utf-8'))
                summary.update(retained=False,scope_cleanup='rotated_failed_scope')
                report.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        except (OSError, ValueError) as error:
            print(f'[CLEANUP] 保留需人工核查的目录 {output}: {error}',flush=True)
    return removed


def finish_scope(output, summary, report=None, keep=False, keep_failures=3):
    output=Path(output).absolute()
    if keep_failures < 0:
        raise ValueError('Failed scope retention must be nonnegative')
    report=Path(report).absolute() if report is not None else None
    if report is not None and not keep and (report == output or output in report.parents):
        raise ValueError('A report inside the checkout requires --keep')
    report_dir=output.parent/'reports'/output.name
    if report_dir.resolve() != report_dir:
        raise ValueError('Report directory cannot be redirected through links')
    report_dir.mkdir(parents=True,exist_ok=True)
    for path in (output/'source/.temp/check').glob('*'):
        if path.name in {'unit.log','regression.log','suites.json'} and path.is_file():
            shutil.copy2(path,report_dir/path.name)
    recovered=output/'recovered_frames.json'
    if recovered.is_file():
        shutil.copy2(recovered,report_dir/recovered.name)
        for row in json.loads(recovered.read_text(encoding='utf-8')):
            relative=Path(row['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unexpected recovered-frame path')
            source=output/'source'/relative
            if source.resolve() != source or hashlib.sha256(source.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('Recovered frame does not match its recorded evidence')
            target=report_dir/'recovered'/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
    for suite in summary.get('suites',{}).values():
        if suite.get('log'): suite['log']=str(report_dir/Path(suite['log']).name)
    status='passed' if summary['code'] == 0 else 'failed'
    _write_retention(output,status=status,explicit_keep=keep,finished=datetime.now().isoformat())
    summary.update(report_dir=str(report_dir),retained=True,scope_cleanup='explicit_keep' if keep else 'failed')
    if status == 'passed' and not keep:
        try:
            _remove_scope(output)
            summary.update(retained=False,scope_cleanup='removed_successful_scope')
        except (OSError,ValueError) as error:
            summary.update(scope_cleanup='manual_cleanup_required',cleanup_error=str(error))
            print(f'[CLEANUP] 检查通过；目录保留待人工核查：{error}',flush=True)
    saved_report=report_dir/'summary.json'
    saved_report.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    summary['rotated_failed_scopes']=prune_failed_scopes(output.parent,keep_failures)
    # Rotation may include this scope when keep_failures=0.
    if not output.exists():
        summary['retained']=False
        if status == 'failed': summary['scope_cleanup']='rotated_failed_scope'
    saved_report.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    if report is not None:
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return report or saved_report
