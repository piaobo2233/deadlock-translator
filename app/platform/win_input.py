"""Win32 hotkeys, sticky input/focus invalidation, and bounded clipboard IO."""
import ctypes as c
from ctypes import wintypes as w
from pathlib import PureWindowsPath
import threading
import time

from app.platform.game_input import parse_hotkey

ULONG_PTR = c.c_size_t
LRESULT = c.c_ssize_t
MARKER = 0x444C5452


class KEYBDINPUT(c.Structure):
    _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', ULONG_PTR)]


class MOUSEINPUT(c.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD),
                ('dwFlags', w.DWORD), ('time', w.DWORD), ('dwExtraInfo', ULONG_PTR)]


class INPUTUNION(c.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT)]


class INPUT(c.Structure):
    _fields_ = [('type', w.DWORD), ('data', INPUTUNION)]


class KBDHOOK(c.Structure):
    _fields_ = [('vkCode', w.DWORD), ('scanCode', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', ULONG_PTR)]


class MOUSEHOOK(c.Structure):
    _fields_ = [('pt', w.POINT), ('mouseData', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', ULONG_PTR)]


class GUITHREADINFO(c.Structure):
    _fields_ = [('cbSize', w.DWORD), ('flags', w.DWORD),
                *[(name, w.HWND) for name in ('active','focus','capture','menu','move','caret')],
                ('rect', w.RECT)]


class WindowsInput:
    def __init__(self, hotkeys, dispatch, report):
        self.user = c.WinDLL('user32', use_last_error=True)
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.hotkeys, self.dispatch, self.report = hotkeys, dispatch, report
        self.revision = 0
        self.ready = False
        self.closed = threading.Event()
        self.started = threading.Event()
        self.thread_id = 0
        self.clipboard_saved = None
        self.clipboard_owned = None
        self._prototypes()

    def _prototypes(self):
        self.HOOKPROC = c.WINFUNCTYPE(LRESULT, c.c_int, w.WPARAM, w.LPARAM)
        self.EVENTPROC = c.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
        signatures = {
            'GetForegroundWindow': ([], w.HWND),
            'GetWindowThreadProcessId': ([w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            'GetGUIThreadInfo': ([w.DWORD, c.POINTER(GUITHREADINFO)], w.BOOL),
            'SendInput': ([w.UINT, c.POINTER(INPUT), c.c_int], w.UINT),
            'SetWindowsHookExW': ([c.c_int, self.HOOKPROC, w.HINSTANCE, w.DWORD], w.HANDLE),
            'CallNextHookEx': ([w.HANDLE, c.c_int, w.WPARAM, w.LPARAM], LRESULT),
            'UnhookWindowsHookEx': ([w.HANDLE], w.BOOL),
            'SetWinEventHook': ([w.DWORD,w.DWORD,w.HMODULE,self.EVENTPROC,w.DWORD,w.DWORD,w.DWORD], w.HANDLE),
            'UnhookWinEvent': ([w.HANDLE], w.BOOL),
            'RegisterHotKey': ([w.HWND,c.c_int,w.UINT,w.UINT], w.BOOL),
            'UnregisterHotKey': ([w.HWND,c.c_int], w.BOOL),
            'PostThreadMessageW': ([w.DWORD,w.UINT,w.WPARAM,w.LPARAM], w.BOOL),
            'PeekMessageW': ([c.POINTER(w.MSG),w.HWND,w.UINT,w.UINT,w.UINT], w.BOOL),
            'GetMessageW': ([c.POINTER(w.MSG),w.HWND,w.UINT,w.UINT], w.BOOL),
            'DispatchMessageW': ([c.POINTER(w.MSG)], LRESULT),
            'GetClipboardData': ([w.UINT], w.HANDLE),
            'GetClipboardOwner': ([], w.HWND),
            'SetClipboardData': ([w.UINT,w.HANDLE], w.HANDLE),
            'OpenClipboard': ([w.HWND], w.BOOL),
            'GetClipboardSequenceNumber': ([], w.DWORD),
        }
        for name, (args, ret) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, ret
        for name, args, ret in (
                ('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
                ('CloseHandle',[w.HANDLE],w.BOOL),
                ('QueryFullProcessImageNameW',[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)],w.BOOL),
                ('GlobalAlloc',[w.UINT,c.c_size_t],w.HGLOBAL),
                ('GlobalLock',[w.HGLOBAL],c.c_void_p),
                ('GlobalUnlock',[w.HGLOBAL],w.BOOL),
                ('GlobalFree',[w.HGLOBAL],w.HGLOBAL)):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, ret

    def start(self):
        self.thread = threading.Thread(target=self._run, name='dl-hotkeys', daemon=True)
        self.thread.start()
        self.started.wait(2)
        return self.ready

    def _run(self):
        hooks, registered = [], []
        try:
            self.thread_id = self.kernel.GetCurrentThreadId()
            msg = w.MSG()
            self.user.PeekMessageW(c.byref(msg), None, 0, 0, 0)
            def keyboard(code, wp, lp):
                if code >= 0 and wp in (0x100, 0x104):
                    event = c.cast(lp, c.POINTER(KBDHOOK)).contents
                    if event.dwExtraInfo != MARKER:
                        self.revision += 1
                    elif (event.vkCode not in (0x10,0x11,0x12) and
                          not self.current(getattr(self, 'active_token', None))):
                        return 1
                return self.user.CallNextHookEx(None, code, wp, lp)
            def mouse(code, wp, lp):
                if code >= 0 and wp in (0x201,0x204,0x207,0x20a,0x20b,0x20e):
                    if c.cast(lp, c.POINTER(MOUSEHOOK)).contents.dwExtraInfo != MARKER:
                        self.revision += 1
                return self.user.CallNextHookEx(None, code, wp, lp)
            def focus(*args):
                self.revision += 1
            self.callbacks = (self.HOOKPROC(keyboard), self.HOOKPROC(mouse), self.EVENTPROC(focus))
            hooks = [self.user.SetWindowsHookExW(13, self.callbacks[0], None, 0),
                     self.user.SetWindowsHookExW(14, self.callbacks[1], None, 0),
                     self.user.SetWinEventHook(3,3,None,self.callbacks[2],0,0,0),
                     self.user.SetWinEventHook(0x8005,0x8005,None,self.callbacks[2],0,0,0)]
            if not all(hooks):
                raise OSError('Cannot watch input/focus')
            for number, (name, value) in enumerate(self.hotkeys.items(), 1):
                mods, key = parse_hotkey(value)
                if not self.user.RegisterHotKey(None, number, mods | 0x4000, key):
                    raise OSError('Hotkey conflict')
                registered.append((number, name))
            self.ready = True
            self.started.set()
            while not self.closed.is_set():
                outcome = self.user.GetMessageW(c.byref(msg), None, 0, 0)
                if outcome <= 0:
                    break
                if msg.message == 0x312:
                    action = dict(registered).get(msg.wParam)
                    if action:
                        self.dispatch(action)
                else:
                    self.user.DispatchMessageW(c.byref(msg))
        except Exception as exc:
            self.report('快捷键未启用（' + type(exc).__name__ + '）；请检查快捷键冲突后重启助手')
        finally:
            self.ready = False
            self.started.set()
            for number, _name in registered:
                self.user.UnregisterHotKey(None, number)
            for handle in hooks[:2]:
                if handle:
                    self.user.UnhookWindowsHookEx(handle)
            for handle in hooks[2:]:
                if handle:
                    self.user.UnhookWinEvent(handle)

    def target(self):
        hwnd = self.user.GetForegroundWindow()
        pid = w.DWORD()
        tid = self.user.GetWindowThreadProcessId(hwnd, c.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            path, length = c.create_unicode_buffer(32768), w.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, c.byref(length)):
                return None
            if PureWindowsPath(path.value).name.lower() != 'deadlock.exe':
                return None
        finally:
            self.kernel.CloseHandle(handle)
        info = GUITHREADINFO(cbSize=c.sizeof(GUITHREADINFO))
        if not self.user.GetGUIThreadInfo(tid, c.byref(info)):
            return None
        return (hwnd, pid.value, info.focus, info.caret)

    def begin(self):
        target, revision = self.target(), self.revision
        # Never release physical modifier keys on the player's behalf.
        end = time.monotonic() + 1.5
        while any(self.user.GetAsyncKeyState(k) & 0x8000 for k in (0x10,0x11,0x12)):
            if time.monotonic() >= end or self.target() != target or self.revision != revision:
                return None
            time.sleep(.01)
        if not target or not self.ready or self.revision != revision:
            return None
        self.clipboard_saved = self.clipboard_read()
        self.clipboard_owned = None
        self.active_token = (target, revision)
        return self.active_token

    def current(self, token):
        return bool(self.ready and not self.closed.is_set() and token == (self.target(), self.revision))

    def keys(self, keys, guard):
        if not guard():
            return False
        events = []
        for vk, up in keys:
            events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(vk,0,2 if up else 0,0,MARKER))))
        array = (INPUT * len(events))(*events)
        return self.user.SendInput(len(events), array, c.sizeof(INPUT)) == len(events)

    def chord(self, key, guard):
        return self.keys([(0x11,False),(key,False),(key,True),(0x11,True)], guard)

    def clipboard_read(self):
        if not self.user.OpenClipboard(None):
            raise OSError('Clipboard busy')
        try:
            handle = self.user.GetClipboardData(13)
            ptr = self.kernel.GlobalLock(handle) if handle else None
            if not ptr:
                return None
            try:
                return c.wstring_at(ptr)
            finally:
                self.kernel.GlobalUnlock(handle)
        finally:
            self.user.CloseClipboard()

    def clipboard_write(self, value):
        data = (value + '\0').encode('utf-16-le')
        handle = self.kernel.GlobalAlloc(2, len(data))
        if not handle:
            raise OSError('Clipboard allocation')
        try:
            ptr = self.kernel.GlobalLock(handle)
            if not ptr:
                raise OSError('Clipboard lock')
            c.memmove(ptr, data, len(data))
            self.kernel.GlobalUnlock(handle)
            if not self.user.OpenClipboard(None):
                raise OSError('Clipboard busy')
            try:
                if not self.user.EmptyClipboard() or not self.user.SetClipboardData(13, handle):
                    raise OSError('Clipboard write')
                handle = None  # Ownership transferred to Windows.
                self.clipboard_owned = self.user.GetClipboardSequenceNumber()
            finally:
                self.user.CloseClipboard()
        finally:
            if handle:
                self.kernel.GlobalFree(handle)

    def read_input(self, token):
        guard = lambda: self.current(token)
        before = self.user.GetClipboardSequenceNumber()
        if not self.chord(ord('A'), guard) or not self.chord(ord('C'), guard):
            return ''
        end = time.monotonic() + .4
        while time.monotonic() < end:
            if not guard():
                return ''
            sequence = self.user.GetClipboardSequenceNumber()
            if sequence != before:
                owner_pid = w.DWORD()
                self.user.GetWindowThreadProcessId(self.user.GetClipboardOwner(), c.byref(owner_pid))
                if owner_pid.value != token[0][1]:
                    return ''
                value = self.clipboard_read()
                self.clipboard_owned = sequence
                return value or ''
            time.sleep(.01)
        return ''

    def replace(self, token, text, current):
        if not current():
            return False
        self.clipboard_write(text)
        if not current() or not self.chord(ord('V'), current):
            return False
        # Give the game's message loop time to consume the paste before readback/restoration.
        time.sleep(.08)
        return current() and self.read_input(token) == text

    def send(self, token, current):
        return self.current(token) and self.keys([(0x0d,False),(0x0d,True)], current)

    def finish(self):
        if (self.clipboard_owned is not None and self.clipboard_saved is not None
                and self.user.GetClipboardSequenceNumber() == self.clipboard_owned):
            self.clipboard_write(self.clipboard_saved)
        self.clipboard_saved = self.clipboard_owned = None

    def close(self):
        self.closed.set()
        if self.thread_id:
            self.user.PostThreadMessageW(self.thread_id, 0x12, 0, 0)
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1)
