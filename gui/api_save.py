"""翻译接口草稿的唯一落盘路径。

[!] 首次引导页和「翻译接口」页共用这里的实现：两套保存代码迟早会分叉，
    然后出现「引导里能存、接口页存不上」或反过来。校验、密钥写入、写后
    核对和部分失败提示都只在这里维护一次；调用方负责自己的控件状态。
"""

from app.config.credentials import is_key_placeholder
from app.storage import store


def persist_api_draft(app, *, provider_key: str, model: str, endpoint: str, key: str):
    """校验并保存服务商/模型/地址/密钥；返回 (是否成功, 错误说明)。

    说明为空表示四个字段都已写盘并回读核对一致。失败时**不清理**致密草稿，
    由调用方保留控件内容让用户重试。
    """
    from gui import pages as p
    from app.config.validation import _value
    value = str(key or "").strip()
    if value and is_key_placeholder(value):
        app.set_status("保存失败：不能保存掩码；草稿保留", p.COLORS["err"])
        return False, "不能保存掩码"
    repo = p._config_repository(app.root_dir)
    patch = dict(model=str(model).strip(), endpoint=str(endpoint).strip(),
                 provider=provider_key)
    try:
        for name, setting in patch.items():
            _value(name, setting, p.settings_spec.BY_KEY[name])
        if value and not repo.save_key(value):
            raise OSError("密钥写入失败")
        if not repo.save(patch):
            raise OSError("模型写入失败")
        disk = store.read_json(repo.path, None)
        if (not isinstance(disk, dict)
                or any(disk.get(name) != setting for name, setting in patch.items())
                or (value and disk.get("api_key") != value)):
            raise OSError("写后核对不一致")
    except (OSError, ValueError, TypeError) as exc:
        app.set_status(f"保存失败：{exc}；草稿保留，可重试（部分字段可能已保存）",
                       p.COLORS["err"])
        return False, str(exc)
    return True, ""
