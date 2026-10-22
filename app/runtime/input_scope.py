"""Process input scope and data paths; configured before any writes."""
from app.paths import application_root
from app.storage import store
import argparse

def _app_dir() -> str:
    """配置要落在"程序旁边"。

    [!] PyInstaller 打包成 exe 后 __file__ 指向临时解包目录（_MEIPASS），
    重启就没了 —— 所以必须用 sys.executable 的目录，否则设置存不住。
    """
    return application_root()


APP_DIR = _app_dir()


_DIAGNOSTIC_ROOT = ""


CONFIG_PATH = store.data_path(APP_DIR, "config.json")


GLOSSARY_PATH = store.data_path(APP_DIR, "glossary.json")


MEMORY_PATH = store.data_path(APP_DIR, "translations.json")


CACHE_PATH = store.data_path(APP_DIR, "local_cache.json")


PENDING_PATH = store.data_path(APP_DIR, "pending.json")


STATIC_UI_PATH = store.data_path(APP_DIR, "static_ui.json")


STATIC_UI_CANDIDATES_PATH = store.data_path(APP_DIR, "static_ui_candidates.json")


MATCHLOG_DIR = store.data_path(APP_DIR, "局记录")


STATS_PATH = store.data_path(APP_DIR, "stats.json")


GLOSSARY_STATS_PATH = store.data_path(APP_DIR, "glossary_stats.json")


RUNTIME_PATH = store.data_path(APP_DIR, "runtime.json")


def _configure_input_mode(argv):
    """Run before migration, logging, configuration, or first glossary export."""
    global APP_DIR, _DIAGNOSTIC_ROOT
    scope = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    scope.add_argument('--simulate')
    scope.add_argument('--replay')
    scope.add_argument('--sandbox-dir', default='')
    scope.add_argument('--panel-root', default='')
    options, _ = scope.parse_known_args(argv)
    if not (options.simulate or options.replay or options.panel_root):
        if options.sandbox_dir:
            raise ValueError('--sandbox-dir 仅用于模拟/回放')
        return
    if _DIAGNOSTIC_ROOT:
        return
    from app.runtime.input_mode import diagnostic_root
    if options.panel_root:
        from pathlib import Path
        selected = Path(options.panel_root).resolve()
        marker = store.read_json(str(selected / '.input-scope.json'), {})
        if marker != dict(version=1, program_root=str(Path(_app_dir()).resolve())):
            raise ValueError('面板诊断目录必须来自本程序创建的输入沙箱')
        _DIAGNOSTIC_ROOT = str(selected)
    else:
        _DIAGNOSTIC_ROOT = diagnostic_root(_app_dir(), options.sandbox_dir)
    APP_DIR = _DIAGNOSTIC_ROOT
    for symbol, name in {
        'CONFIG_PATH': 'config.json', 'GLOSSARY_PATH': 'glossary.json',
        'MEMORY_PATH': 'translations.json', 'CACHE_PATH': 'local_cache.json',
        'PENDING_PATH': 'pending.json', 'STATIC_UI_PATH': 'static_ui.json',
        'STATIC_UI_CANDIDATES_PATH': 'static_ui_candidates.json',
        'MATCHLOG_DIR': '局记录', 'STATS_PATH': 'stats.json',
        'GLOSSARY_STATS_PATH': 'glossary_stats.json', 'RUNTIME_PATH': 'runtime.json',
    }.items():
        globals()[symbol] = store.data_path(APP_DIR, name)
    print(f'[info] 模拟/回放沙箱：{APP_DIR}；真实 HTTP 已禁止')
