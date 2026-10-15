"""Validate runtime values without changing the source file or dropping unknown keys."""

import math

from app.config.defaults import default_config
from app.config.specification import BY_KEY

MONEY_KEYS = frozenset(("price_in", "price_out", "budget_session", "budget_daily"))


def _value(key, value, spec):
    kind = spec["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError("Expected a boolean")
    if kind in ("int", "float"):
        if isinstance(value, bool):
            raise ValueError("Boolean is not a number")
        number = float(value)
        if not math.isfinite(number) or number < spec["lo"]:
            raise ValueError("Invalid number")
        # UI slider maxima are not billing limits; custom prices remain valid.
        if key not in MONEY_KEYS and number > spec["hi"]:
            raise ValueError("Out of range")
        if kind == "int" and not number.is_integer():
            raise ValueError("Expected an integer")
        return int(number) if kind == "int" else number
    if key == "schema_version":
        if type(value) is not int or value < 1:
            raise ValueError("Invalid version")
        return value
    if not isinstance(value, str):
        raise ValueError("Expected text")
    if key == 'close_action' and value not in {'exit','tray'}:
        raise ValueError('Invalid close preference')
    if key in ("model", "endpoint") and not value.strip():
        raise ValueError("Empty model/endpoint")
    # Arbitrary nonempty model names are intentional (custom OpenAI-compatible APIs).
    return value


def normalize_config(raw):
    result = default_config()
    result.update(raw)
    if 'realtime_learn' in result:
        result['realtime_learn'] = False  # Old enabled value cannot trigger paid work.
    invalid = []
    for key, spec in BY_KEY.items():
        try:
            result[key] = _value(key, result[key], spec)
        except (ValueError, TypeError, OverflowError):
            result[key] = default_config()[key]
            invalid.append(key)
    if MONEY_KEYS.intersection(invalid):
        # An invalid budget must not silently turn into unlimited paid API calls.
        result["translate_api"] = False
        result['realtime_learn'] = False
    return result, invalid
