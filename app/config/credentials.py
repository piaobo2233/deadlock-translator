"""Credential input safety; never infer validity from a provider-specific format."""


def is_key_placeholder(value):
    text = str(value or "").strip()
    return "****" in text or "•" in text or text in {"（未配置）", "未配置"}
