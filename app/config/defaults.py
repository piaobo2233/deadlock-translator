"""The only source of persistent defaults (no environment or filesystem reads)."""

from copy import deepcopy

CONFIG_SCHEMA = 1
RETIRED_UI_KEYS = frozenset(("hold_key", "position", "auto_settings", "show_original", "dev_key_fallback", "realtime_learn"))

DEFAULT_CONFIG = {
    "api_key": "",
    "endpoint": "https://api.deepseek.com/chat/completions",
    "model": "deepseek-flash",
    "max_messages": 6,
    "fade_seconds": 10,
    "fade_extension": 7,
    "font_size": 15,
    "width": 300,
    "ocr_interval": 0.25,
    "ocr_votes": 3,
    "only_in_game": True,
    "tesseract": "",
    "chatlog_path": "",
    "dump_dir": "",
    "translate_api": False,
    "close_action": "exit",
    "builtins_enabled": True,
    "schema_version": CONFIG_SCHEMA,
    # Zero means not configured / no limit, never a guessed provider price.
    "price_in": 0.0,
    "price_out": 0.0,
    "budget_session": 0.0,
    "budget_daily": 0.0,
}


def default_config():
    return deepcopy(DEFAULT_CONFIG)
