"""Assembly of native hotkeys and existing translation services."""
import threading

from app.platform.game_input import InputTransaction
from app.platform.win_input import WindowsInput
from app.translation.manual import ManualTranslator


class GameControls:
    def __init__(self, root, load_config, gate, session, report, *, paused=lambda: False):
        self.load_config, self.gate, self.session, self.report, self.paused = load_config, gate, session, report, paused
        config = load_config()
        self.native = WindowsInput(dict(send=config['send_hotkey'], recognize=config['recognize_hotkey']),
                                   self.dispatch, report)
        def publish(value):
            saved = session.input(value)
            if not saved:
                report('本次输入恢复记录未保存；请勿关闭助手，可在剪贴板核对原文')
            if value['status'] != '正在翻译':
                report(value['status'])
            return saved
        self.transaction = InputTransaction(self.native, ManualTranslator(root, load_config),
                                            load_config, publish, allowed=lambda: not self.paused())

    def dispatch(self, action):
        if self.paused() or not self.native.target():
            return
        if action == 'recognize':
            self.gate.request()
        elif action == 'send':
            threading.Thread(target=self.transaction.run, name='dl-game-input', daemon=True).start()

    def start(self):
        return self.native.start()

    def close(self):
        self.transaction.close()
        self.native.close()
