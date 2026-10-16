"""Exact-pixel negative reuse. Cached reads never supply temporal votes."""
from collections import OrderedDict
import hashlib


class RejectedPixels:
    def __init__(self, *, cooldown=.75, limit=128):
        self.cooldown, self.limit = cooldown, limit
        self.rows = OrderedDict()
        self.observed = OrderedDict()

    @staticmethod
    def key(image, rect):
        x, y, w, h = map(int, rect[:4])
        crop = image[max(0,y):max(0,y)+h, max(0,x):max(0,x)+w]
        return tuple(rect[:4]), hashlib.blake2b(crop.tobytes(), digest_size=16).digest()

    def skip(self, key, now):
        stamp = self.rows.get(key)
        return stamp is not None and 0 <= now - stamp < self.cooldown

    def reject(self, key, now):
        self.rows[key] = now
        self.rows.move_to_end(key)
        while len(self.rows) > self.limit:
            self.rows.popitem(last=False)

    def first_seen(self, key, now):
        first, last = self.observed.get(key, (now, now))
        if now - last > 1.0 or now < last:
            first = now
        self.observed[key] = first, now
        self.observed.move_to_end(key)
        while len(self.observed) > self.limit:
            self.observed.popitem(last=False)
        return first


class QuickChatPixels:
    """Reuse verified canned-message glyphs, without adapting toward a new word."""
    def __init__(self, *, lifetime=10., limit=32):
        self.lifetime,self.limit=lifetime,limit
        self.rows=[]

    def remember(self,image,region,at):
        from app.vision.presence import visual_body
        body=visual_body(image,region)
        if body is not None:
            self.rows.append((at,body))
            self.rows=self.rows[-self.limit:]

    def skip(self,image,region,at):
        from app.vision.presence import visual_body, nearby, same_glyphs
        self.rows=[(first,body) for first,body in self.rows if 0<=at-first<self.lifetime]
        current=visual_body(image,region)
        return current is not None and any(nearby(old.box,current.box) and
            same_glyphs(old.mask,current.mask) for _,old in self.rows)


class QuickChatTemplates:
    """Local, manually verified official Chinese glyphs; never learn from OCR."""
    def __init__(self,path,catalog):
        import base64
        import json
        from pathlib import Path
        import numpy as np
        self.templates=[]
        self._scale,self._scaled=None,()
        try:
            data=json.loads(Path(path).read_text(encoding='utf-8'))
            for row in data['templates'][:128]:
                if row.get('verified') is not True or not catalog.matches(row['text']):
                    continue
                height,width=row['shape']
                if not (1<=height<=150 and 1<=width<=1000):
                    continue
                bits=np.unpackbits(np.frombuffer(base64.b64decode(row['mask'],validate=True),np.uint8))
                if len(bits)<height*width:
                    continue
                mask=bits[:height*width].reshape(height,width)
                mask.setflags(write=False)
                self.templates.append(mask)
        except (OSError,ValueError,KeyError,TypeError):
            self.templates=[]

    def matches(self,image,region,*,scale=1.):
        from app.vision.presence import visual_body,same_glyphs
        current=visual_body(image,region)
        if current is None or not self.templates:
            return False
        if self._scale!=scale:
            import cv2
            scaled=tuple(cv2.resize(mask,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
                if scale!=1 else mask for mask in self.templates)
            self._scaled,self._scale=scaled,scale
        return any(same_glyphs(mask,current.mask) for mask in self._scaled)
