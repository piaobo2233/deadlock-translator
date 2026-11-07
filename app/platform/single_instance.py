"""Windows instance guard: uncertainty never authorizes a translation worker."""

import ctypes
from ctypes import wintypes

MUTEX_NAME = "DeadlockTranslate_SingleInstance"
INSTALL_MUTEX_NAME = "Local\\LockLingo_InstallGuard_v2"
LEGACY_INSTALL_MUTEX_NAME = "DeadlockTranslate_InstallGuard"
_install_handles = ()
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000


def retain_install_guard():
    """冻结程序的每个进程均阻止安装覆盖，独立面板/OCR 辅助进程也适用。"""
    global _install_handles
    if not _install_handles:
        _, handle, error = _presence_mutex(INSTALL_MUTEX_NAME)
        if not handle:
            raise ctypes.WinError(error)
        # Older installers still check the legacy name. A restricted legacy
        # marker may already exist. The new marker protects current installers.
        _, legacy, _ = _presence_mutex(LEGACY_INSTALL_MUTEX_NAME)
        _install_handles = (handle, legacy)
    # Windows 在进程结束时释放句柄；多个进程均可持有，不影响原有单实例协议。


def _presence_mutex(name):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexExW.restype = wintypes.HANDLE
    kernel.CreateMutexExW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    # Keeping a presence marker needs no ownership or ACL modification.
    handle = kernel.CreateMutexExW(None, name, 0, SYNCHRONIZE)
    return kernel, handle, ctypes.get_last_error()


def _create_mutex():
    return _presence_mutex(MUTEX_NAME)


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
