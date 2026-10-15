# -*- coding: utf-8 -*-
"""可复用的界面零件：数据表、输入对话框、导入导出。

[!] 为什么数据表要自己收口一层：
    ttk.Treeview 本身能用，但深色样式在「有 ttkbootstrap」和「没有」两条路径下
    长得完全不一样，中文列宽也得自己调。上层（词库 / 记忆 / 待审核）只管
    给数据和回调，不该关心这些。
"""

import csv
import json
import os

from gui.theme import COLORS, GAP, PAD

__all__ = ["DataTable", "ask_text", "ask_choice", "confirm", "info",
           "read_import", "write_export", "timestamp"]


def timestamp() -> str:
    import time
    return time.strftime("%Y%m%d_%H%M%S")


class DataTable:
    """深色数据表：搜索 + 列排序 + 选择 + 滚动条。

    `columns` 是 [{"key", "title", "width", "anchor"}]；
    `rows` 是 [dict]，每行**必须**带 `"__key"`（唯一键），
    操作按钮靠它定位到具体条目。
    """

    def __init__(self, parent, theme, columns, on_select=None, on_double=None,
                 height=18):
        self.th = theme
        self.c = COLORS
        self.columns = list(columns)
        self.on_select = on_select
        self.on_double = on_double
        self.rows = []
        self._sort_key = None
        self._sort_desc = False

        outer = theme.tk.Frame(parent, bg=self.c["card"], highlightthickness=1,
                               highlightbackground=self.c["border"])
        outer.pack(fill="both", expand=True)

        self.frame = theme.tk.Frame(outer, bg=self.c["card"])
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        ttk = theme.ttk
        keys = [col["key"] for col in self.columns]
        self.tree = ttk.Treeview(self.frame, columns=keys, show="headings",
                                 height=height, selectmode="extended")
        for col in self.columns:
            self.tree.heading(col["key"], text=col["title"],
                              command=lambda k=col["key"]: self.sort_by(k))
            self.tree.column(col["key"], width=col.get("width", 120),
                             anchor=col.get("anchor", "w"),
                             stretch=col.get("stretch", False))
        bar = ttk.Scrollbar(self.frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set, style="Treeview")
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", self._double)
        self.tree.bind("<Button-3>", self._popup)

        self.menu = None
        self._popup_actions = []

    # ---- 数据 ----
    def set_rows(self, rows):
        self.rows = list(rows)
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        rows = self.rows
        if self._sort_key:
            rows = sorted(rows, key=lambda r: self._sort_value(r, self._sort_key),
                          reverse=self._sort_desc)
        for row in rows:
            values = [row.get(col["key"], "") for col in self.columns]
            iid = str(row.get("__key", "")) or f"r{id(row)}"
            # Treeview 的 iid 不能重复；重复时加后缀（理论上有重复键，防御一下）
            while self.tree.exists(iid):
                iid += "*"
            self.tree.insert("", "end", iid=iid, values=values,
                             tags=(row.get("__tag", ""),))
        self.tree.tag_configure("flagged", foreground=self.c["muted"])
        self.tree.tag_configure("bad", foreground=self.c["err"])

    @staticmethod
    def _sort_value(row, key):
        value = row.get(key, "")
        try:
            return (0, float(value))
        except (TypeError, ValueError):
            return (1, str(value))

    def sort_by(self, key):
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = key, False
        for col in self.columns:
            base = col["title"].rstrip(" ▲▼")
            if col["key"] == key:
                self.tree.heading(col["key"], text=f"{base} {'▼' if self._sort_desc else '▲'}")
            else:
                self.tree.heading(col["key"], text=base)
        self._render()

    # ---- 选择 ----
    def selected_keys(self) -> list:
        return [self.tree.item(iid, "values") and
                self.rows_key_of(iid) for iid in self.tree.selection()]

    def rows_key_of(self, iid):
        row = self.row_of(iid)
        return row.get("__key") if row else None

    def row_of(self, iid):
        index = self.tree.index(iid) if self.tree.exists(iid) else -1
        # 排序之后 index 是显示顺序，要按同样规则映射回去
        rows = self.rows
        if self._sort_key:
            rows = sorted(rows, key=lambda r: self._sort_value(r, self._sort_key),
                          reverse=self._sort_desc)
        if 0 <= index < len(rows):
            return rows[index]
        return None

    def selected_row(self):
        sel = self.tree.selection()
        return self.row_of(sel[0]) if sel else None

    def _selected(self, _event=None):
        if self.on_select:
            self.on_select(self.selected_row())

    def _double(self, _event=None):
        if self.on_double:
            self.on_double(self.selected_row())

    # ---- 右键菜单（可选） ----
    def set_popup(self, actions):
        """actions: [(标题, 回调)]，回调收当前选中行。"""
        self._popup_actions = list(actions)
        if self.menu is None:
            self.menu = self.th.tk.Menu(self.th.root, tearoff=0)

    def _popup(self, event):
        if not self._popup_actions or self.menu is None:
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        self.menu.delete(0, "end")
        row = self.selected_row()
        for title, callback in self._popup_actions:
            self.menu.add_command(label=title,
                                  command=lambda cb=callback, r=row: cb(r))
            if title == "-":
                self.menu.delete("end")
                self.menu.add_separator()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


