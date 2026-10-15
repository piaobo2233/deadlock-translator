"""Explicit configuration IO; imports never touch a real user's files or credentials."""

import os
from copy import deepcopy

from app.config.defaults import default_config, RETIRED_UI_KEYS
from app.config.validation import normalize_config
from app.config.credentials import is_key_placeholder
from app.storage import store
from app.storage.locking import config_lock


class RuntimeConfig(dict):
    """A dict-compatible runtime view that remembers which fields were edited."""

    def __init__(self, values, *, key_source="none"):
        super().__init__(values)
        self.key_source = key_source
        self.mark_saved()

    def mark_saved(self):
        self.baseline = deepcopy({k: v for k, v in self.items()
                                  if not k.startswith("_") and k != "api_key"})


class ConfigRepository:
    def __init__(self, path, *, log=print):
        self.path = os.fspath(path)
        self.log = log

    def load(self, *, environment=None):
        raw = store.read_json(self.path, None)
        if not isinstance(raw, dict):
            if os.path.exists(self.path):
                self.log("[warn] config.json 无法读取，使用默认设置；原文件保持不动。")
            raw = {}
        config, invalid = normalize_config(raw)
        if invalid:
            # Report field names only: invalid values may themselves be credentials.
            self.log("[warn] 配置字段无效，已使用安全值：" + ", ".join(invalid))
        env = os.environ if environment is None else environment
        env_key = str(env.get("DEEPSEEK_API_KEY") or "").strip()
        source = "config" if config["api_key"] else "none"
        if env_key:
            config["api_key"] = env_key
            source = "environment"
        return RuntimeConfig(config, key_source=source)

    def _change(self, update, *, create=False):
        try:
            with config_lock(self.path):
                exists = os.path.exists(self.path)
                if create and exists:
                    return False
                if not store._guard_overwrite(self.path, None):
                    return False
                raw = store.read_json(self.path, None) if exists else {}
                if not isinstance(raw, dict):
                    self.log("[warn] 配置不是 JSON 对象，拒绝覆盖。")
                    return False
                if not update(raw):
                    return False
                return store.atomic_write_json(self.path, raw)
        except OSError as exc:
            self.log(f"[warn] 配置未保存：{type(exc).__name__}")
            return False

    def ensure_template(self):
        def update(raw):
            raw.update(default_config())
            return True
        return self._change(update, create=True)

    def save(self, config, ratios=None):
        baseline = config.baseline if isinstance(config, RuntimeConfig) else {}
        changes = {key: value for key, value in config.items()
                   if isinstance(key, str) and not key.startswith("_") and key != "api_key"
                   and key not in RETIRED_UI_KEYS and (key not in baseline or value != baseline[key])}

        def update(raw):
            raw.update(changes)
            return True
        saved = self._change(update)
        if saved and isinstance(config, RuntimeConfig):
            config.mark_saved()
        return saved

    def save_key(self, value):
        if is_key_placeholder(value):
            self.log("[warn] API Key 未保存：不能使用掩码或占位文字。")
            return False
        def update(raw):
            raw["api_key"] = str(value or "").strip()
            return True
        return self._change(update)

    def upgrade(self):
        if not os.path.exists(self.path):
            return []
        added = []

        def update(raw):
            for key, value in default_config().items():
                if key not in raw:
                    raw[key] = value
                    added.append(key)
            return bool(added)
        return added if self._change(update) else []
