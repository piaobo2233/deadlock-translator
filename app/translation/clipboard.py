"""Confirmed outgoing language, with English compatibility for old preferences."""
from app.translation.manual import ManualTranslator


def effective_target(config):
    from app.translation.directions import LANGUAGES
    target = config.get('send_target', 'en')
    return target if config.get('send_target_confirmed') is True and target in LANGUAGES else 'en'


class ClipboardTranslator(ManualTranslator):
    def _config(self):
        config = super()._config()
        config['send_target'] = effective_target(config)
        return config