# ================================================================ 对话框

def _center(win, parent):
    win.update_idletasks()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
    except Exception:
        pass


def ask_text(theme, title, fields, values=None, note=""):
    """多字段输入对话框。fields: [(键, 标签)]，values: {键: 初值}。

    返回 {键: 值} 或 None（取消）。
    """
    c = COLORS
    values = values or {}
    win = theme.tk.Toplevel(theme.root)
    win.title(title)
    win.configure(bg=c["bg"])
    win.transient(theme.root)
    win.resizable(False, False)

    body = theme.frame(win, bg=c["bg"])
    body.pack(fill="both", expand=True, padx=PAD, pady=PAD)
    if note:
        theme.label(body, note, bg=c["bg"], fg=c["muted"],
                    font=theme.font_small, wraplength=380,
                    justify="left").pack(fill="x", pady=(0, GAP))

    entries = {}
    for key, label in fields:
        theme.label(body, label, bg=c["bg"], fg=c["muted"],
                    font=theme.font_small).pack(fill="x", pady=(6, 2))
        var = theme.tk.StringVar(value=str(values.get(key, "")))
        ent = theme.tk.Entry(body, textvariable=var, bg=c["card"], fg=c["fg"],
                             insertbackground=c["fg"], relief="flat",
                             font=theme.font, width=44)
        ent.pack(fill="x", ipady=5)
        entries[key] = var

    result = {"value": None}

    def ok():
        result["value"] = {k: v.get() for k, v in entries.items()}
        win.destroy()

    def cancel():
        win.destroy()

    bar = theme.frame(win, bg=c["bg"])
    bar.pack(fill="x", padx=PAD, pady=(0, PAD))
    theme.button(bar, "确定", ok, accent=True).pack(side="right")
    theme.button(bar, "取消", cancel).pack(side="right", padx=(0, 8))
    win.bind("<Return>", lambda _e: ok())
    win.bind("<Escape>", lambda _e: cancel())
    _center(win, theme.root)
    win.grab_set()
    theme.root.wait_window(win)
    return result["value"]


def ask_choice(theme, title, message, options, default=None):
    """单选框对话框。options: [(值, 标签)]。返回选中的值或 None。"""
    c = COLORS
    win = theme.tk.Toplevel(theme.root)
    win.title(title)
    win.configure(bg=c["bg"])
    win.transient(theme.root)
    win.resizable(False, False)
    body = theme.frame(win, bg=c["bg"])
    body.pack(fill="both", expand=True, padx=PAD, pady=PAD)
    theme.label(body, message, bg=c["bg"], font=theme.font).pack(fill="x",
                                                                 pady=(0, GAP))
    var = theme.tk.StringVar(value=default if default is not None else options[0][0])
    for value, label in options:
        theme.tk.Radiobutton(body, text=label, value=value, variable=var,
                             bg=c["bg"], fg=c["fg"], selectcolor=c["card"],
                             activebackground=c["bg"], activeforeground=c["fg"],
                             font=theme.font, anchor="w",
                             highlightthickness=0).pack(fill="x")
    result = {"value": None}

    def ok():
        result["value"] = var.get()
        win.destroy()

    bar = theme.frame(win, bg=c["bg"])
    bar.pack(fill="x", padx=PAD, pady=(GAP, PAD))
    theme.button(bar, "确定", ok, accent=True).pack(side="right")
    theme.button(bar, "取消", win.destroy).pack(side="right", padx=(0, 8))
    _center(win, theme.root)
    win.grab_set()
    theme.root.wait_window(win)
    return result["value"]


