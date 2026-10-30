"""Finish native child painting after a scroll or a viewport resize."""


class ScrollPaint:
    def __init__(self, widget):
        self.widget = widget
        self.pending = None
        self.painting = False
        widget.bind('<Configure>', self.configure, add='+')
        widget.bind('<Destroy>', self.destroy, add='+')

    def configure(self, event):
        if event.widget is self.widget:
            self.request()

    def request(self):
        if self.pending is None and not self.painting:
            self.pending = self.widget.after_idle(self.flush)

    def flush(self):
        self.pending = None
        self.painting = True
        try:
            # Settle geometry first. Native canvas scrolling does not expose
            # every clipped child; explicitly invalidate only visible regions.
            # Drain their idle paints before accepting the next scroll frame.
            self.widget.update_idletasks()
            x,y=self.widget.winfo_rootx(),self.widget.winfo_rooty()
            self.expose(self.widget,(x,y,x+self.widget.winfo_width(),y+self.widget.winfo_height()))
            self.widget.update_idletasks()
        finally:
            self.painting = False

    def expose(self, widget, clip):
        if not widget.winfo_ismapped():return
        x,y=widget.winfo_rootx(),widget.winfo_rooty()
        width,height=widget.winfo_width(),widget.winfo_height()
        visible=(max(x,clip[0]),max(y,clip[1]),min(x+width,clip[2]),min(y+height,clip[3]))
        if visible[0]>=visible[2] or visible[1]>=visible[3]:return
        widget.event_generate('<Expose>',x=visible[0]-x,y=visible[1]-y,
            width=visible[2]-visible[0],height=visible[3]-visible[1])
        for child in widget.winfo_children():
            self.expose(child,visible)

    def destroy(self, event):
        if event.widget is self.widget and self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None


class MaximizedWindow:
    """One desktop layout; minimize/close still use native window controls."""
    def __init__(self, root):
        self.root=root
        self.pending=None
        root.bind('<Map>',self.changed,add='+')
        root.bind('<Configure>',self.changed,add='+')
        root.bind('<Destroy>',self.destroy,add='+')
        root.state('zoomed')

    def changed(self,event):
        if event.widget is self.root and self.root.state()=='normal' and self.pending is None:
            self.pending=self.root.after_idle(self.restore)

    def restore(self):
        self.pending=None
        if self.root.state()=='normal':self.root.state('zoomed')

    def destroy(self,event):
        if event.widget is self.root and self.pending is not None:
            self.root.after_cancel(self.pending)
            self.pending=None
