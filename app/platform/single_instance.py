"""Windows instance guard: uncertainty never authorizes a translation worker."""

import ctypes
from ctypes import wintypes

MUTEX_NAME = "DeadlockTranslate_SingleInstance"
ERROR_ALREADY_EXISTS = 183


def _create_mutex():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    return kernel, handle, ctypes.get_last_error()


def acquire_instance():
    """Return (handle, first); (None, False) means guard failure, not duplicate.

    The entry retains a successful handle until process exit. Only the current
    process's handle is owned here; never release another running worker's lock.
    """
    try:
        kernel, handle, error = _create_mutex()
        if not handle:
            return None, False
        if error not in (0, ERROR_ALREADY_EXISTS):
            kernel.CloseHandle(handle)
            return None, False
        return handle, error == 0
    except Exception:
        return None, False


def instance_running():
    """Legacy status probe; release its temporary handle in both outcomes."""
    try:
        kernel, handle, error = _create_mutex()
        if not handle:
            return False
        try:
            return error == ERROR_ALREADY_EXISTS
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return False
