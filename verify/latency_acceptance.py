"""Score human-labelled footage, requiring correctness and natural sample coverage."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

RESOLUTIONS = ('1920x1080', '2560x1440')
ROUTES = ('local', 'api', 'unknown')
SOURCES = ('natural', 'controlled', 'unknown')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _score(rows):
    drawn = sorted(row['elapsed'] for row in rows if row['elapsed'] is not None)
    timely = sum(row['correct'] is True and row['elapsed'] is not None
                 and row['elapsed'] <= 2 for row in rows)
    late = sum(value > 4 for value in drawn)
    stages = {}
    for row in rows:
        for name, seconds in row['stages'].items():
            stages.setdefault(name, []).append(seconds)
    return dict(
        eligible=len(rows), drawn=len(drawn), missed=len(rows) - len(drawn),
        incorrect=sum(row['elapsed'] is not None and row['correct'] is False for row in rows),
        correctness_unconfirmed=sum(row['elapsed'] is not None and row['correct'] is None for row in rows),
        within_2s=timely, within_2s_rate=timely / len(rows) if rows else None,
        between_2s_and_4s=sum(2 < value <= 4 for value in drawn), over_4s=late,
        p90_drawn_seconds=drawn[math.ceil(.9 * len(drawn)) - 1] if drawn else None,
        stage_seconds={name: dict(samples=len(values), mean=sum(values) / len(values), maximum=max(values))
                       for name, values in sorted(stages.items())},
        target_met=bool(rows and timely / len(rows) >= .9 and late == 0))


def summarize(messages, sessions=None):
    """Group targets are diagnostic; only the top-level target signs off the release sample."""
    if not isinstance(messages, list):
        raise ValueError('Messages must be a list')
    sessions = [] if sessions is None else sessions
    if not isinstance(sessions, list):
        raise ValueError('Sessions must be a list')
    session_map = {}
    for session in sessions:
        if (not isinstance(session, dict) or not isinstance(session.get('id'), str)
                or not session['id'].strip() or session['id'] in session_map
                or type(session.get('complete')) is not bool):
            raise ValueError('Sessions need unique ids and explicit complete flags')
        session_map[session['id']] = session
    seen, rows = set(), []
    skipped = 0
    for row in messages:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'].strip() or row['id'] in seen):
            raise ValueError('Every labelled message needs a unique id')
        seen.add(row['id'])
        if type(row.get('eligible')) is not bool:
            raise ValueError('Eligibility must come from the original frame label')
        if not row['eligible']:
            skipped+=1
            continue
        start,end=row.get('appeared_at'),row.get('drawn_at')
        if not _number(start):
            raise ValueError('Missing original appearance timestamp')
        if end is not None and (not _number(end) or end < start):
            raise ValueError('Invalid actual display timestamp')
        correct = row.get('correct')
        if correct is not None and type(correct) is not bool:
            raise ValueError('Correctness must be true, false or null')
        if end is None and correct is not None:
            raise ValueError('An undisplayed message cannot have confirmed correctness')
        route, source = row.get('route', 'unknown'), row.get('source', 'unknown')
        if route not in ROUTES or source not in SOURCES:
            raise ValueError('Invalid route or sample source')
        resolution = row.get('resolution', 'unknown')
        if (not isinstance(resolution, str)
                or resolution != 'unknown' and not re.fullmatch(r'[1-9]\d*x[1-9]\d*', resolution)):
            raise ValueError('Resolution must be WIDTHxHEIGHT or unknown')
        session_id = row.get('session_id')
        if session_id is not None and (not isinstance(session_id, str) or session_id not in session_map):
            raise ValueError('Message references an unrecorded session')
        stages = row.get('stages', {})
        if (not isinstance(stages, dict)
                or any(not isinstance(name, str) or not name.strip() or not _number(value)
                       for name, value in stages.items())):
            raise ValueError('Stage durations must be finite nonnegative seconds')
        rows.append(dict(elapsed=None if end is None else end - start, correct=correct,
                         route=route, source=source, resolution=resolution,
                         session_id=session_id, stages=stages))

    def by_route(items):
        return dict(all=_score(items), **{
            route: _score([row for row in items if row['route'] == route]) for route in ROUTES})

    natural = [row for row in rows if row['source'] == 'natural']
    groups = by_route(rows)
    names = sorted(set(RESOLUTIONS) | {row['resolution'] for row in rows})
    natural_groups = by_route(natural)
    natural_by_resolution = {
        name: by_route([row for row in natural if row['resolution'] == name]) for name in names}
    complete = {row['session_id'] for row in natural
                if row['session_id'] in session_map and session_map[row['session_id']]['complete']}
    failures = []
    if len(complete) < 3:
        failures.append('fewer_than_3_complete_matches')
    if len(natural) < 100:
        failures.append('fewer_than_100_natural_messages')
    for resolution in RESOLUTIONS:
        group = natural_by_resolution[resolution]['all']
        if group['eligible'] < 50:
            failures.append(f'{resolution}:fewer_than_50_natural_messages')
        if not group['target_met']:
            failures.append(f'{resolution}:timely_correct_rate_or_deadline_failed')
    if natural_groups['api']['eligible'] < 30:
        failures.append('fewer_than_30_natural_api_messages')
    if not natural_groups['api']['target_met']:
        failures.append('api:timely_correct_rate_or_deadline_failed')
    if not natural_groups['all']['target_met']:
        failures.append('natural:timely_correct_rate_or_deadline_failed')
    if any(row['resolution'] not in RESOLUTIONS for row in natural):
        failures.append('natural_messages_outside_declared_resolutions')
    if any(row['session_id'] is None for row in natural):
        failures.append('natural_messages_missing_session')
    if any(row['source'] == 'unknown' for row in rows):
        failures.append('unclassified_sample_source')
    if groups['all']['over_4s']:
        failures.append('display_after_4s')
    acceptance = dict(target_met=not failures, failures=failures, complete_matches=len(complete),
                      natural_messages=len(natural), natural_api_messages=natural_groups['api']['eligible'])
    return dict(schema_version=2,
                scope='Human-labelled original frames on one recording timebase; correctness required',
                skipped_by_policy=skipped, groups=groups,
                by_source={source: by_route([row for row in rows if row['source'] == source])
                           for source in SOURCES},
                by_resolution={name: by_route([row for row in rows if row['resolution'] == name])
                               for name in names},
                natural_groups=natural_groups, natural_by_resolution=natural_by_resolution,
                acceptance=acceptance, target_met=acceptance['target_met'])


def template():
    """An intentionally incomplete private label sheet, never a successful sample."""
    return dict(schema_version=2, candidate_sha256='', batch_id='', recording_timebase='',
                capture_window='', initial_cache_state='',
                environment=dict(os='', game_build='', provider='DeepSeek', model='deepseek-flash'),
                sessions=[dict(id='replace-match-id', complete=False)],
                messages=[dict(id='replace-message-id', session_id='replace-match-id', eligible=True,
                               appeared_at=0, drawn_at=None, correct=None, route='unknown',
                               source='natural', resolution='1920x1080', stages={},
                               evidence='', reviewer='', correctness_note='')])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('labels', type=Path, nargs='?')
    parser.add_argument('--template', action='store_true', help='Write an incomplete private label template')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.template == (args.labels is not None):
        parser.error('Supply labels or --template, not both')
    if args.labels and args.output.resolve() == args.labels.resolve():
        parser.error('Output must not overwrite the human label sheet')
    if args.output.exists():
        parser.error('Output already exists; use a new batch path')
    try:
        labels = template() if args.template else json.loads(args.labels.read_text(encoding='utf-8-sig'))
        result = labels if args.template else summarize(labels['messages'], labels.get('sessions'))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if not args.template:
        result['labels_sha256'] = hashlib.sha256(args.labels.read_bytes()).hexdigest()
        result['context'] = {name: labels.get(name) for name in (
            'candidate_sha256', 'batch_id', 'recording_timebase', 'capture_window',
            'initial_cache_state', 'environment')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not args.template:
        print(json.dumps(result['acceptance'], ensure_ascii=False))
    return 0 if args.template or result['target_met'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
