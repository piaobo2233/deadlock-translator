"""One physical-pixel rectangle for selection, preview and capture."""
import threading
import time


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


def preset(screen):
    width,height=screen
    # Both teams' portraits, text overhang, and room for stacked bubbles.
    top=round(190*height/1440)
    return dict(rect=[round(339*width/2560),top,round(2220*width/2560),
                      min(height,top+round(.4*height))],screen=list(screen))


PRESETS = ('auto', '1920x1080', '2560x1440', '3840x2160')


def supported_screen(screen, selected='auto'):
    w,h=screen
    if w < 1280 or h < 720 or abs(w*9-h*16) > 16:
        raise ValueError('当前画面不在首轮主屏16:9适配范围，识别已暂停；请使用主屏全屏或无边框')
    if selected not in PRESETS:
        raise ValueError('画面预设无效，识别已暂停')
    if selected != 'auto' and tuple(map(int,selected.split('x'))) != tuple(screen):
        raise ValueError('所选预设与主屏实际像素不一致，识别已暂停；请选择自动或匹配的分辨率')


def resolve_region(config, screen):
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
    from app.main import game_in_foreground,DPI_AWARE
    user=ctypes.windll.user32
    screen=(user.GetSystemMetrics(0),user.GetSystemMetrics(1))
    if game_in_foreground():
        if not DPI_AWARE:raise ValueError('未能确认物理像素坐标，识别已暂停；请检查DPI设置')
        user.GetForegroundWindow.restype=wintypes.HWND
        user.GetClientRect.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.RECT))
        user.ClientToScreen.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.POINT))
        hwnd=user.GetForegroundWindow();rect=wintypes.RECT();origin=wintypes.POINT()
        if not hwnd or not user.GetClientRect(hwnd,ctypes.byref(rect)) or not user.ClientToScreen(hwnd,ctypes.byref(origin)):
            raise ValueError('未能确认游戏客户区，识别已暂停')
        require_primary_client((origin.x,origin.y,origin.x+rect.right,origin.y+rect.bottom),screen)
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

    def snapshot(self):
        with self._lock:
            now = self.clock()
            stamp = self.stamp() if self.stamp is not None else None
            if now >= self._next_read or stamp != self._stamp:
                config=self.load_config()
                self._region = ({'capture_preset':config['capture_preset']} if 'capture_preset' in config
                                else validate_region(config.get('capture_region', {})))
                self._next_read = now + .1
                self._stamp = stamp
            screen = tuple(self.screen_size())
            capture=(self._region if 'capture_preset' in self._region else {'capture_region':self._region})
            return screen, resolve_region(capture,screen), self._region

    def current(self):
        try:snapshot = self.snapshot()
        except ValueError:return False
        with self._lock:
            return self._applied == snapshot

    def applied(self, value):
        with self._lock:
            return self.current() and (self._applied[2].get('capture_preset',self._applied[2]) == value)

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
                       bool(snapshot[2].get('screen') and snapshot[2]['screen'] != list(snapshot[0])))
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
                yield from factory(screen_size=snapshot[0],capture_config=(snapshot[2] if 'capture_preset' in snapshot[2] else {'capture_region':snapshot[2]}),
                                   capture_changed=changed,capture_ready=ready)
            finally:
                with self._lock:
                    self._applied = None
            if not changed():
                return


class RecognitionGate:
    """Manual requests open a short sampling burst; visual tracking may continue."""
    def __init__(self, mode='realtime', *, clock=time.monotonic):
        self.mode, self.clock = mode, clock
        self._until = 0.0
        self._lock = threading.Lock()

    def request(self):
        with self._lock:
            self._until = self.clock() + 3.5

    def allowed(self):
        with self._lock:
            return self.mode == 'realtime' or self.clock() < self._until
