"""One physical-pixel rectangle for selection, preview and capture."""
import threading
import time
from copy import deepcopy
from datetime import datetime

_PRIMARY_GEOMETRY = {}


def primary_geometry():
    """Latest physical geometry only; periodic diagnostics own persistence."""
    return dict(_PRIMARY_GEOMETRY)


def validate_region(value):
    if value == {}:
        return {}
    if not isinstance(value, dict) or set(value) != {'rect', 'screen'}:
        raise ValueError('Invalid region')
    rect, screen = value['rect'], value['screen']
    if not isinstance(rect, (list, tuple)) or not isinstance(screen, (list, tuple)) or len(rect) != 4 or len(screen) != 2:
        raise ValueError('Invalid dimensions')
    if any(type(v) is not int for v in (*rect, *screen)):
        raise ValueError('Region must use integer pixels')
    x0, y0, x1, y1 = rect
    w, h = screen
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h and x1-x0 > 30 and y1-y0 > 20):
        raise ValueError('Region outside screen or too small')
    return dict(rect=list(rect), screen=list(screen))


def validate_calibration(value):
    if value == {}:
        return {}
    fields = {'version', 'screen', 'rect', 'source', 'method',
              'full_frame_confirmed', 'coverage', 'created_at'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('个人区域记录格式无效')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('个人区域记录版本不支持')
    geometry = validate_region({key: value[key] for key in ('rect', 'screen')})
    supported_screen(geometry['screen'])
    if value['source'] not in ('imported_screenshot', 'game_capture'):
        raise ValueError('个人区域来源无效')
    if value['method'] not in ('reference_confirmed', 'user_adjusted'):
        raise ValueError('个人区域设置方式无效')
    if value['full_frame_confirmed'] is not True:
        raise ValueError('请确认图片是完整原始局内截图')
    if value['coverage'] not in ('unverified', 'user_checked'):
        raise ValueError('聊天覆盖状态无效')
    try:
        stamp = datetime.fromisoformat(value['created_at'])
        if stamp.tzinfo is None:
            raise ValueError('Missing timezone')
    except (ValueError, TypeError):
        raise ValueError('个人区域记录时间无效') from None
    return dict(value, **geometry)


def calibration_for_screen(config, screen):
    try:
        saved = validate_calibration(config.get('capture_calibration', {}))
    except (ValueError, TypeError):
        return {}, '个人区域记录无效，当前使用参考范围'
    if config.get('_capture_calibration_error'):
        return {}, '个人区域记录无效，当前使用参考范围'
    if saved and saved['screen'] != list(screen):
        return {}, '个人区域适用于 %d×%d；当前使用参考范围' % tuple(saved['screen'])
    return saved, ''


def capture_settings(config):
    """Own a complete immutable-by-convention snapshot, including legacy basis."""
    return deepcopy({key: config[key] for key in (
        'capture_preset', 'capture_region', 'capture_calibration', '_capture_calibration_error') if key in config})


def preset(screen):
    width,height=screen
    if abs(width*10-height*16) <= 16:
        # 2304x1440 recording: the left long bubble begins at x=260.
        # Width-based 16:9 scaling cuts it at x=305; retain the measured HUD
        # width at the same height, centred on the physical game picture.
        scale=height/1440
        left=(width-2304*scale)/2
        return dict(rect=[max(0,round(left+211*scale)),round(190*scale),
                          min(width,round(left+2092*scale)),round(766*scale)],
                    screen=list(screen))
    # Both teams' portraits, text overhang, and room for stacked bubbles.
    top=round(190*height/1440)
    return dict(rect=[round(339*width/2560),top,round(2220*width/2560),
                      min(height,top+round(.4*height))],screen=list(screen))


PRESETS = ('auto', '1920x1080', '2560x1440', '3840x2160')

# The original gate admitted only a 16:9 primary screen. The search region is
# derived from screen size, but HUD anchoring is measured for 16:9 only; other
# shapes stay usable as experimental adaptation until verified per ratio.
MIN_SCREEN = (1280, 720)
# 4:3 (1.33) through 32:9 (3.56), plus margin. Portrait or absurd geometry is
# still rejected: it cannot be a normal game picture.
ASPECT_RANGE = (1.2, 3.6)


def six_nine(screen):
    """True when the primary screen is close to 16:9 (fully verified shape)."""
    w, h = screen
    return abs(w*9-h*16) <= 16


def experimental_aspect(screen):
    """Non-16:9 screen: usable, but its HUD anchoring is not verified yet."""
    return not six_nine(screen)


def supported_screen(screen, selected='auto'):
    w,h=screen
    if w < MIN_SCREEN[0] or h < MIN_SCREEN[1]:
        raise ValueError('当前主屏小于 1280×720，识别已暂停；请使用主屏全屏或无边框')
    if not ASPECT_RANGE[0] <= w/h <= ASPECT_RANGE[1]:
        raise ValueError('当前画面比例暂不支持（%d×%d），识别已暂停；请使用主屏全屏或无边框' % (w,h))
    if selected not in PRESETS:
        raise ValueError('画面预设无效，识别已暂停')
    # Old fixed choices are only a preference; a resolution change must not
    # permanently disable recognition. Geometry still comes from the screen.


def region_details(config, screen):
    rect = resolve_region(config, screen)
    personal, reason = calibration_for_screen(config, screen)
    selected = config.get('capture_preset', 'auto')
    fallback = selected in PRESETS[1:] and tuple(map(int, selected.split('x'))) != tuple(screen)
    manual = 'capture_preset' not in config and config.get('capture_region')
    calibrated = abs(screen[0]*10-screen[1]*16) <= 16
    source = ('个人区域' if personal else '保存的手动区域' if manual else '16:10 录像调校布局（待完整验收）' if calibrated
              else '16:9 参考布局按实际像素换算')
    return dict(width=screen[0], height=screen[1], region=list(rect), source=source,
                region_source='personal' if personal else 'saved_manual' if manual else 'recording16_10' if calibrated else 'reference16_9',
                fallback=bool(reason) or fallback, fallback_reason=reason,
                verification='已由用户核对当前截图' if personal.get('coverage') == 'user_checked' else '聊天覆盖待验证')


def resolve_region(config, screen):
    if config.get('capture_calibration') or config.get('_capture_calibration_error'):
        supported_screen(screen, config.get('capture_preset', 'auto'))
        personal, _ = calibration_for_screen(config, screen)
        if personal:
            return tuple(personal['rect'])
    if 'capture_preset' in config:
        supported_screen(screen,config['capture_preset'])
        return tuple(preset(screen)['rect'])
    saved = validate_region(config.get('capture_region', {}))
    if not saved:
        return tuple(preset(screen)['rect'])
    if saved['screen'] == list(screen):
        return tuple(saved['rect'])
    # Scale from the original saved basis each time; never accumulate rounding by saving it back.
    old_w, old_h = saved['screen']
    w, h = screen
    x0,y0,x1,y1 = saved['rect']
    x0,x1 = round(x0*w/old_w), round(x1*w/old_w)
    y0,y1 = round(y0*h/old_h), round(y1*h/old_h)
    x0,y0 = min(max(0,x0),max(0,w-31)), min(max(0,y0),max(0,h-21))
    x1,y1 = min(w,max(x0+31,x1)), min(h,max(y0+21,y1))
    return tuple(validate_region(dict(rect=[x0,y0,x1,y1],screen=list(screen)))['rect'])


def require_primary_client(client_rect,screen):
    if tuple(client_rect)!=(0,0,*screen):
        raise ValueError('游戏画面未覆盖主屏：窗口化、副屏或画面黑边尚未校准，识别已暂停')


def primary_game_screen():
    """Physical primary pixels; reject a foreground game client on another surface."""
    import ctypes
    from ctypes import wintypes
    from app.platform.desktop import game_in_foreground
    from app.platform.desktop import DPI_AWARE
    user=ctypes.windll.user32
    screen=(user.GetSystemMetrics(0),user.GetSystemMetrics(1))
    global _PRIMARY_GEOMETRY
    geometry = dict(width=screen[0], height=screen[1], code='geometry')
    try:
        dpi = user.GetDpiForSystem()
        if dpi > 0:geometry['dpi'] = dpi
    except (AttributeError, OSError):
        pass  # Missing DPI remains unknown, never guessed as 96.
    _PRIMARY_GEOMETRY = geometry
    if game_in_foreground():
        if not DPI_AWARE:
            _PRIMARY_GEOMETRY = dict(geometry, code='dpi')
            raise ValueError('未能确认物理像素坐标，识别已暂停；请检查DPI设置')
        user.GetForegroundWindow.restype=wintypes.HWND
        user.GetClientRect.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.RECT))
        user.ClientToScreen.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.POINT))
        hwnd=user.GetForegroundWindow();rect=wintypes.RECT();origin=wintypes.POINT()
        if not hwnd or not user.GetClientRect(hwnd,ctypes.byref(rect)) or not user.ClientToScreen(hwnd,ctypes.byref(origin)):
            _PRIMARY_GEOMETRY = dict(geometry, code='client')
            raise ValueError('未能确认游戏客户区，识别已暂停')
        client = (origin.x,origin.y,origin.x+rect.right,origin.y+rect.bottom)
        _PRIMARY_GEOMETRY = dict(geometry, client=client, code='client')
        require_primary_client(client,screen)
        _PRIMARY_GEOMETRY = dict(geometry, client=client)
    return screen


