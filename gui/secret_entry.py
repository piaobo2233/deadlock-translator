"""Bind a blank credential draft without ever putting a saved key in the widget."""

from app.config.credentials import is_key_placeholder

SOURCE_LABELS = {
    "none": "未配置密钥",
    "config": "使用已保存密钥",
    "environment": "环境变量优先",
}


def bind_secret_entry(entry, variable, save, *, source="none", hint=None, invalid=lambda: None,
                      autosave=True):
    if autosave:
        variable.set("")
    entry.configure(show="•")
    if hint is not None:
        hint.configure(text=SOURCE_LABELS.get(source, SOURCE_LABELS["none"]))

    def commit(_event=None):
        value = variable.get().strip()
        # Focus changes, navigation and an untouched/empty draft must not clear
        # the persisted credential. Deletion requires a separate explicit action.
        if not value:
            return
        if is_key_placeholder(value):
            invalid()
            return
        if save(value):
            variable.set("")
            if hint is not None:
                hint.configure(text=SOURCE_LABELS["environment" if source == "environment" else "config"])

    if autosave:
        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
    return commit
