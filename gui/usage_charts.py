"""Native, resizable ledger charts with keyboard and pointer inspection."""
import tkinter as tk

from app.storage import usage
from gui.theme import COLORS as C


class UsageChart(tk.Canvas):
    def __init__(self, parent, th, *, mode, inspect, height):
        super().__init__(parent, bg=C['bg'], height=height, width=1,
                         highlightthickness=1, highlightbackground=C['border'], takefocus=True)
        self.th, self.mode, self.inspect = th, mode, inspect
        self.small = th.font_small
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
        from gui.cost_view import range_cost, _amount_text
        line_height = self.small.metrics('linespace')
        needed = max(280, min(440, round(width*.24)), line_height*10)
        if int(self.cget('height')) != needed:
            self.configure(height=needed)
        infos = [range_cost(bucket) for _, bucket in self.rows]
        maximum = max([info['amount'] or 0 for info in infos] + [.0001])*1.25
        ticks = [f'{maximum*(4-index)/4:.4g}' for index in range(5)]
        dates = [stamp[5:].replace('-','/') for stamp, _ in self.rows]
        left = max(self.small.measure(value) for value in ticks)+28
        right = width-max(18, self.small.measure(dates[-1])/2+14)
        top, bottom = 18+line_height*1.5, height-line_height-30
        self.label(14, 12, '费用（元）', anchor='nw')
        for index, value in enumerate(ticks):
            y = top+(bottom-top)*index/4
            self.create_line(left, y, right, y, fill=C['border'], dash=(2, 5))
            self.label(left-12, y, value, anchor='e')
        previous = None
        last_date_right = -1
        for index, ((stamp, _bucket), info) in enumerate(zip(self.rows, infos)):
            x = left+(right-left)*index/max(1, len(infos)-1)
            amount = info['amount']
            y = bottom if amount is None else bottom-(bottom-top)*amount/maximum
            valid = amount is not None
            # Unknown amounts break the line: absence of evidence is not zero.
            if previous is not None and valid:
                self.create_line(*previous, x, y, fill=C['accent'], width=3, tags='trend-line')
            previous = (x, y) if valid else None
            if info['status'] == 'partial':
                self.create_polygon(x,y-5,x+5,y,x,y+5,x-5,y,fill=C['accent'],outline=C['paper'])
            elif valid:
                self.create_oval(x-4,y-4,x+4,y+4,fill=C['bg'] if info['status']=='empty' else C['paper'],
                                 outline=C['accent'],width=1)
            else:
                self.label(x,bottom-9,'?',fill=C['accent'],anchor='center')
            half = self.small.measure(dates[index])/2
            last_start = right-self.small.measure(dates[-1])/2
            if index in {0, len(infos)-1} or (x-half > last_date_right+16 and x+half < last_start-16):
                item = self.label(x,bottom+16,dates[index],anchor='n')
                self.addtag_withtag('date-label', item)
                last_date_right = x+half
            self.points.append((x,y,index))
        if all(info['status']=='empty' for info in infos):
            self.label((left+right)/2, (top+bottom)/2,'暂无请求费用记录',anchor='center')
        else:
            peak = max(range(len(infos)), key=lambda i:infos[i]['amount'] or 0)
            if (infos[peak]['amount'] or 0) > 0:
                value = '已知最高 '+_amount_text(infos[peak])
                half = self.small.measure(value)/2
                x,y,_ = self.points[peak]
                item = self.label(max(left+half,min(right-half,x)),y-12,value,fill=C['paper'],anchor='s')
                self.addtag_withtag('peak-label', item)
        if any(info['status'] not in {'complete','empty'} for info in infos):
            self.label(width-14,12,'◇ 部分已知   ? 金额未知',fill=C['accent'],anchor='ne')
        self._trend_bounds = (left, top, right, bottom)
        self.highlight()

    def draw_models(self, width, height):
        from gui.cost_view import range_cost, _amount_text
        rows=[(name, bucket, range_cost(bucket)) for name, bucket in self.rows]
        count = len(rows)
        max_amount = max([info['amount'] or 0 for _, _, info in rows] + [.0001])
        texts=[(('已知 ' if info['status']=='partial' else '')+_amount_text(info),
                f"{bucket.get('api_attempts',0)} 次 · {bucket.get('total_tokens',0):,} Token")
               for _, bucket, info in rows]
        reserve = max(250, 40+max(self.small.measure(amount) for amount, _ in texts)
                            + max(self.small.measure(stats) for _, stats in texts))
        name_width = min(28+max(self.small.measure(name) for name, _, _ in rows), width*.3)
        stacked = width-reserve-name_width < 80
        line_height = self.small.metrics('linespace')
        row_height = max(28, line_height+12) * (2 if stacked else 1)
        needed = 16+count*row_height
        if int(self.cget('height')) != needed:
            self.configure(height=needed)
        bar_left = 13 if stacked else name_width
        bar_right = width-13 if stacked else width-reserve
        for index, ((name, bucket, info), (amount_label, stats)) in enumerate(zip(rows, texts)):
            y = 8+row_height*(index+.5)
            text_y = y-row_height/4 if stacked else y
            bar_y = y+row_height/4 if stacked else y
            name_space = (width-reserve if stacked else name_width)-28
            shown = name
            if self.small.measure(shown) > name_space:
                while shown and self.small.measure(shown+'…') > name_space:
                    shown = shown[:-1]
                shown = shown+'…' if shown else ''
            self.label(13,text_y,shown,fill=C['fg'])
            self.create_line(bar_left,bar_y,bar_right,bar_y,fill=C['card_hi'],width=11)
            amount = info['amount']
            if amount is not None and amount > 0:
                end = bar_left+(bar_right-bar_left)*amount/max_amount
                self.create_line(bar_left,bar_y,end,bar_y,fill=C['accent'] if index==0 else C['ok'],width=11)
            self.label(width-reserve+12,text_y,amount_label,fill=C['fg'])
            self.label(width-12,text_y,stats,anchor='e')
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
            _,top,_,bottom = self._trend_bounds
            self.create_line(x,top,x,bottom,fill=C['muted'],dash=(3,5),tags='selection')
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
