"""One main Tk panel per program root, with transient local commands only.

The named mutex lives as long as its owner handle. File messages run on the
Tk thread; no network server, registry, config keys or fee state are involved.
"""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import time
import uuid

from app.storage import store

POLL_MS = 150
MAX_COMMANDS = 128


def scope(root):
    root = Path(root).resolve()
    directory = root / '.temp' / 'panel-instance'
    if directory.resolve() != directory:
        raise ValueError('Panel control directory is redirected')
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def mutex_name(root):
    key = str(Path(root).resolve()).casefold().encode('utf-8')
    return 'Local\\DeadlockPanel_' + hashlib.sha256(key).hexdigest()


def kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    return api


def process_info(pid=None):
    pid = os.getpid() if pid is None else int(pid)
    api = kernel()
    handle = api.OpenProcess(0x00101000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if api.WaitForSingleObject(handle, 0) != 258 or not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return dict(pid=pid, born=(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def alive(info):
    if not isinstance(info, dict) or type(info.get('pid')) is not int or info['pid'] <= 0 or type(info.get('born')) is not int:
        return False
    return process_info(info['pid']) == {k: info[k] for k in ('pid', 'born')}


class PanelLease:
    def __init__(self, root):
        self.api = kernel()
        ctypes.set_last_error(0)
        self.handle = self.api.CreateMutexW(None, False, mutex_name(root))
        error = ctypes.get_last_error()
        self.first = error == 0
        if not self.handle:
            raise OSError('Cannot establish panel instance guard')
        if error not in {0,183}:
            self.close()
            raise OSError('Panel instance guard result is uncertain')
        if not self.first:
            self.close()  # A probe must never retain the owner's mutex.

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def owner(root):
    value = store.read_json(str(scope(root) / 'translator.json'), {})
    return value if isinstance(value, dict) else {}


def submit(root, action='navigate', *, page='home', translator=None):
    """Return True only after a command is safely queued for a live panel."""
    directory = scope(root)
    state = store.read_json(str(directory / 'state.json'), {})
    if not isinstance(state, dict) or not alive(state.get('process')):
        return False
    token = state.get('token', '')
    if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        return False
    mailbox = directory / token
    if mailbox.resolve() != mailbox or not mailbox.is_dir():
        return False
    if len(list(mailbox.glob('request-*.json'))) >= MAX_COMMANDS:
        return False
    command = dict(version=1, token=token, action=action, page=page,
                   translator=translator, at=time.time_ns())
    path = mailbox / ('request-' + uuid.uuid4().hex + '.json')
    return bool(store.atomic_write_json(str(path), command))


def begin_translator(root):
    record = dict(version=1, process=process_info(), token=uuid.uuid4().hex, stopped=False)
    if not record['process'] or not store.atomic_write_json(str(scope(root) / 'translator.json'), record):
        raise OSError('Cannot publish translator window ownership')
    submit(root, 'attach', translator=record)
    return record


def end_translator(root, record):
    if not record:
        return
    current = owner(root)
    if current.get('token') != record['token']:
        return  # An old instance cannot close a newer instance's panel.
    current['stopped'] = True
    store.atomic_write_json(str(scope(root) / 'translator.json'), current)
    submit(root, 'close', translator=current)

def request_exit(root, *, action='exit'):
    """One session-bound exit intent, handled by the existing assistant Tk loop."""
    if action not in {'exit','retry_review','pause','resume'}:return False
    current=owner(root)
    token=current.get('token','')
    if not isinstance(token,str) or len(token)!=32 or any(c not in '0123456789abcdef' for c in token):return False
    if current.get('stopped') or not alive(current.get('process')):return False
    return store.atomic_write_json(str(scope(root)/('exit-'+token+'.json')),
        dict(version=1,action=action,token=token,process=current['process'],requester=process_info(),at=time.time()))

def consume_action(root,record):
    if not record or owner(root).get('token')!=record.get('token'):return ''
    path=scope(root)/('exit-'+record['token']+'.json')
    if not path.is_file():return ''
    value=store.read_json(str(path),{})
    path.unlink(missing_ok=True)
    valid=(isinstance(value,dict) and value.get('version')==1 and value.get('token')==record['token']
        and value.get('process')==record['process'] and alive(value.get('requester'))
        and type(value.get('at')) in (int,float) and 0<=time.time()-value['at']<10)
    action=value.get('action','exit') if valid else ''
    return action if action in {'exit','retry_review','pause','resume'} else ''

def consume_exit(root,record):
    return consume_action(root,record)=='exit'


class PanelCoordinator:
    def __init__(self, root, *, parent_pid=0):
        self.root_dir = root
        self.directory = scope(root)
        self.lease = PanelLease(root)
        self.app = None
        self.translator = None
        self.parent = process_info(parent_pid) if parent_pid else None
        if self.lease.first:
            self.token = uuid.uuid4().hex
            self.mailbox = self.directory / self.token
            self.mailbox.mkdir()
            self.process = process_info()
            if not self.process or not self.publish('starting'):
                self.close()
                raise OSError('Cannot publish main panel instance')

    def publish(self, status='ready'):
        state = dict(version=1, token=self.token, process=self.process, status=status,
                     page=self.app.current if self.app else '',
                     translator_token=(self.translator or {}).get('token', ''))
        return store.atomic_write_json(str(self.directory / 'state.json'), state)

    def forward(self, page, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if submit(self.root_dir, page=page):
                return True
            # If startup failed or the owner died, caller may acquire afresh.
            probe = PanelLease(self.root_dir)
            first = probe.first
            probe.close()
            if first:
                return False
            time.sleep(.05)
        return False

    def bind(self, app):
        self.app = app
        app._coordinator = self
        self.publish()
        app.window_callbacks.schedule(POLL_MS, self.poll)

    def _attach(self, record):
        current = owner(self.root_dir)
        if isinstance(record, dict) and record.get('token') == current.get('token'):
            self.translator = current
            self.parent = None

    def poll(self):
        try:
            self._poll_once()
        except Exception as exc:
            print(f'[warn] 面板协调异常：{type(exc).__name__}')
            if self.app and self.app.root.winfo_exists():
                self.app.set_status('面板请求处理异常，请重试')
        finally:
            if self.app and self.app.root.winfo_exists():
                self.app.window_callbacks.schedule(POLL_MS, self.poll)

    def _poll_once(self):
        app = self.app
        if not app or not app.root.winfo_exists():
            return
        current = owner(self.root_dir)
        if current.get('token') and not current.get('stopped') and alive(current.get('process')):
            self._attach(current)  # Independent panel associates after translator startup.
        files = list(self.mailbox.glob('request-*.json'))[:MAX_COMMANDS]
        commands = []
        for path in files:
            value = store.read_json(str(path), {})
            if isinstance(value, dict) and value.get('version') == 1 and value.get('token') == self.token:
                stamp = value.get('at', 0)
                if type(stamp) is int:
                    commands.append((stamp, value))
            path.unlink(missing_ok=True)
        for _, command in sorted(commands, key=lambda item: item[0]):
            action = command.get('action')
            if action == 'attach':
                self._attach(command.get('translator'))
            elif action == 'close':
                token = (command.get('translator') or {}).get('token')
                if self.translator and token == self.translator['token']:
                    app.root.destroy()
                    return
            elif action == 'navigate':
                app.open_page(command.get('page', ''))
                app.bring_to_front()
        if (self.translator and (self.translator.get('stopped') or
                owner(self.root_dir).get('token') == self.translator['token'] and owner(self.root_dir).get('stopped')
                or not alive(self.translator.get('process')))) or (self.parent and not alive(self.parent)):
            app.root.destroy()
            return
        self.publish()

    def close(self):
        if self.lease.first and getattr(self, 'token', None):
            state = store.read_json(str(self.directory / 'state.json'), {})
            if isinstance(state, dict) and state.get('token') == self.token:
                (self.directory / 'state.json').unlink(missing_ok=True)
        self.lease.close()
        self.app = None
