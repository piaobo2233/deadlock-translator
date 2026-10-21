"""The clipboard entry has its own English target; panel preferences stay intact."""
from app.translation.manual import ManualTranslator


class ClipboardTranslator(ManualTranslator):
    def _config(self):
        config = super()._config()
        config['send_target'] = 'en'
        return config
