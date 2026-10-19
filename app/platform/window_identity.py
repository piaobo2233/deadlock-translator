"""Give Python-hosted windows their own Windows shell identity and icon."""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import uuid


class PropertyKey(ctypes.Structure):
    _fields_ = [('fmtid', ctypes.c_ubyte * 16), ('pid', wintypes.DWORD)]


class VariantValue(ctypes.Union):
    _fields_ = [('text', ctypes.c_wchar_p), ('storage', ctypes.c_void_p * 2)]


class PropVariant(ctypes.Structure):
    _fields_ = [('vt', wintypes.WORD), ('reserved', wintypes.WORD * 3),
                ('value', VariantValue)]


def property_key(pid):
    return PropertyKey((ctypes.c_ubyte * 16).from_buffer_copy(
        uuid.UUID('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3').bytes_le), pid)


@contextmanager
def property_store(window):
    user32 = ctypes.WinDLL('user32')
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.GetAncestor(window.winfo_id(), 2)
    iid = (ctypes.c_ubyte * 16).from_buffer_copy(
        uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
    store = ctypes.c_void_p()
    get = ctypes.OleDLL('shell32').SHGetPropertyStoreForWindow
    get.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get.restype = ctypes.HRESULT
    get(hwnd, ctypes.byref(iid), ctypes.byref(store))
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    try:
        yield store, table
    finally:
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(table[2])(store)


def apply(window, app_id, title, icon):
    if os.name != 'nt':
        return False
    from app.paths import application_root
    exe = Path(sys.executable)
    command = [str(exe)]
    if not getattr(sys, 'frozen', False):
        pythonw = exe.with_name('pythonw.exe')
        command = [str(pythonw if pythonw.is_file() else exe),
                   str(Path(application_root()) / 'scripts/run.py')]
    # Windows refreshes grouping on ID assignment: publish launch details first.
    values = [(2, subprocess.list2cmdline(command)), (3, icon + ',0'),
              (4, title), (5, app_id)]
    with property_store(window) as (store, table):
        set_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(table[6])
        for pid, text in values:
            value = PropVariant(vt=31, value=VariantValue(text=text))
            if set_value(store, ctypes.byref(property_key(pid)), ctypes.byref(value)) < 0:
                return False
        commit = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(table[7])
        return commit(store) >= 0
