"""A cancel-on-change input transaction; native operations live in win_input."""
import threading
import time


def parse_hotkey(value):
    parts = value.upper().replace(' ', '').split('+')
    modifiers = {'ALT': 1, 'CTRL': 2, 'SHIFT': 4}
    if len(parts) < 2 or len(set(parts)) != len(parts) or any(p not in modifiers for p in parts[:-1]):
        raise ValueError('快捷键格式示例：Ctrl+Alt+T')
    key = parts[-1]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key)
    elif key.startswith('F') and key[1:].isdigit() and 1 <= int(key[1:]) <= 11:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError('快捷键须使用字母、数字或 F1–F11')
    return sum(modifiers[p] for p in parts[:-1]), vk


class InputTransaction:
    def __init__(self, native, service, config, publish, *, clock=time.monotonic, allowed=lambda: True):
        self.native, self.service, self.config, self.publish = native, service, config, publish
        self.clock = clock
        self.allowed = allowed
        self._lock = threading.Lock()
        self.closed = False

    def run(self):
        if not self._lock.acquire(blocking=False):
            return
        original, translated, sent, token = '', '', False, None
        status = '未回填'
        try:
            if self.closed or not self.allowed():
                return
            token = self.native.begin()
            if token is None:
                return
            started = self.clock()
            config = dict(self.config())
            def current():
                latest = self.config()
                return (not self.closed and self.allowed() and self.clock() - started < 5.5 and self.native.current(token)
                        and all(latest.get(k) == config.get(k) for k in
                                ('api_key','endpoint','model','translate_api','send_target','auto_send')))
            if not current():
                status = '输入状态已改变，已取消'
                return
            original = self.native.read_input(token)
            if not original or not current():
                status = '未读到有效输入或焦点/输入已改变'
                return
            if self.publish(dict(original=original, translation='', status='正在翻译', sent=False)) is False:
                status = '原文恢复记录未保存，已停止自动输入'
                return
            result = self.service.translate(original, is_current=current)
            if not result.successful:
                status = result.reason or '翻译未完成，原文保留'
                return
            translated = result.text
            # Recopy immediately before replacement; edits, including undo, invalidate the token.
            if not current() or self.native.read_input(token) != original or not current():
                status = '焦点或输入已改变，已取消回填；原文可在面板恢复'
                return
            if not self.native.replace(token, translated, current):
                status = '回填未确认，未发送；请检查游戏输入，原文可在面板恢复'
                return
            status = '已回填，请在游戏中手动发送'
            if not current():
                status = '回填后状态已改变，未自动发送；请核对游戏输入'
            elif config.get('auto_send'):
                # Require a confirmed readback before sending, not just SendInput's return count.
                if self.native.read_input(token) == translated and current() and self.native.send(token, current):
                    sent = True
                    status = '已按自动发送设置发送'
                else:
                    status = '回填后状态已改变或无法确认，未自动发送'
        except Exception as exc:
            status = '自动输入已停止（' + type(exc).__name__ + '）；原文可在面板恢复'
        finally:
            try:
                try:
                    self.native.finish()
                except Exception:
                    status += '；剪贴板恢复未完成'
                if original or token is not None:
                    self.publish(dict(original=original, translation=translated, status=status, sent=sent))
            finally:
                self._lock.release()

    def close(self):
        self.closed = True
