"""Standalone recovery entry, frozen separately from the main program."""
import argparse
import ctypes
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.platform import rollback, update_install
from app.platform.updates import UpdateError


def guard(name):
    api = update_install.mutex_api()
    ctypes.set_last_error(0)
    handle = api.CreateMutexW(None, False, name)
    if not handle or ctypes.get_last_error() != 0:
        if handle:
            api.CloseHandle(handle)
        raise UpdateError('已有更新或恢复窗口正在运行，请先完成该操作')
    return api, handle


def log_error(root, exc):
    try:
        path = rollback.directory(root)/'recovery.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {type(exc).__name__}: {exc}\n')
    except OSError:
        pass


def window(root, confirmed=False, wait=False):
    import tkinter as tk
    from tkinter import messagebox
    if update_install.mutex_exists(update_install.UPDATE_MUTEX):
        raise UpdateError('正在更新程序，请等待更新完成后打开恢复窗口')
    api, handle = guard(rollback.RECOVERY_MUTEX)
    ui = tk.Tk(); ui.title('锁语 · 版本恢复'); ui.geometry('580x250')
    ui.configure(bg='#211f1b'); ui.resizable(False, False)
    state = tk.StringVar()
    events = queue.Queue(); busy = [False]
    tk.Label(ui, text='恢复更新前的版本', font=('Microsoft YaHei UI',18,'bold'),
             bg='#211f1b', fg='#59e3a5').pack(anchor='w',padx=24,pady=(22,12))
    tk.Label(ui, textvariable=state, wraplength=520, justify='left',
             font=('Microsoft YaHei UI',11), bg='#211f1b',fg='#eee6cf').pack(anchor='w',padx=24)
    actions = tk.Frame(ui,bg='#211f1b'); actions.pack(side='bottom',fill='x',padx=24,pady=24)
    def refresh():
        try:
            info = rollback.status(root)
            state.set(info['message']+'。个人数据保留当前内容。')
            button.configure(state='normal' if info['available'] else 'disabled')
        except Exception as exc:
            state.set('无法读取回退备份，请检查安装目录。'); log_error(root,exc)
            button.configure(state='disabled')
    def recover():
        if busy[0]: return
        if not confirmed and not messagebox.askyesno('确认回退',
                '将恢复本机更新前的程序，保留当前个人数据。\n回退后默认跳过刚退掉的版本。现在继续？',parent=ui):
            return
        busy[0]=True; button.configure(state='disabled')
        state.set('正在等待助手退出，然后校验并恢复程序…')
        def work():
            mutex_api, mutex = None, None
            try:
                mutex_api, mutex = guard(update_install.UPDATE_MUTEX)
                update_install.wait_for_exit([], timeout=120 if wait else 1)
                value = rollback.restore(root)
                events.put((True, '已恢复到 '+value+'。个人数据已保留。'))
            except Exception as exc:
                log_error(root,exc)
                events.put((False, str(exc) if isinstance(exc,UpdateError)
                            else '恢复未完成，备份已保留。请关闭占用程序后重试。'))
            finally:
                if mutex: mutex_api.CloseHandle(mutex)
        threading.Thread(target=work,daemon=True).start()
    def poll():
        try:
            ok, text = events.get_nowait()
            busy[0]=False; state.set(text)
            button.configure(state='disabled' if ok else 'normal')
            if ok: launch.configure(state='normal')
        except queue.Empty:
            pass
        ui.after(150,poll)
    def open_app():
        subprocess.Popen([str(root/'LockLingo.exe'),'--settings'],cwd=root,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        ui.destroy()
    button=tk.Button(actions,text='回退版本',command=recover,bg='#59e3a5',padx=20,pady=7)
    button.pack(side='left')
    launch=tk.Button(actions,text='打开锁语',command=open_app,state='disabled',padx=20,pady=7)
    launch.pack(side='left',padx=12)
    ui.protocol('WM_DELETE_WINDOW',lambda:None if busy[0] else ui.destroy())
    try:
        refresh(); poll()
        if confirmed: ui.after(100,recover)
        ui.mainloop()
    finally:
        api.CloseHandle(handle)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--prepare',metavar='VERSION')
    group.add_argument('--commit',action='store_true')
    group.add_argument('--abort',action='store_true')
    group.add_argument('--status',action='store_true')
    group.add_argument('--restore',action='store_true')
    group.add_argument('--apply-update',type=Path)
    parser.add_argument('--setup',type=Path)
    parser.add_argument('--update-active',action='store_true')
    parser.add_argument('--confirmed',action='store_true')
    parser.add_argument('--wait',action='store_true')
    args=parser.parse_args()
    if args.apply_update:
        return update_install.run(args.apply_update)
    root=(args.root or Path(sys.executable).parent.parent).absolute()
    try:
        if args.prepare or args.commit or args.abort:
            update_install.wait_for_exit([],timeout=1)
        if args.prepare:
            if args.update_active and (not args.setup or not rollback.active_update(root,args.setup)):
                raise UpdateError('另一个更新正在进行，请等待其结束')
            rollback.prepare(root,args.prepare)
        elif args.commit: rollback.commit(root)
        elif args.abort: rollback.abort(root)
        elif args.status: print(json.dumps(rollback.status(root),ensure_ascii=False))
        elif args.restore:
            if not args.confirmed:
                raise UpdateError('请通过恢复窗口确认回退')
            api,handle=guard(rollback.RECOVERY_MUTEX)
            update_api,update_handle=None,None
            try:
                update_api,update_handle=guard(update_install.UPDATE_MUTEX)
                update_install.wait_for_exit([],timeout=120 if args.wait else 1)
                rollback.restore(root)
            finally:
                if update_handle:update_api.CloseHandle(update_handle)
                api.CloseHandle(handle)
        else: window(root,args.confirmed,args.wait)
        return 0
    except Exception as exc:
        log_error(root,exc)
        if not (args.prepare or args.commit or args.abort or args.status or args.restore):
            ctypes.windll.user32.MessageBoxW(None,str(exc),'锁语 · 版本恢复',0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
