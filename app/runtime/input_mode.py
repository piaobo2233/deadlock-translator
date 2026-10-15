"""Input-mode scope: diagnostic runs own isolated data and no network permission."""

from pathlib import Path
import shutil
import tempfile

from app.storage import store


def diagnostic_root(program_root, requested=""):
    program = Path(program_root).resolve()
    if requested:
        root = Path(requested).resolve()
        protected_data = (program / 'data').resolve()
        if root == program or root in program.parents or root == protected_data or protected_data in root.parents:
            raise ValueError('沙箱不能使用程序目录或真实数据目录')
        for relative in ('backups','.git','verify/truth','verify/truth_candidates','.temp/video-review-20261010'):
            protected=(program/relative).resolve()
            if root == protected or protected in root.parents:
                raise ValueError('沙箱不能使用受保护数据或素材目录')
        if root.exists() and any(root.iterdir()):
            raise ValueError('沙箱必须是新的或空目录，避免覆盖既有文件')
        root.mkdir(parents=True, exist_ok=True)
    else:
        parent = program / '.temp' / 'input-runs'
        if parent.resolve() != parent:
            raise ValueError('默认沙箱目录不能通过链接重定向；请指定安全新目录')
        parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix='diagnostic-', dir=parent))
    data = root / 'data'
    data.mkdir()
    # Copy only known local lookup inputs. Audit/session/output paths never
    # follow a production-configured absolute path into the real data tree.
    for name in ('glossary.json', 'translations.json', 'local_cache.json',
                 'ai_review.json', 'static_ui.json', 'learning_cache.json'):
        source = program / 'data' / name
        if source.is_file():
            shutil.copy2(source, data / name)
    config = store.read_json(str(program / 'data' / 'config.json'), {})
    config = dict(config) if isinstance(config, dict) else {}
    config.update(api_key='', dev_key_fallback=False, translate_api=False,
                  realtime_learn=False, chatlog_path=str(data / 'chatlog.simulate.jsonl'),
                  dump_dir='')
    if not store.atomic_write_json(str(data / 'config.json'), config):
        raise OSError('沙箱配置未保存')
    if not store.atomic_write_json(str(root / '.input-scope.json'), dict(version=1, program_root=str(program))):
        raise OSError('沙箱归属未保存')
    return str(root)
