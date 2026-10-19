"""Build both catalogues from a single, verified installation snapshot."""
import hashlib
import html
import re

from .localization import extract, tokens
from .valve import kv3, message_tokens, resources

DEFINITIONS = ('scripts/ping_targets.vdata_c', 'scripts/ping_wheel_message_types.vdata_c',
               'scripts/ping_wheel_messages.vdata_c')
GROUPS = ('citadel_gc_hero_names', 'citadel_gc_mod_names', 'citadel_heroes', 'citadel_main')


def inputs(directory):
    files = [directory / 'resource/localization' / group / (group + '_' + lang + '.txt')
             for group in GROUPS for lang in ('english', 'schinese')]
    files.append(directory / 'pak01_dir.vpk')
    manifest = next((parent / 'appmanifest_1422450.acf' for parent in directory.parents
                     if (parent / 'appmanifest_1422450.acf').is_file()), None)
    if manifest is None:
        raise ValueError('未找到游戏安装清单，请选择 Steam 安装的 Deadlock 目录。')
    return files + [manifest]


def snapshot(directory):
    rows = []
    for path in inputs(directory):
        body = path.read_bytes()
        rows.append(dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest()))
    match = re.search(rb'"buildid"\s+"(\d+)"', (inputs(directory)[-1]).read_bytes())
    if not match:
        raise ValueError('游戏安装清单缺少版本，请等待 Steam 完成更新。')
    archives = set()
    selected = resources(directory, DEFINITIONS, archives)
    return dict(build_id=match[1].decode(), files=rows,
                archives=[dict(path=str(path), stamp=file_stamp(path)) for path in sorted(archives)],
                definitions={name: hashlib.sha256(body).hexdigest() for name, body in selected.items()}), selected


def file_stamp(path):
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]


def build(directory):
    before, selected = snapshot(directory)
    trees = {name: kv3(body) for name, body in selected.items()}
    referenced = message_tokens(trees['scripts/ping_wheel_messages.vdata_c'])
    if not referenced:
        raise ValueError('当前游戏定义中未找到快捷消息，请保留旧资料并反馈游戏版本。')
    localization = directory / 'resource/localization'
    terms = extract(localization)
    main = tokens(localization / 'citadel_main/citadel_main_schinese.txt')
    missing = referenced - main.keys()
    if missing:
        raise ValueError('游戏中文资料与快捷消息定义不一致，请等待游戏更新完成。')
    messages = []
    for token in sorted(referenced | {'ping_ability_ready', 'ping_ability_on_cooldown', 'ping_use_ability'}):
        plain = html.unescape(re.sub(r'<[^>]*>', '', main.get(token, ''))).strip()
        if re.search(r'[\u3400-\u9fff]', plain):
            messages.append(dict(token=token, chinese=plain, definition_referenced=token in referenced))
    abilities = tokens(localization / 'citadel_heroes/citadel_heroes_schinese.txt')
    ability_names = {value for key, value in abilities.items() if key.startswith('ability_') and '/' not in key
                     and len(value) <= 24 and re.search(r'[\u3400-\u9fff]', value)
                     and not re.search(r'[{}<>\n%]', value)}
    quick = dict(version=1, build_id=before['build_id'], messages=messages,
                 names=sorted({row['chinese'] for row in terms['terms']} | ability_names),
                 boundary='Message-role references in installed definitions; actual game enablement is not asserted.')
    after, _ = snapshot(directory)
    if before != after:
        raise ValueError('生成期间游戏资料发生变化，请等 Steam 更新完成后重试。')
    if not terms['terms'] or not quick['messages']:
        raise ValueError('没有生成完整游戏资料，旧缓存保留。')
    return dict(schema_version=1, generator_version=1, game_dir=str(directory),
                snapshot=before, terms=terms, quick_chat=quick)
