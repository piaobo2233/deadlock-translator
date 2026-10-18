"""Native, resizable ledger charts with keyboard and pointer inspection."""
import tkinter as tk

from app.storage import usage
from gui.theme import COLORS as C


class UsageChart(tk.Canvas):
    def __init__(self, parent, th, *, mode, inspect, height):
        super().__init__(parent, bg=C['bg'], height=height, width=1,
                         highlightthickness=1, highlightbackground=C['border'], takefocus=True)
        self.th, self.mode, self.inspect = th, mode, inspect
        self.rows, self.points, self.selected = [], [], 0
        self.bind('<Configure>', lambda _e: self.draw())
        self.bind('<Motion>', self.motion)
        self.bind('<Button-1>', lambda _e: self.focus_set())
        self.bind('<Left>', lambda _e: self.step(-1))
        self.bind('<Right>', lambda _e: self.step(1))
        self.bind('<FocusIn>', lambda _e: self.configure(highlightbackground=C['accent']))
        self.bind('<FocusOut>', lambda _e: self.configure(highlightbackground=C['border']))

    def set_rows(self, rows, *, available=True):
        self.rows = rows if available else []
        self.available = available
        self.selected = max(0, len(self.rows)-1) if self.mode == 'trend' else 0
        self.draw()

    def label(self, x, y, text, *, fill=None, anchor='w', font=None):
        return self.create_text(x, y, text=text, fill=fill or C['muted'],
                                anchor=anchor, font=font or self.th.font_small)

    def draw(self):
        self.delete('all'); self.points = []
        width, height = self.winfo_width(), self.winfo_height()
        if width < 20 or height < 20:
            return
        if not self.rows:
            self.label(width/2, height/2, '暂无模型请求记录' if getattr(self, 'available', True)
                       else '记录不可用，无法绘图', anchor='center')
            return
        if self.mode == 'trend':
            self.draw_trend(width, height)
        else:
            self.draw_models(width, height)

    def draw_trend(self, width, height):
        from gui.cost_view import range_cost
        left, right, top, bottom = 68, width-24, 18, height-32
        infos = [range_cost(bucket) for _, bucket in self.rows]
        maximum = max([info['amount'] or 0 for info in infos] + [.0001])*1.2
        for index in range(4):
            y = top+(bottom-top)*index/3
            self.create_line(left, y, right, y, fill=C['border'], dash=(2, 5))
            self.label(left-9, y, f'{maximum*(3-index)/3:.3g}', anchor='e')
        self.label(8, height-16, '元', anchor='nw')
        previous = None
        for index, ((stamp, _bucket), info) in enumerate(zip(self.rows, infos)):
            x = left+(right-left)*index/max(1, len(infos)-1)
            amount = info['amount']
            y = bottom if amount is None else bottom-(bottom-top)*amount/maximum
            valid = amount is not None
            # Unknown amounts break the line: absence of evidence is not zero.
            if previous is not None and valid:
                self.create_line(*previous, x, y, fill=C['accent'], width=2)
            previous = (x, y) if valid else None
            if info['status'] == 'partial':
                self.create_polygon(x,y-5,x+5,y,x,y+5,x-5,y,fill=C['accent'],outline=C['paper'])
            elif valid:
                self.create_oval(x-3,y-3,x+3,y+3,fill=C['bg'] if info['status']=='empty' else C['paper'],
                                 outline=C['accent'],width=1)
            else:
                self.label(x,bottom-9,'?',fill=C['accent'],anchor='center')
            if len(infos) <= 7 or index in {0, len(infos)-1} or index % 5 == 0:
                self.label(x,bottom+15,stamp[5:].replace('-','/'),anchor='center')
            self.points.append((x,y,index))
        if all(info['status']=='empty' for info in infos):
            self.label((left+right)/2, top+18,'暂无请求费用记录',anchor='center')
        elif any(info['status'] not in {'complete','empty'} for info in infos):
            self.label(right,top,'◇ 部分已知   ? 金额未知',fill=C['accent'],anchor='e')
        self.highlight()

    def draw_models(self, width, height):
        from gui.cost_view import range_cost, _amount_text
        count = len(self.rows)
        row_height = max(28, (height-12)/max(count,1))
        max_amount = max([range_cost(b)['amount'] or 0 for _, b in self.rows] + [.0001])
        name_width = min(165, width*.23)
        bar_right = max(name_width+25, width-250)
        for index, (name, bucket) in enumerate(self.rows):
            y = 8+row_height*(index+.5)
            info = range_cost(bucket)
            shown = name if len(name) <= 20 else name[:19]+'…'
            self.label(13,y,shown,fill=C['fg'])
            self.create_line(name_width,y,bar_right,y,fill=C['card_hi'],width=11)
            amount = info['amount']
            if amount is not None and amount > 0:
                end = name_width+(bar_right-name_width)*amount/max_amount
                self.create_line(name_width,y,end,y,fill=C['accent'] if index==0 else C['ok'],width=11)
            amount_label=('已知 ' if info['status']=='partial' else '')+_amount_text(info)
            self.label(bar_right+12,y,amount_label,fill=C['fg'])
            self.label(width-12,y,f"{bucket.get('api_attempts',0)} 次 · {bucket.get('total_tokens',0):,} Token",anchor='e')
            self.points.append((width/2,y,index))

    def motion(self, event):
        if not self.points:
            return
        axis = 0 if self.mode == 'trend' else 1
        value = event.x if axis == 0 else event.y
        self.select(min(self.points, key=lambda p: abs(p[axis]-value))[2])

    def step(self, offset):
        if self.rows:
            self.select((self.selected+offset) % len(self.rows))
        return 'break'

    def select(self, index):
        self.selected = index
        name, bucket = self.rows[index]
        from gui.cost_view import range_cost, _amount_text
        info = range_cost(bucket)
        state = {'partial':'部分已知','unknown':'金额未知','unpriced':'缺当时单价',
                 'complete':'已知估算','empty':'无记录'}[info['status']]
        self.inspect(f"{name} · {_amount_text(info)} · {state} · "
                     f"{bucket.get('api_attempts',0)} 次请求 · 输入 {bucket.get('prompt_tokens',0):,} / "
                     f"输出 {bucket.get('completion_tokens',0):,} Token")
        self.highlight()

    def highlight(self):
        self.delete('selection')
        if self.mode == 'trend' and self.points:
            x,y,_ = self.points[min(self.selected,len(self.points)-1)]
            self.create_oval(x-6,y-6,x+6,y+6,outline=C['paper'],width=1,tags='selection')


def visible_models(models, limit=3):
    """Keep the page compact; remaining models stay inspectable in details."""
    rows = sorted(models.items(), key=lambda row: -(row[1].get('request_cost') or {}).get('known_amount',0))
    if len(rows) <= limit:
        return rows
    other = usage.empty_bucket()
    for _, bucket in rows[limit-1:]:
        usage._merge(other, bucket)
    return rows[:limit-1]+[(f'其他 {len(rows)-limit+1} 个模型',other)]
