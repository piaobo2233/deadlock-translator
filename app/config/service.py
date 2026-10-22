"""Extracted service responsibilities."""
from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.platform.logging import mask_key
import app.runtime.input_scope as input_scope
import os

def load_config() -> dict:
    """Read and validate settings, resolving credentials only for runtime use."""
    if input_scope._DIAGNOSTIC_ROOT:
        return ConfigRepository(input_scope.CONFIG_PATH).load(environment={})
    return ConfigRepository(input_scope.CONFIG_PATH).load()


CONFIG_TEMPLATE = default_config()


def ensure_config_template() -> bool:
    return ConfigRepository(input_scope.CONFIG_PATH).ensure_template()


def save_config(config: dict, ratios=None) -> bool:
    """Persist settings without copying environment credentials into the file."""
    saved = ConfigRepository(input_scope.CONFIG_PATH).save(config, ratios)
    if not saved:
        print("[warn] config.json 未保存，程序继续运行")
    return saved


def merge_disk_into_memory(config: dict, fresh: dict) -> list:
    """把「盘上重新读到的配置」回写进内存里那份 config，返回真正变化的键。

    [!] 2026-10-08 修（P0-4）。原来的链路是：
            watch_config() 里 fresh = load_config()      # 局部变量
            apply_live_config(fresh, ...)                 # 只同步白名单键
            # fresh 用完就丢 —— config（内存那份）永远不知道盘上变了
        后果：**不在热更白名单里的 16 个键永不生效**，而且退出时 on_exit
        会把内存里的旧值写回盘上，用户改的设置直接丢失。
        实测被咬过三次：改 dump_dir 两次、改 api_key 一次。

    回写口径与 save_config 保持一致：**跳过 `_` 开头的私有键与 api_key**
    （它们本来就不会被 save_config 落盘，回写它们没有意义，还可能把
    环境变量/兜底来源解析出来的值混进来）。
    """
    changed = []
    if not isinstance(fresh, dict) or not isinstance(config, dict):
        return changed
    for key, value in fresh.items():
        if not isinstance(key, str) or key.startswith("_") or key == "api_key":
            continue
        if config.get(key) != value:
            config[key] = value
            changed.append(key)
    return changed


def upgrade_config_file() -> list:
    """Only add missing fields; preserve all existing and unknown values."""
    added = ConfigRepository(input_scope.CONFIG_PATH).upgrade()
    if added:
        print(f"[info] config.json 已补齐 {len(added)} 个新字段（原有设置不变）")
    return added


def save_api_key(value: str) -> bool:
    """Explicit credential write; corrupted configuration is never overwritten."""
    if not ConfigRepository(input_scope.CONFIG_PATH).save_key(value):
        return False
    print(f"[info] API Key 已更新：{mask_key(value)}")
    return True


def config_stamp():
    """config.json 的 (mtime_ns, 大小)。用来判断「设置中心是不是改过配置」。"""
    try:
        st = os.stat(input_scope.CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def apply_live_config(config: dict, overlay=None, translator=None,
                      api_toggle: bool = True, *, report=None) -> list:
    """Supported UI/backend hot values; report actual session-bound outcomes."""
    from app.runtime.config_application import apply_config
    return apply_config(config, overlay, translator, api_toggle=api_toggle, report=report)
