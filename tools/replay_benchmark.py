"""Measure the production replay in an isolated directory, with optional bounded API trials.

API results are kept in one experiment ledger. Repeats wait the recorded request
duration instead of buying the same request again; reports distinguish both modes.
The ordinary application's replay remains strictly offline.
"""
import argparse
from dataclasses import asdict
import ctypes
import hashlib
import json
import math
import shutil
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.storage import store
from app.storage.locking import config_lock
from app.translation.client import ApiResponse, ChatCompletionClient


class TrialClient:
    def __init__(self, ledger, budget, *, price_in, price_out, allow_new=False):
        if (any(not math.isfinite(value) for value in (budget,price_in,price_out))
                or not 0 < budget <= 5 or price_in < 2 or price_out < 8):
            raise ValueError('Finite experiment budget <= 5 and conservative official prices required')
        self.path, self.budget = Path(ledger), budget
        self.price_in, self.price_out = price_in, price_out
        self.allow_new = allow_new
        self.events = []

    def request(self, **kwargs):
        safe = {k: kwargs[k] for k in ('endpoint','model','text','system_prompt','max_tokens') if k in kwargs}
        key = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
        # UTF-8 byte count plus generous chat framing overestimates token input
        # for this provider. Use peak cache-miss prices, with no cache discount.
        input_bound = len((kwargs['system_prompt'] + kwargs['text']).encode('utf-8')) + 512
        reserve = (input_bound * self.price_in + kwargs.get('max_tokens', 200) * self.price_out) / 1e6
        with config_lock(self.path):
            ledger = store.read_json(str(self.path), dict(version=1, requests={}))
            if not isinstance(ledger, dict) or not isinstance(ledger.get('requests'), dict):
                raise ValueError('Invalid experiment ledger')
            entry = ledger['requests'].get(key)
            if entry is not None:
                if 'response' not in entry:
                    return ApiResponse(reason='测试请求结果未知，禁止重复购买')
                saved = dict(entry)
            else:
                if not self.allow_new:
                    return ApiResponse(reason='本组只复用已记录的真实接口样本')
                reservations = [v['reserved_yuan'] for v in ledger['requests'].values()]
                if any(not math.isfinite(v) or v < 0 for v in reservations):
                    raise ValueError('Invalid experiment reservation')
                total = sum(reservations)
                if total + reserve > self.budget:
                    return ApiResponse(reason='本次测试已达到累计预算上限')
                saved = None
                ledger['requests'][key] = dict(reserved_yuan=reserve, text=kwargs['text'], state='reserved')
                if not store.atomic_write_json(str(self.path), ledger):
                    raise OSError('Cannot reserve experiment budget')
        if saved is not None:
            time.sleep(saved['elapsed_seconds'])
            self.events.append(dict(key=key, mode='recorded_response_and_delay', seconds=saved['elapsed_seconds']))
            return ApiResponse(**saved['response'])
        started = time.monotonic()
        result = ChatCompletionClient().request(**kwargs)
        elapsed = time.monotonic() - started
        with config_lock(self.path):
            ledger = store.read_json(str(self.path), {})
            usage = result.usage
            cost = ((usage.get('prompt_tokens', 0) * self.price_in +
                     usage.get('completion_tokens', 0) * self.price_out) / 1e6) if usage else None
            ledger['requests'][key].update(state='responded', response=asdict(result),
                elapsed_seconds=elapsed, estimated_yuan_peak=cost)
            if not store.atomic_write_json(str(self.path), ledger):
                raise OSError('Cannot save experiment response')
        self.events.append(dict(key=key, mode='live_api', seconds=elapsed))
        return result


def process_cpu(process):
    values = [ctypes.c_ulonglong() for _ in range(4)]
    api = ctypes.windll.kernel32.GetProcessTimes
    api.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    if not api(int(process._handle), *(ctypes.byref(v) for v in values)):
        return None
    return (values[2].value + values[3].value) / 1e7


