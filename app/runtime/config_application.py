"""Content-bound UI acknowledgements, never persistent configuration or prices."""
import hashlib
import json
import time

from app.storage import store


def value_token(key, value):
    encoded = json.dumps([key, value], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def result(key, value, state='applied', reason=''):
    return dict(token=value_token(key, value), state=state, reason=reason)


def application_state(root, key, value, *, running):
    if not running:
        return 'saved'
    runtime = store.read_json(store.data_path(root, 'runtime.json'), {})
    if not isinstance(runtime, dict) or type(runtime.get('at')) not in (int, float) or time.time() - runtime['at'] > 10:
        return 'pending'
    from app.runtime.panel_instance import owner, alive
    current = owner(root)
    if (not alive(current.get('process')) or current.get('stopped')
            or runtime.get('pid') != current['process']['pid']
            or runtime.get('config_session') != current.get('token')):
        return 'pending'
    evidence = runtime.get('config_application', {})
    entry = evidence.get(key, {}) if isinstance(evidence, dict) else {}
    if not isinstance(entry, dict) or entry.get('token') != value_token(key, value):
        return 'pending'
    return entry.get('state', 'pending')


def apply_config(config, overlay=None, translator=None, *, api_toggle=True, report=None):
    """Apply supported values, acknowledge failures, repaint only current UI."""
    from app.config.specification import BY_KEY
    from app.config.validation import _value
    applied = []
    evidence = report if report is not None else {}
    visual = []
    labels = dict(font_size='字号')

    def failed(key, exc, label=None):
        evidence[key] = result(key, config[key], 'failed', type(exc).__name__)
        print(f'[warn] {label or labels.get(key,key)}热更失败：{type(exc).__name__}（设置已存进文件，但本次运行没生效）')

    if overlay is not None:
        check_region = vars(overlay).get('_capture_region_applied')
        if 'capture_preset' in config and callable(check_region):
            try:
                state = 'applied' if check_region(config['capture_preset']) else 'pending'
                evidence['capture_preset'] = result('capture_preset',config['capture_preset'],state)
                if state == 'applied':
                    applied.append('capture_preset')
            except Exception as exc:
                failed('capture_preset',exc,'识别区域')
        for key in labels:
            if key not in config:
                continue
            # Disk merge is a persistence snapshot, not evidence of application.
            # Failed font/attribute updates must retry even after that merge.
            previous = getattr(overlay,key,None)
            try:
                value = _value(key, config[key], BY_KEY[key])
                if previous != value:
                    old_attribute = previous
                    old_fonts = {name:getattr(overlay,name,None) for name in ('font_name','font_text')}
                    setattr(overlay,key,int(value))
                    try:
                        overlay._build_fonts()
                    except Exception:
                        setattr(overlay,key,old_attribute)
                        for name,font in old_fonts.items():
                            if font is not None:setattr(overlay,name,font)
                        raise
                    visual.append(key)
                    overlay.config[key] = value
                    applied.append(key)
                evidence[key] = result(key, value)
            except Exception as exc:
                failed(key, exc)
        if visual or vars(overlay).get('_config_redraw_pending', False):
            overlay._display_dirty = True
            try:
                provider = vars(overlay).get('_current_messages')
                messages = provider() if callable(provider) else overlay._last_messages
                overlay.render(messages)
                overlay._config_redraw_pending = False
            except Exception as exc:
                overlay._config_redraw_pending = True
                for key in labels:
                    if key in config:
                        failed(key, exc, '重绘')
                        if key in applied:
                            applied.remove(key)
    if translator is not None:
        backend = ('api_key', 'endpoint', 'model', 'translate_api', 'realtime_learn',
                   'builtins_enabled', 'price_in', 'price_out', 'send_target', 'auto_send')
        try:
            if callable(getattr(type(translator), 'reconfigure', None)):
                applied.extend(translator.reconfigure(config, api_toggle=api_toggle))
            else:
                for key in backend:
                    if key not in config or key == 'translate_api':
                        continue
                    if getattr(translator, key, None) != config[key]:
                        setattr(translator, key, config[key])
                        applied.append(key)
                if api_toggle and 'translate_api' in config:
                    want_offline = not bool(config['translate_api'])
                    if translator.offline != want_offline:
                        translator.offline = want_offline
                        applied.append('translate_api')
            for key in backend:
                if key in config:
                    protected = (key in {'translate_api','realtime_learn'} and bool(config[key]) and
                                 bool(getattr(translator, 'hard_offline', False)))
                    evidence[key] = result(key, config[key], 'protected' if protected else 'applied')
        except Exception as exc:
            for key in backend:
                if key in config:
                    failed(key, exc)
    return applied
