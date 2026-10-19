"""Read-only glossary projection; does not initialize desktop or migrate data."""

from app.storage import store
from app.translation.builtins import DEFAULT_GLOSSARY


def merge_glossary(entries, *, builtins_enabled=True):
    merged = dict(DEFAULT_GLOSSARY) if builtins_enabled else {}
    user = store.glossary_flat(entries)
    if not builtins_enabled:
        user = {key: value for key, value in user.items() if entries.get(key, {}).get('source') != 'builtin'}
    merged.update({str(key).strip().lower(): str(value) for key, value in user.items() if value})
    for name in store.glossary_inactive(entries):
        merged.pop(name, None)
    return merged
