"""Exclusive Win32 clipboard IO without hotkeys, input injection or input hooks."""
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
import os
from pathlib import PureWindowsPath


@dataclass(frozen=True)
class ClipboardSnapshot:
    sequence: int
    window: int
    pid: int
    owner: int
    owner_pid: int


@dataclass(frozen=True)
class ClipboardWrite:
    stage: str
    sequence: int | None = None
    modified: bool = False


class LogFont(c.Structure):
    _fields_ = [(name, w.LONG) for name in ('height', 'width', 'escapement', 'orientation', 'weight')] + [
        (name, w.BYTE) for name in ('italic', 'underline', 'strikeout', 'charset',
                                   'out_precision', 'clip_precision', 'quality', 'pitch')] + [('face', w.WCHAR*32)]


class WindowsClipboard:
    def __init__(self):
        self.user = c.WinDLL('user32', use_last_error=True)
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('GetForegroundWindow', [], w.HWND),
            ('GetClipboardOwner', [], w.HWND),
            ('GetClipboardSequenceNumber', [], w.DWORD),
            ('GetWindowThreadProcessId', [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            ('OpenClipboard', [w.HWND], w.BOOL), ('CloseClipboard', [], w.BOOL),
            ('IsClipboardFormatAvailable', [w.UINT], w.BOOL),
            ('GetClipboardData', [w.UINT], w.HANDLE),
            ('EmptyClipboard', [], w.BOOL), ('SetClipboardData', [w.UINT, w.HANDLE], w.HANDLE),
            ('CreateWindowExW', [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                 c.c_int, c.c_int, c.c_int, c.c_int,
                                 w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p], w.HWND),
            ('DestroyWindow', [w.HWND], w.BOOL),
            ('ShowWindow', [w.HWND, c.c_int], w.BOOL),
            ('SetWindowTextW', [w.HWND, w.LPCWSTR], w.BOOL),
            ('SetWindowPos', [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT], w.BOOL),
            ('PeekMessageW', [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL),
            ('TranslateMessage', [c.POINTER(w.MSG)], w.BOOL),
            ('DispatchMessageW', [c.POINTER(w.MSG)], c.c_ssize_t),
        ):
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in (
            ('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ('QueryFullProcessImageNameW', [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL),
            ('CloseHandle', [w.HANDLE], w.BOOL),
            ('GlobalAlloc', [w.UINT, c.c_size_t], w.HGLOBAL),
            ('GlobalLock', [w.HGLOBAL], c.c_void_p),
            ('GlobalUnlock', [w.HGLOBAL], w.BOOL),
            ('GlobalSize', [w.HGLOBAL], c.c_size_t),
            ('GlobalFree', [w.HGLOBAL], w.HGLOBAL),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        # This dedicated, invisible window is created and destroyed by the IO thread.
        self.window = self.user.CreateWindowExW(0, 'STATIC', 'LockLingo clipboard owner',
                                               0, 0, 0, 0, 0, None, None, None, None)
        if not self.window:
            raise OSError('Clipboard owner window unavailable')
        self.notice = None
        self.notice_text = self.notice_brush = self.notice_font = None

    def _pid(self, window):
        pid = w.DWORD()
        if window:
            self.user.GetWindowThreadProcessId(window, c.byref(pid))
        return pid.value

    def snapshot(self):
        window, owner = self.user.GetForegroundWindow(), self.user.GetClipboardOwner()
        return ClipboardSnapshot(self.user.GetClipboardSequenceNumber(), window or 0,
                                 self._pid(window), owner or 0, self._pid(owner))

    def game(self, snapshot):
        handle = self.kernel.OpenProcess(0x1000, False, snapshot.pid) if snapshot.pid else None
        if not handle:
            return False
        try:
            path, length = c.create_unicode_buffer(32768), w.DWORD(32768)
            return bool(self.kernel.QueryFullProcessImageNameW(handle, 0, path, c.byref(length))
                        and PureWindowsPath(path.value).name.lower() == 'deadlock.exe')
        finally:
            self.kernel.CloseHandle(handle)

    def read(self, expected, current=lambda: True):
        if not expected.owner_pid or expected.owner_pid != expected.pid or not self.game(expected):
            return 'source', ''
        if not current():
            return 'changed', ''
        if not self.user.OpenClipboard(None):
            return 'busy', ''
        try:
            if self.snapshot() != expected or not current():
                return 'changed', ''
            if not self.user.IsClipboardFormatAvailable(13):
                return 'format', ''
            handle = self.user.GetClipboardData(13)
            size = self.kernel.GlobalSize(handle) if handle else 0
            if size < 2 or size > 1024 * 1024 or size % 2:
                return 'buffer', ''
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                return 'read', ''
            try:
                decoded = c.string_at(pointer, size).decode('utf-16-le')
                if '\0' not in decoded:
                    return 'buffer', ''
                value = decoded.split('\0', 1)[0].strip()
            except UnicodeError:
                return 'buffer', ''
            finally:
                self.kernel.GlobalUnlock(handle)
            return ('ok', value) if self.snapshot() == expected and current() else ('changed', '')
        finally:
            self.user.CloseClipboard()

    def write(self, value, expected, current):
        data = (value + '\0').encode('utf-16-le')
        handle = self.kernel.GlobalAlloc(2, len(data))
        if not handle:
            return ClipboardWrite('allocation')
        modified = False
        try:
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                return ClipboardWrite('allocation')
            try:
                c.memmove(pointer, data, len(data))
            finally:
                self.kernel.GlobalUnlock(handle)
            if not self.user.OpenClipboard(self.window):
                return ClipboardWrite('busy')
            try:
                # Both checks are inside the lock, immediately before EmptyClipboard.
                if self.snapshot() != expected or not current():
                    return ClipboardWrite('changed')
                if not self.user.EmptyClipboard():
                    return ClipboardWrite('empty')
                modified = True
                if not self.user.SetClipboardData(13, handle):
                    return ClipboardWrite('set', modified=True)
                handle = None
            finally:
                self.user.CloseClipboard()
            # CloseClipboard can change the sequence. Reopen, verify our exclusive
            # owner and claim exactly one sequence, never a broad new baseline.
            sequence = None
            if self.user.OpenClipboard(None):
                try:
                    observed = self.snapshot()
                    if observed.owner == self.window and observed.owner_pid == os.getpid():
                        sequence = observed.sequence
                finally:
                    self.user.CloseClipboard()
            return ClipboardWrite('written' if sequence is not None else 'superseded', sequence, modified)
        finally:
            if handle:
                self.kernel.GlobalFree(handle)

    def show_notice(self, message):
        if not message:
            if self.notice:
                self.user.ShowWindow(self.notice, 0)
            return
        if not self.notice:
            self._create_notice()
        success = message == '已复制英文，可 Ctrl+V 粘贴'
        text = '英文已复制 · Ctrl+V' if success else message
        scale = self.notice_scale
        notice_width, notice_height = round((224 if success else 400)*scale), round((34 if success else 50)*scale)
        region = self.gdi.CreateRoundRectRgn(0, 0, notice_width+1, notice_height+1,
                                             round(12*scale), round(12*scale))
        if region and not self.user.SetWindowRgn(self.notice, region, True):
            self.gdi.DeleteObject(region)
        self.user.SetWindowTextW(self.notice_text, text)
        self.user.SetWindowPos(self.notice_text, None, 0, 0, notice_width, notice_height, 0x14)
        width, height = self.user.GetSystemMetrics(0), self.user.GetSystemMetrics(1)
        self.user.SetWindowPos(self.notice, w.HWND(-1), max(0, (width-notice_width)//2), round(height*.72),
                               notice_width, notice_height, 0x10)
        self.user.ShowWindow(self.notice, 4)

    def _create_notice(self):
        self.gdi = c.WinDLL('gdi32', use_last_error=True)
        for library, name, args, result in (
            (self.gdi, 'CreateSolidBrush', [w.DWORD], w.HBRUSH),
            (self.gdi, 'SetTextColor', [w.HDC, w.DWORD], w.DWORD),
            (self.gdi, 'SetBkColor', [w.HDC, w.DWORD], w.DWORD),
            (self.gdi, 'CreateFontIndirectW', [c.POINTER(LogFont)], w.HFONT),
            (self.gdi, 'DeleteObject', [w.HANDLE], w.BOOL),
            (self.gdi, 'CreateRoundRectRgn', [c.c_int]*6, w.HANDLE),
            (self.user, 'SetWindowRgn', [w.HWND, w.HANDLE, w.BOOL], c.c_int),
            (self.user, 'SetLayeredWindowAttributes', [w.HWND, w.DWORD, w.BYTE, w.DWORD], w.BOOL),
            (self.user, 'SendMessageW', [w.HWND, w.UINT, w.WPARAM, w.LPARAM], c.c_ssize_t),
            (self.user, 'SetWindowLongPtrW', [w.HWND, c.c_int, c.c_void_p], c.c_void_p),
            (self.user, 'CallWindowProcW', [c.c_void_p, w.HWND, w.UINT, w.WPARAM, w.LPARAM], c.c_ssize_t),
        ):
            fn = getattr(library, name)
            fn.argtypes, fn.restype = args, result
        self.notice_scale = self.user.GetDpiForSystem()/96
        self.notice_brush = self.gdi.CreateSolidBrush(0x2B2520)
        font = LogFont(height=-round(14*self.notice_scale), weight=400, charset=1,
                       quality=5, face='Microsoft YaHei UI')
        self.notice_font = self.gdi.CreateFontIndirectW(c.byref(font))
        self.notice = self.user.CreateWindowExW(0x080800A8, 'STATIC', 'LockLingo 提示', 0x80000000,
                                                0, 0, 1, 1, None, None, None, None)
        if not self.notice or not self.notice_font or not self.notice_brush:
            raise OSError('Clipboard notice unavailable')
        procedure = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        def paint(window, message, wp, lp):
            if message == 0x138:  # WM_CTLCOLORSTATIC: a single native label on a dark surface.
                self.gdi.SetTextColor(wp, 0xEFECE9)
                self.gdi.SetBkColor(wp, 0x2B2520)
                return self.notice_brush
            return self.user.CallWindowProcW(self.notice_procedure, window, message, wp, lp)
        self.notice_callback = procedure(paint)
        self.notice_procedure = self.user.SetWindowLongPtrW(self.notice, -4,
                                                           c.cast(self.notice_callback, c.c_void_p))
        self.notice_text = self.user.CreateWindowExW(0, 'STATIC', '', 0x50000201,
                                                     0, 0, 1, 1, self.notice, None, None, None)
        if not self.notice_text:
            raise OSError('Clipboard notice label unavailable')
        self.user.SendMessageW(self.notice_text, 0x30, self.notice_font, 1)
        self.user.SetLayeredWindowAttributes(self.notice, 0, 245, 2)

    def pump(self):
        message = w.MSG()
        while self.user.PeekMessageW(c.byref(message), None, 0, 0, 1):
            self.user.TranslateMessage(c.byref(message))
            self.user.DispatchMessageW(c.byref(message))

    def close(self):
        if self.notice:
            self.user.DestroyWindow(self.notice)
        for resource in (self.notice_font, self.notice_brush):
            if resource:
                self.gdi.DeleteObject(resource)
        if self.window:
            self.user.DestroyWindow(self.window)
        self.notice = self.window = None
        self.notice_font = self.notice_brush = None
