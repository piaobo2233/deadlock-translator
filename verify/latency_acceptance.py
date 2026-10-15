"""Score human-labelled original-appearance/display times, including missed messages."""
import argparse
import json
import math
from pathlib import Path


def summarize(messages):
    groups={name:[] for name in ('all','local','api','unknown')}
    seen=set()
    skipped=0
    for row in messages:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in seen:
            raise ValueError('Every labelled message needs a unique id')
        seen.add(row['id'])
        if type(row.get('eligible')) is not bool:
            raise ValueError('Eligibility must come from the original frame label')
        if not row['eligible']:
            skipped+=1
            continue
        start,end=row.get('appeared_at'),row.get('drawn_at')
        if type(start) not in (int,float) or not math.isfinite(start):
            raise ValueError('Missing original appearance timestamp')
        if end is not None and (type(end) not in (int,float) or not math.isfinite(end) or end < start):
            raise ValueError('Invalid actual display timestamp')
        route=row.get('route','unknown')
        if route not in ('local','api','unknown'):
            raise ValueError('Invalid route')
        elapsed=None if end is None else end-start
        groups['all'].append(elapsed)
        groups[route].append(elapsed)
    def score(values):
        drawn=sorted(v for v in values if v is not None)
        within=sum(v<=2 for v in drawn)
        late=sum(v>4 for v in drawn)
        return dict(eligible=len(values),drawn=len(drawn),missed=len(values)-len(drawn),
            within_2s=within,within_2s_rate=within/len(values) if values else None,
            over_4s=late,p90_drawn_seconds=drawn[math.ceil(.9*len(drawn))-1] if drawn else None,
            target_met=bool(values and within/len(values)>=.9 and late==0))
    return dict(scope='Human-labelled original frames; misses remain in the 90% denominator',
                skipped_by_policy=skipped,groups={name:score(values) for name,values in groups.items()})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('labels',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=summarize(json.loads(args.labels.read_text(encoding='utf-8'))['messages'])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