class RegionCapture:
    """Rebuild one capture source when its physical geometry changes, without stopping the pipeline."""
    def __init__(self, load_config, screen_size, *, clock=time.monotonic, stamp=None, notify=lambda text: None):
        self.load_config, self.screen_size, self.clock = load_config, screen_size, clock
        self._next_read = -1.0
        self.stamp, self._stamp = stamp, None
        self.notify = notify
        self._region = {}
        self._applied = None
        self._lock = threading.RLock()
        self.geometry = {}

    def snapshot(self):
        with self._lock:
            now = self.clock()
            stamp = self.stamp() if self.stamp is not None else None
            if now >= self._next_read or stamp != self._stamp:
                config=self.load_config()
                self._region = capture_settings(config)
                self._next_read = now + .1
                self._stamp = stamp
            self.geometry = {}
            screen = tuple(self.screen_size())
            capture=self._region
            self.geometry = region_details(capture, screen)
            return screen, resolve_region(capture,screen), deepcopy(self._region)

    def current(self):
        try:snapshot = self.snapshot()
        except ValueError:return False
        with self._lock:
            return self._applied == snapshot

    def applied(self, value, *, key=None):
        with self._lock:
            if not self.current():
                return False
            config = self._applied[2]
            key = key or ('capture_preset' if 'capture_preset' in config else 'capture_region')
            if key == 'capture_calibration' and value:
                saved, _ = calibration_for_screen(config, self._applied[0])
                return bool(saved) and saved == value
            return config.get(key, {}) == value

    def iter_source(self, factory, stop_event):
        previous_screen = None
        last_error = ''
        while not stop_event.is_set():
            try:
                snapshot = self.snapshot()
            except ValueError as exc:
                with self._lock:self._applied=None
                if str(exc)!=last_error:
                    self.notify(str(exc));last_error=str(exc)
                stop_event.wait(.25)
                continue
            if last_error:
                self.notify('画面预设已匹配，恢复识别');last_error=''
            resized = (previous_screen != snapshot[0] if previous_screen is not None else
                       bool(snapshot[2].get('capture_region', {}).get('screen') and
                            snapshot[2]['capture_region']['screen'] != list(snapshot[0])))
            previous_screen = snapshot[0]
            def changed():
                try:return self.snapshot() != snapshot
                except ValueError:return True
            def ready():
                with self._lock:
                    if not changed():
                        self._applied = snapshot
                        if resized:
                            self.notify('屏幕分辨率已变化，识别区域已按像素适配；请在设置的识别与显示中测试识别')
            try:
                yield from factory(screen_size=snapshot[0],capture_config=deepcopy(snapshot[2]),
                                   capture_changed=changed,capture_ready=ready)
            finally:
                with self._lock:
                    self._applied = None
            if not changed():
                return


class RecognitionGate:
    """Manual requests open a short sampling burst; visual tracking may continue."""
    def __init__(self, mode='realtime', *, clock=time.monotonic):
        self.mode, self.clock = ('realtime' if mode == 'manual' else mode), clock
        self._until = 0.0
        self._lock = threading.Lock()

    def request(self):
        with self._lock:
            self._until = self.clock() + 3.5

    def allowed(self):
        with self._lock:
            return self.mode == 'realtime' or self.clock() < self._until
