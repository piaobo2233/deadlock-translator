"""Incremental current-session reader, with full text when overlay space runs out."""
import time

from app.runtime.session_view import view_path
from app.storage import store
from gui.appearance import panel, text, section
from gui.theme import COLORS as C


def display_label(row):
    if not row.get('successful', True):
        from app.presentation.reasons import LABELS
        return '翻译未完成：' + LABELS.get(row.get('reason'), '原因无法判断')
    state = row.get('display')
    if state == 'layout_unavailable':return '空间不足，仅在面板显示'
    if state == 'tk_drawn':return '已收到绘制回执（不代表游戏实际可见）'
    if state == 'waiting':return '已有译文，等待显示回执'
    if state in {'not_current', 'display_expired', 'source_changed', 'removed'}:return '原文已离开，面板保留全文'
    if state == 'suspended':return '叠加已暂停，面板保留全文'
    return '仅在面板显示；当前叠加状态：' + str(state or '无法判断')


class ReceiveMessages:
    LIMIT = 200

    def __init__(self, parent, app):
        self.app, self.th = app, app.th
        self.rows, self.rendered, self.expanded = {}, {}, set()
        self.session, self.selected, self.following = None, None, True
        area = section(parent, app.th, '本次会话消息')
        self.status = text(area, app.th, '暂无消息；启动后在这里查看最近原文与译文。', small=True, muted=True)
        self.status.pack(anchor='w', pady=(0, 6))
        body = panel(area, app.th);body.pack(fill='both', expand=True)
        self.view = app.th.tk.Text(body, height=8, width=20, wrap='word', state='disabled',
            bg=C['soft'], fg=C['fg'], font=app.th.font, padx=12, pady=10,
            relief='flat', highlightthickness=1, highlightbackground=C['border'], highlightcolor=C['accent'])
        scroll = app.th.ttk.Scrollbar(body, command=self.scroll)
        scroll.pack(side='right', fill='y')
        self.view.configure(yscrollcommand=scroll.set)
        self.view.pack(fill='both', expand=True)
        self.view.bind('<MouseWheel>', lambda event: app.after_page(1, self.track_scroll), add='+')
        self.view.bind('<KeyRelease>', lambda event: self.track_scroll(), add='+')
        actions = panel(area, app.th);actions.pack(fill='x', pady=(8, 0))
        more = panel(area, app.th);more.pack(fill='x', pady=(6, 0))
        self.buttons = {}
        for key, label, command in (
                ('original', '复制原文', lambda: self.copy('original')),
                ('translation', '复制译文', lambda: self.copy('translation')),
                ('expand', '展开／收起全文', self.expand), ('review', '错译处理', self.review),
                ('latest', '返回最新', self.latest)):
            button = app.th.button(more if key in {'review', 'latest'} else actions, label, command)
            button.pack(side='left', padx=(0, 6))
            self.buttons[key] = button
        self.refresh()

    def scroll(self, *args):
        self.view.yview(*args)
        self.track_scroll()

    def track_scroll(self):
        self.following = self.view.yview()[1] >= .999

    def latest(self):
        self.following = True
        self.view.see('end')

    def select(self, key):
        self.selected = key
        self.view.tag_configure('chosen', background=C['card_hi'])
        self.view.tag_remove('chosen', '1.0', 'end')
        bounds = self.view.tag_ranges('message_' + key)
        if bounds:self.view.tag_add('chosen', *bounds)
        self.repaint_actions()

    def repaint_actions(self):
        row = self.rows.get(self.selected, {})
        for key in ('original', 'translation', 'expand', 'review'):
            enabled = bool(row.get(key if key in {'original', 'translation'} else 'original'))
            self.buttons[key].configure(state='normal' if enabled else 'disabled')

    def content(self, row):
        original, translation = str(row.get('original', '')), str(row.get('translation', ''))
        if row['id'] not in self.expanded:
            original = original if len(original) <= 220 else original[:220]+'…〔选中后展开全文〕'
            translation = translation if len(translation) <= 220 else translation[:220]+'…〔选中后展开全文〕'
        clock = time.strftime('%H:%M:%S', time.localtime(row.get('at', 0)))
        return f'{clock}  {display_label(row)}\n原文：{original}\n译文：{translation or "暂无译文"}\n\n'

    def reconcile(self, rows):
        self.view.mark_set('reading_anchor', '@0,0')
        self.view.mark_gravity('reading_anchor', 'right')
        current = {row['id']: row for row in rows[-self.LIMIT:]}
        self.view.configure(state='normal')
        for key in set(self.rows)-set(current):
            tag = 'message_' + key
            bounds = self.view.tag_ranges(tag)
            if bounds:self.view.delete(*bounds)
            self.view.tag_delete(tag)
            self.rendered.pop(key, None);self.expanded.discard(key)
        for key, row in current.items():
            value, tag = self.content(row), 'message_' + key
            if self.rendered.get(key) == value:continue
            bounds = self.view.tag_ranges(tag)
            position = str(bounds[0]) if bounds else 'end-1c'
            if bounds:self.view.delete(*bounds)
            self.view.insert(position, value, (tag,))
            self.view.tag_bind(tag, '<Button-1>', lambda event, key=key: self.select(key))
            self.rendered[key] = value
        self.rows = current
        if self.selected not in current:self.selected = None
        elif self.selected is not None:self.select(self.selected)
        if self.following:self.view.see('end')
        else:self.view.yview('reading_anchor')
        self.view.configure(state='disabled')
        self.repaint_actions()

    def refresh(self):
        from app.runtime.panel_instance import owner
        current = owner(self.app.root_dir)
        value = store.read_json(view_path(self.app.root_dir), {})
        session = value.get('session_id') if isinstance(value, dict) else None
        valid = session and session == current.get('token')
        if session != self.session or not valid:
            self.view.configure(state='normal');self.view.delete('1.0', 'end');self.view.configure(state='disabled')
            self.rows.clear();self.rendered.clear();self.expanded.clear();self.selected=None
            self.session = session if valid else None
        rows = value.get('rows', []) if valid else []
        self.reconcile([row for row in rows if isinstance(row, dict) and row.get('direction') == 'receive'
                        and isinstance(row.get('id'), str)])
        self.status.configure(text=(f'最近 {len(self.rows)} 条 · '+('会话已结束，记录仅保留在本机' if current.get('stopped') else '点击消息选择操作'))
                              if self.rows else '暂无消息；启动后在这里查看最近原文与译文。')
        self.app.after_page(1000, self.refresh)

    def copy(self, field):
        value = self.rows.get(self.selected, {}).get(field)
        if value:
            self.app.root.clipboard_clear();self.app.root.clipboard_append(value)
            self.app.set_status('已复制'+('原文' if field == 'original' else '译文'), C['ok'])

    def expand(self):
        if self.selected is None:return
        if self.selected in self.expanded:self.expanded.remove(self.selected)
        else:self.expanded.add(self.selected)
        self.following = False
        self.reconcile(list(self.rows.values()))
        bounds = self.view.tag_ranges('message_' + self.selected)
        if bounds:self.view.yview(bounds[0])
        self.track_scroll()

    def review(self):
        row = self.rows.get(self.selected, {})
        from app.storage.review_queue import ReviewQueue
        from app.translation.query import normalize_query
        service = ReviewQueue(store.data_path(self.app.root_dir, 'pending.json'),
                              store.data_path(self.app.root_dir, 'glossary.json'))
        try:
            match = next((item for item in service.items() if row.get('request_id')
                and row['request_id'] in item.get('request_ids', [])
                and normalize_query(row['original']) == normalize_query(item['original'])
                and row.get('translation') == item.get('api_translation', item.get('translation'))), None)
        except (OSError, ValueError, TypeError):match = None
        if match and (match.get('status') != 'pending' or service.reviewable(match)):
            self.app.open_page('dictionary')
            self.app.dictionary.pick('review' if match['status'] == 'pending' else 'processed')
            self.app.dictionary.list.choose(match['key'])
        else:
            self.app.set_status('没有可核对的审核来源或显示回执；可在词库中手动添加／修改，本操作未请求 API。', C['warn'])