def run(args):
    import app.main as app
    from app.recognition import ocr
    from app.config.repository import ConfigRepository
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        'app/main.py','app/runtime/video_replay.py','app/runtime/frame_source.py',
        'app/runtime/visual_pulse.py','app/recognition/ocr.py','app/recognition/selection.py',
        'app/recognition/region.py','app/recognition/chat_area.py','app/recognition/tracking.py',
        'app/recognition/quick_chat.py','app/recognition/quick_chat.json','app/recognition/reuse.py',
        'app/vision/presence.py')}
    real_config = ConfigRepository(ROOT / 'data/config.json').load()
    sandbox = ROOT / '.temp/video-replay-runs' / output.name
    argv = ['replay-benchmark', '--replay', str(args.video.resolve()), '--replay-realtime',
            '--replay-start', str(args.start), '--replay-seconds', str(args.seconds),
            '--sandbox-dir', str(sandbox), '--headless', '--offline']
    app._configure_input_mode(argv[1:])
    inputs = output.parent / 'fixed-inputs'
    with config_lock(inputs):
        if not inputs.exists():
            shutil.copytree(sandbox/'data', inputs)
        for name in ('glossary.json','translations.json','local_cache.json','ai_review.json',
                     'static_ui.json','learning_cache.json','config.json'):
            source = inputs/name
            destination = sandbox/'data'/name
            if source.exists():
                shutil.copy2(source,destination)
            elif destination.exists():
                destination.unlink()
    # Manually verified recognition prototypes are part of this algorithm
    # revision, separate from the frozen translation vocabulary/cache inputs.
    visual_source=ROOT/'data/quick_chat_visual.json'
    visual_destination=sandbox/'data/quick_chat_visual.json'
    if visual_source.exists():
        shutil.copy2(visual_source,visual_destination)
        source_hashes['data/quick_chat_visual.json']=hashlib.sha256(visual_source.read_bytes()).hexdigest()
    elif visual_destination.exists():
        visual_destination.unlink()
    config = app.load_config()
    config['chatlog_path']=str(sandbox/'data/chatlog.simulate.jsonl')
    if args.config:
        overrides = json.loads(args.config.read_text(encoding='utf-8'))
        allowed = {'capture_region', 'ocr_interval', 'ocr_votes'}
        if set(overrides) - allowed:
            raise ValueError('Only capture region/interval/votes can be overridden')
        config.update(overrides)
    if not store.atomic_write_json(app.CONFIG_PATH, config):
        raise OSError('Cannot save isolated settings')
    client = TrialClient(args.ledger, args.api_budget, price_in=args.price_in,
                         price_out=args.price_out, allow_new=args.allow_new_api) if args.ledger else None
    if client:
        if (real_config.get('endpoint') != 'https://api.deepseek.com/chat/completions'
                or real_config.get('model') != 'deepseek-flash'
                or not real_config.get('api_key') or not 0 < args.api_budget <= 5
                or args.price_in < 2 or args.price_out < 8):
            raise ValueError('Trial requires configured official Flash key and verified conservative prices/budget')
        original_translator = app.Translator
        class TrialTranslator(original_translator):
            def __init__(self, *positional, **kwargs):
                positional = list(positional)
                if positional:
                    positional[0] = real_config['api_key']
                else:
                    kwargs['api_key'] = real_config['api_key']
                kwargs.update(offline=False, hard_offline=False,
                              price_in=args.price_in, price_out=args.price_out)
                super().__init__(*positional, **kwargs)
                self.client = client
        app.Translator = TrialTranslator
    records, providers, counts = [], [], []
    processes = {}
    cpu_samples = {}
    done = threading.Event()
    def monitor():
        while not done.wait(.25):
            if ocr._RESIDENT is not None:
                p = ocr._RESIDENT.process
                processes[p.pid] = ('ocr_resident', p)
            for pid, (label, process) in list(processes.items()):
                value = process_cpu(process)
                if value is not None:
                    cpu_samples[pid] = (label, value)
    def media_time():
        return (args.start + time.monotonic() - providers[0].epoch
                if providers and providers[0].epoch is not None else None)
    original_frames = app.replay_frame_provider
    def frames(*a, **kw):
        provider, size = original_frames(*a, **kw)
        providers.append(provider)
        processes[provider.process.pid] = ('decoder', provider.process)
        return provider, size
    app.replay_frame_provider = frames
    original_source = app.iter_ocr_source
    def source(*a, **kw):
        for candidate in original_source(*a, **kw):
            records.append(dict(stage='confirmed', at=media_time(), text=candidate.text,
                box=candidate.position, bubble_id=candidate.bubble_id,
                identity=candidate.identity.key if candidate.identity else None,
                source_verified=candidate.evidence.verified if candidate.evidence else None,
                source_reason=candidate.evidence.reason if candidate.evidence else None))
            yield candidate
    app.iter_ocr_source = source
    original_delivery = app.format_delivery
    def delivery(message):
        records.append(dict(stage='dequeued', at=media_time(), text=message.text,
            translation=message.translated, successful=message.successful,
            box=message.position, bubble_id=message.bubble_id, reason=message.reason_code,
            identity=message.identity.key if message.identity else None))
        return original_delivery(message)
    app.format_delivery = delivery
    original_stats = app.new_capture_stats
    def stats():
        value = original_stats()
        counts.append(value)
        return value
    app.new_capture_stats = stats
    started, cpu_started = time.monotonic(), time.process_time()
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    sys.argv = argv
    result = None
    try:
        result = app.run()
        return result
    finally:
        done.set()
        watcher.join(timeout=1)
        for pid,(label, process) in processes.items():
            value = process_cpu(process)
            if value is not None:
                cpu_samples[pid] = (label, value)
        summary = dict(exit_code=result, start=args.start, seconds=args.seconds, sandbox=str(sandbox),
            source_sha256=source_hashes,
            wall_seconds=time.monotonic()-started, python_cpu_seconds=time.process_time()-cpu_started,
            child_cpu_seconds=[dict(pid=pid,label=label,seconds=value) for pid,(label,value) in cpu_samples.items()],
            cpu_scope='Python + known decoder/resident; transient fallback processes excluded',
            settings={k:config.get(k) for k in ('capture_region','ocr_interval','ocr_votes')},
            capture_stats=counts[-1] if counts else {}, records=records,
            ocr_diagnostics=ocr.diagnostics_snapshot(),
            api_events=client.events if client else [],
            boundary='0.1s resampled media grid; confirmation and dequeue, NOT actual overlay pixels or game load')
        if not store.atomic_write_json(str(output/'result.json'),summary):
            raise OSError('Cannot save benchmark result')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--seconds', type=float, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--ledger', type=Path)
    parser.add_argument('--allow-new-api', action='store_true')
    parser.add_argument('--api-budget', type=float, default=0)
    parser.add_argument('--price-in', type=float, default=2)
    parser.add_argument('--price-out', type=float, default=8)
    options = parser.parse_args()
    if options.allow_new_api and (not options.ledger or not 0 < options.api_budget <= 5):
        parser.error('New requests require an experiment ledger and a budget no larger than 5 yuan')
    raise SystemExit(run(options))
