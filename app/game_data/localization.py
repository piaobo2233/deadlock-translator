"""Read installed localization pairs; export names and provenance, never game assets."""
import hashlib,json,re
from pathlib import Path

PAIR=re.compile(r'^\s*"((?:\\.|[^"\\])*)"\s+"((?:\\.|[^"\\])*)"')
def tokens(path,raw=None):
    raw=path.read_bytes() if raw is None else raw
    text=raw.decode('utf-16' if raw[:2] in (b'\xff\xfe',b'\xfe\xff') else 'utf-8-sig')
    result={}
    for line in text.splitlines():
        match=PAIR.match(line)
        if match:
            try:key,value=(json.loads('"'+part+'"') for part in match.groups())
            except ValueError:continue
            result[key]=value
    return result

def extract(directory):
    records={};sources=[];rejected=0
    for group,category in [('citadel_gc_hero_names','英雄'),('citadel_gc_mod_names','物品'),
                           ('citadel_heroes','技能'),('citadel_main','地图与目标')]:
        files=[directory/group/(group+'_'+lang+'.txt') for lang in ('english','schinese')]
        if not all(p.is_file() for p in files):continue
        snapshots=[p.read_bytes() for p in files]
        english,chinese=(tokens(p,raw) for p,raw in zip(files,snapshots))
        sources.extend(dict(file=p.relative_to(directory).as_posix(),sha256=hashlib.sha256(raw).hexdigest()) for p,raw in zip(files,snapshots))
        for token,en in english.items():
            zh=chinese.get(token,'')
            name=(group.endswith('_names') and '_search' not in token or
                  group=='citadel_heroes' and token.startswith('citadel_ability_') and not re.search(r'_(desc|quip|t\d|upgrade|tooltip|duration|range|damage|passive)',token) or
                  group=='citadel_main' and token in {'Citadel_LaneColorBlue','Citadel_LaneColorGreen','Citadel_LaneColorPurple','Citadel_LaneColorYellow','Citadel_LaneStats_Guardians'})
            if not name or token.endswith('_ui_meter'):continue
            if (not re.search(r'[\u4e00-\u9fff]',zh) or not en.strip() or len(en)>64 or len(zh)>40
                    or any(ch in en+zh for ch in '{}<>\n') or en==zh):
                rejected+=1;continue
            key=en.casefold().strip();row=dict(english=en,chinese=zh,category=category,tokens=[token],source=group)
            if key in records:
                if records[key]['chinese']!=zh:records[key]['ambiguous']=True
                else:records[key]['tokens'].append(token)
            else:records[key]=row
    build={}
    for parent in directory.parents:
        manifest=parent/'appmanifest_1422450.acf'
        if manifest.is_file():
            raw=manifest.read_bytes();match=re.search(rb'"buildid"\s+"(\d+)"',raw)
            if match:build=dict(build_id=match[1].decode(),manifest_sha256=hashlib.sha256(raw).hexdigest())
            break
    return dict(version=1,source_kind='installed Valve localization; same-install token pairing',build=build,
        sources=sources,rejected=rejected,terms=[row for row in records.values() if not row.get('ambiguous')])


