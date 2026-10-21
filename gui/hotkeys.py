"""Focused shortcut recording with an explicit save and no editable key strings."""
import os

from app.platform.game_input import parse_hotkey
from gui.appearance import panel, text
from gui.theme import COLORS as C

MODIFIERS = {16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165}
KEYS = (27, *range(48, 58), *range(65, 91), *range(112, 124))
# Windows Tk maps Mod1 (0x8) to Num Lock and Mod2 (0x10) to Alt.
ALT_MASK = (0x10 | 0x20000) if os.name == 'nt' else 0x8


def combination(key, *, ctrl=False, alt=False, shift=False, win=False):
    if win:
        raise ValueError('请使用 Ctrl、Alt 或 Shift，不支持 Windows 键')
    name = chr(key) if 48 <= key <= 57 or 65 <= key <= 90 else (
        'F'+str(key-111) if 112 <= key <= 122 else '')
    if not name:
        raise ValueError('主键请选择字母、数字或 F1–F11')
    value = '+'.join([name for name, down in (('Ctrl',ctrl),('Alt',alt),('Shift',shift)) if down]+[name])
    if not any((ctrl,alt,shift)):
        raise ValueError('请同时按住 Ctrl、Alt 或 Shift，再按主键')
    parse_hotkey(value)
    return value


class HotkeyControl:
    def __init__(self, parent, app, key, value):
        self.app, self.key, self.value = app, key, value
        self.dialog = None
        self.label = text(parent,app.th,value)
        self.label.pack(side='left',padx=(0,12))
        self.button = app.th.button(parent,'修改',self.edit)
        self.button.pack(side='left')
        parent.bind('<Destroy>',self.destroy,add='+')
        self.parent = parent

    def destroy(self, event):
        if event.widget is self.parent and self.dialog is not None:
            self.dialog.close()

    def edit(self):
        if self.dialog is not None:
            self.dialog.window.lift();self.dialog.window.focus_set();return
        from gui import pages as p
        self.value=p._load_config(self.app.root_dir)[self.key]
        self.label.configure(text=self.value)
        self.dialog = Recorder(self)


class Recorder:
    def __init__(self, control):
        self.control, self.app = control, control.app
        th = self.app.th
        self.window = win = th.tk.Toplevel(self.app.root)
        win.title('修改快捷键')
        win.configure(bg=C['panel'])
        win.transient(self.app.root)
        win.resizable(False,False)
        self.recording = False
        self.candidate = None
        self.pending = None
        self.pressed = set()
        self.previous = set()
        self.get_state = None
        if os.name == 'nt':
            import ctypes
            self.get_state = ctypes.WinDLL('user32').GetAsyncKeyState
            self.get_state.argtypes = [ctypes.c_int]
            self.get_state.restype = ctypes.c_short
        body = panel(win,th);body.pack(fill='both',expand=True,padx=24,pady=20)
        text(body,th,'当前快捷键：'+control.value).pack(anchor='w')
        self.preview = text(body,th,'')
        self.preview.configure(font=th.font_heading)
        self.preview.pack(anchor='w',pady=(16,8))
        self.hint = text(body,th,'',small=True,muted=True,wraplength=420,justify='left')
        self.hint.pack(anchor='w',pady=(0,16))
        actions=panel(body,th);actions.pack(fill='x')
        self.retry=th.button(actions,'重新录制',self.start);self.retry.pack(side='left')
        self.save_button=th.button(actions,'确认保存',self.save,kind='primary');self.save_button.pack(side='left',padx=10)
        self.cancel=th.button(actions,'取消',self.close);self.cancel.pack(side='left')
        win.bind('<KeyPress>',self.keypress)
        win.bind('<KeyRelease>',self.keyrelease)
        win.protocol('WM_DELETE_WINDOW',self.close)
        win.bind('<Destroy>',self.destroy,add='+')
        win.grab_set()
        win.focus_set()
        self.start()

    def down(self, key):
        return bool(self.get_state and self.get_state(key)&0x8000)

    def start(self):
        self.recording=True;self.candidate=None;self.pressed.clear()
        self.preview.configure(text='请直接按组合键…')
        self.hint.configure(text='推荐 Ctrl 组合；也可使用 Alt 或 Shift，配合字母、数字或 F1–F11。Esc 取消。',fg=C['muted'])
        self.save_button.configure(state='disabled')
        self.window.focus_set()
        self.previous={key for key in KEYS if self.down(key)}
        if self.get_state and self.pending is None:self.pending=self.window.after(20,self.poll)

    def poll(self):
        self.pending=None
        if not self.recording:return
        down={key for key in KEYS if self.down(key)}
        focused=self.window.focus_displayof()
        # RegisterHotKey can consume the Tk keypress. Read only while this dialog
        # owns focus; held keys from another application never become a recording.
        if focused is not None and focused.winfo_toplevel() is self.window:
            for key in sorted(down-self.previous):
                if key==27:self.close();return
                self.capture(key,ctrl=self.down(17),alt=self.down(18),shift=self.down(16),
                             win=self.down(91) or self.down(92))
                if not self.recording:return
        self.previous=down
        self.pending=self.window.after(20,self.poll)

    def keypress(self, event):
        if event.keysym=='Escape':self.close();return 'break'
        if not self.recording:return
        key=event.keycode
        if key in MODIFIERS:self.pressed.add(key)
        else:
            self.capture(key,ctrl=bool(event.state&4),alt=bool(event.state&ALT_MASK),
                         shift=bool(event.state&1),win=bool(self.pressed&{91,92}))
        return 'break'

    def keyrelease(self, event):
        self.pressed.discard(event.keycode)
        if self.recording:return 'break'

    def capture(self, key, **modifiers):
        if not self.recording:return
        try:value=combination(key,**modifiers)
        except ValueError as exc:
            self.hint.configure(text=str(exc)+'；Esc 取消。',fg=C['warn']);return
        self.candidate=value;self.recording=False
        self.preview.configure(text=value.replace('+',' + '))
        self.hint.configure(text='检查组合键，确认保存后重开助手生效。',fg=C['muted'])
        self.save_button.configure(state='normal')

    def save(self):
        if self.candidate is None:return
        from gui import pages as p
        other='recognize_hotkey' if self.control.key=='send_hotkey' else 'send_hotkey'
        if parse_hotkey(self.candidate)==parse_hotkey(p._load_config(self.app.root_dir)[other]):
            self.hint.configure(text='两个快捷键不能相同，请重新录制。',fg=C['err']);return
        if not p._save(self.control.key,self.candidate,self.app):
            self.hint.configure(text='保存失败，原快捷键保留；可以再次确认保存。',fg=C['err']);return
        self.control.value=self.candidate
        self.control.label.configure(text=self.candidate)
        self.app.set_status('快捷键已保存，重开助手后生效',C['ok'])
        self.close()

    def destroy(self, event):
        if event.widget is self.window:
            self.recording=False
            if self.pending is not None:self.window.after_cancel(self.pending);self.pending=None
            self.control.dialog=None

    def close(self):
        if self.window.winfo_exists():
            self.window.grab_release()
            self.window.destroy()