def confirm(theme, title, message) -> bool:
    """是/否确认框。默认焦点在「取消」—— 破坏性操作别让回车误触发。"""
    c = COLORS
    win = theme.tk.Toplevel(theme.root)
    win.title(title)
    win.configure(bg=c["bg"])
    win.transient(theme.root)
    win.resizable(False, False)
    body = theme.frame(win, bg=c["bg"])
    body.pack(fill="both", expand=True, padx=PAD, pady=PAD)
    theme.label(body, message, bg=c["bg"], font=theme.font,
                wraplength=420, justify="left").pack(fill="x")
    result = {"value": False}

    def ok():
        result["value"] = True
        win.destroy()

    bar = theme.frame(win, bg=c["bg"])
    bar.pack(fill="x", padx=PAD, pady=(GAP, PAD))
    theme.button(bar, "确定", ok, accent=True).pack(side="right")
    theme.button(bar, "取消", win.destroy).pack(side="right", padx=(0, 8))
    win.bind("<Escape>", lambda _e: win.destroy())
    _center(win, theme.root)
    win.grab_set()
    theme.root.wait_window(win)
    return result["value"]


def info(theme, title, message):
    """只读提示。"""
    c = COLORS
    win = theme.tk.Toplevel(theme.root)
    win.title(title)
    win.configure(bg=c["bg"])
    win.transient(theme.root)
    win.resizable(False, False)
    body = theme.frame(win, bg=c["bg"])
    body.pack(fill="both", expand=True, padx=PAD, pady=PAD)
    theme.label(body, message, bg=c["bg"], font=theme.font,
                wraplength=460, justify="left").pack(fill="x")
    bar = theme.frame(win, bg=c["bg"])
    bar.pack(fill="x", padx=PAD, pady=(GAP, PAD))
    theme.button(bar, "知道了", win.destroy, accent=True).pack(side="right")
    win.bind("<Return>", lambda _e: win.destroy())
    win.bind("<Escape>", lambda _e: win.destroy())
    _center(win, theme.root)
    win.grab_set()
    theme.root.wait_window(win)


# ================================================================ 导入导出

def write_export(path, rows, columns) -> int:
    """导出。路径后缀决定格式：.csv 或 .json。返回写了多少行。

    [!] CSV 用 utf-8-sig（带 BOM）—— 不带的话 Excel 打开是乱码，
        而"导出来给 Excel 看"正是这个功能最常见的用途。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row.get(col, "") for col in columns])
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    return len(rows)


def read_import(path, columns) -> list:
    """读入。返回 [dict]，键取 columns。格式按后缀判断。

    [!] 坏行**跳过而不是整份报错** —— 用户从别处粘来的 CSV 里有几行不干净
        是常态，整份拒绝会让人以为文件坏了。
    """
    ext = os.path.splitext(path)[1].lower()
    out = []
    if ext == ".csv":
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                with open(path, "r", encoding=enc, newline="") as fh:
                    reader = csv.reader(fh)
                    header = next(reader, None)
                    if header is None:
                        return []
                    index = {name: i for i, name in enumerate(header)}
                    for line in reader:
                        item = {}
                        for col in columns:
                            pos = index.get(col)
                            item[col] = (line[pos].strip()
                                         if pos is not None and pos < len(line) else "")
                        if any(item.values()):
                            out.append(item)
                return out
            except UnicodeDecodeError:
                out = []
                continue
        return out
    data = json.load(open(path, "r", encoding="utf-8"))
    if isinstance(data, dict):
        data = [{"key": k, "value": v} for k, v in data.items()]
    if not isinstance(data, list):
        return []
    for item in data:
        if isinstance(item, dict):
            out.append({col: str(item.get(col, "") or "") for col in columns})
    return out
