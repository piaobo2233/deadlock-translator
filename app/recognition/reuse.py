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
