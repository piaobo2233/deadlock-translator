"""Bubble identity and temporal confirmation, independent of OCR and desktop UI."""

import collections
import difflib
import itertools
import math
import time
import threading
import uuid
from functools import wraps
from dataclasses import replace
from app.recognition.messages import ConfirmedMessage
from app.recognition.visual import Presence, VisualObservation
from app.translation.query import QueryKey, normalize_query

_BUBBLE_IDS = itertools.count(1)


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def same_text(first, second):
    """Only case/whitespace are safe for confirmed text and quick renewal."""
    return bool(first and second and normalize_query(first) == normalize_query(second))

def normalize(text):
    t = text.strip().lower()
    t = ''.join((ch if ch.isalnum() or ch.isspace() else ' ' for ch in t))
    return ' '.join(t.split())

def similarity(a, b):
    sa, sb = (set(a.split()), set(b.split()))
    tok = len(sa & sb) / max(len(sa), len(sb)) if sa and sb else 0.0
    na, nb = (a.replace(' ', ''), b.replace(' ', ''))
    if not na or not nb:
        return tok
    if na == nb:
        return 1.0
    return max(tok, difflib.SequenceMatcher(None, na, nb).ratio())

class Consensus:

    def __init__(self, threshold=2, similarity_cut=0.6, ttl=5.0, pos_seconds=12.0,
                 pos_px=30, static_seconds=25.0, clock=None, clock_domain=None):
        self._lock = threading.RLock()
        self.session_id = uuid.uuid4().hex
        self.clock_domain = clock_domain or ("wall" if clock is None else "injected")
        self.confirmed = {}
        self.clock = clock
        self.threshold = threshold
        self.similarity_cut = similarity_cut
        self.ttl = ttl
        self.pos_seconds = pos_seconds
        self.pos_px = pos_px
        self.static_seconds = static_seconds
        self.static_seen = {}
        # 确认名单：由调用方注入，参与抑制，**本类绝不修改它**。
        self.static_blacklist = set()
        # 本会话自动学到的静态位置：参与抑制，但**只活在这次进程里**。
        # 分家的理由见 _is_static 的说明。
        self.static_learned = set()
        self.static_candidates = {}
        # [!] 2026-10-11：被静态名单挡掉的次数。原来这件事**完全不可见**
        #     （feed 直接 return None，连 [BUBBLE] 都不打），博士因此在"什么都
        #     识别不了"的状态下白试了一局，而日志里一个字都没有。
        self.static_suppressed = 0
        self.groups = []
        self.recent_pos = []
        self.next_id = next(_BUBBLE_IDS)
        self.live = {}
        self.origin = (0, 0)
        self.last_id = None
        self.last_box = None
        self.last_resumed = None
        self.recheck = 2.0

    def _now(self):
        return self.clock() if self.clock is not None else time.time()

    def _near(self, a, b):
        if a is None or b is None:
            return True
        ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
        bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
        return (abs(ax - bx) < max(self.pos_px, min(a[2], b[2]) * 0.25)
                and abs(ay - by) < max(self.pos_px, min(a[3], b[3]) * 0.5))

    @staticmethod
    def _pos_key(box):
        cx = box[0] + box[2] / 2.0
        cy = box[1] + box[3] / 2.0
        return (round(cx / 25.0), round(cy / 25.0))

    @_synchronized
    def _is_static(self, key, box, now):
        if box is None:
            return False
        pk = self._pos_key(box)
        # [!] 2026-10-11：**只有人工确认的名单才抑制**。
        #     自动学习降级为"只观测"（照样记录候选，但不再靠它挡人）。
        #     为什么必须降级 —— 实测铁证（2026-10-11 10:34~10:43 博士那一局）：
        #         feed 收到的是**抓屏相对坐标**，而"同文本同格停够 25 秒"这条规则
        #         会把**聊天行**的格子学成"静态"。那一局里：
        #           that has gotta be the worst pocket i have ever seen → 格子 (27,2) 在名单里 → 丢弃
        #           lmao                                                   → 格子 (27,2) 在名单里 → 丢弃
        #           > go                                                   → 格子 (23,2) 在名单里 → 丢弃
        #         而同一局里的垃圾 5A all/ff/rE: iw/Ff 落在名单外的格子 → 正常建组。
        #         **4/4 真聊天被静默吃掉**，表现就是"什么都识别不了"。
        #     代价：静态 UI 不再被自动抑制，会多花一点 OCR。但"能认出真聊天"远比
        #     "省一点 CPU"重要 —— 何况垃圾本来还要过文本过滤那一关。
        if pk in self.static_blacklist:
            return True
        prev = self.static_seen.get(pk)
        if prev and prev[0] == key:
            if now - prev[1] >= self.static_seconds:
                self.static_learned.add(pk)
                self.static_candidates.setdefault(pk, {"text": key, "at": now})
        else:
            self.static_seen[pk] = (key, now)
        return False

    @_synchronized
    def learned_static(self):
        """本会话自动学到的静态位置候选：{位置: {"text": ..., "at": ...}}。

        它**不参与跨会话**，只是给调用方一份可审查、可提升的清单 ——
        确认要抑制某个位置必须由人来做（或由调用方明确提升）。
        """
        return dict(self.static_candidates)

    @_synchronized
    def handled(self, box):
        if box is None:
            return False
        now = self._now()
        self._prune_pos(now)
        cx, cy = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
        for e in self.recent_pos:
            if abs(cx - e[0]) < self.pos_px and abs(cy - e[1]) < self.pos_px:
                if self._group_suspended(e[5] if len(e) > 5 else None):
                    return False
                return now - e[3] < self.recheck
        return False

    def _prune_pos(self, now):
        self.recent_pos = [e for e in self.recent_pos if now - e[2] < self.pos_seconds]

    def _group_suspended(self, gid):
        return any(g['id'] == gid and g.get('suspended') for g in self.groups)

    @_synchronized
    def emitted_text(self, box):
        if box is None:
            return None
        self._prune_pos(self._now())
        cx, cy = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
        for e in self.recent_pos:
            if abs(cx - e[0]) < self.pos_px and abs(cy - e[1]) < self.pos_px:
                return e[4]
        return None

    @_synchronized
    def touch(self, box, text=None):
        if box is None:
            return
        now = self._now()
        cx, cy = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
        for e in self.recent_pos:
            if abs(cx - e[0]) < self.pos_px and abs(cy - e[1]) < self.pos_px:
                if text is not None and not same_text(text, e[4]):
                    return False
                gid = e[5] if len(e) > 5 else None
                if self._group_suspended(gid):
                    return False
                e[0], e[1] = cx, cy
                e[3] = now
                for g in self.groups:
                    if g['id'] == gid:
                        g['last'] = now
                        g['box'] = tuple(box)
                        self.live[gid] = [tuple(box), now]
                return True
        return False

    def _already_emitted(self, now, box, text=None):
        if box is None:
            return False
        self._prune_pos(now)
        cx, cy = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
        return any(abs(cx - e[0]) < self.pos_px and abs(cy - e[1]) < self.pos_px
                   and (text is None or same_text(text, e[4]))
                   for e in self.recent_pos)

    @_synchronized
    def feed(self, text, width=0, box=None, *, captured_at=None):
        self.last_resumed = None
        key = normalize(text)
        if len(key) < 2:
            return None
        now = self._now()
        if self._is_static(key, box, now):
            self.static_suppressed += 1
            return None
        self._expire(now)
        # Contradictory selected text suspends the old snapshot immediately.
        # Exact fresh votes can restore it if this was OCR noise; a confirmed
        # replacement permanently retires it. Similar text never renews it.
        for g in self.groups:
            if g['emitted'] and self._near(box, g['box']) and not same_text(text, g['confirmed_text']):
                self._suspend(g)
        group = None
        for g in self.groups:
            matches = (same_text(text, g['confirmed_text']) if g['emitted']
                       else similarity(key, g['key']) >= self.similarity_cut)
            if self._near(box, g['box']) and matches:
                group = g
                break
        if group is None:
            group = {'id': self.next_id, 'key': key, 'box': box, 'variants': collections.Counter(),
                     'votes': collections.Counter(), 'first': now if captured_at is None else captured_at,
                     'last': now, 'width': width, 'emitted': False}
            self.next_id = next(_BUBBLE_IDS)
            self.groups.append(group)
            if box is not None:
                print(f"[BUBBLE] id={group['id']} box={tuple((int(v) for v in box[:4]))}")
        if box is not None:
            ob = group['box']
            if ob is None:
                group['box'] = box
            else:
                group['box'] = tuple((ob[i] * 0.7 + box[i] * 0.3 for i in range(min(len(ob), len(box)))))
        if group['emitted'] and group.get('suspended'):
            group['resume_votes'] += 1
            if group['resume_votes'] >= self.threshold:
                group['suspended'] = False
                previous = self.confirmed[group['id']]
                renewed = replace(previous, text_version=previous.text_version + 1, confirmed_at=now)
                self.confirmed[group['id']] = self.last_resumed = renewed
                self.last_id, self.last_box = group['id'], group['box'] or box
        if group['box'] is not None and not group.get('suspended'):
            self.live[group['id']] = [group['box'], now]
        group['variants'][text.strip()] += 1
        group['votes'][normalize_query(text)] += 1
        group['last'] = now
        group['width'] = max(group['width'], width)
        vote_key, vote_count = group['votes'].most_common(1)[0]
        if not group['emitted'] and vote_count >= self.threshold:
            group['emitted'] = True
            winner = next(value for value, _ in group['variants'].most_common()
                          if normalize_query(value) == vote_key)
            group['confirmed_text'] = winner
            if self._already_emitted(now, box, winner):
                return None
            if box is not None:
                cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
                replaced = [e for e in self.recent_pos
                            if abs(cx - e[0]) < self.pos_px and abs(cy - e[1]) < self.pos_px]
                for entry in replaced:
                    if len(entry) > 5:
                        self.live.pop(entry[5], None)
                        self.confirmed.pop(entry[5], None)
                        self.groups = [g for g in self.groups if g['id'] != entry[5]]
                self.recent_pos = [e for e in self.recent_pos if e not in replaced]
                self.recent_pos.append([box[0] + box[2] / 2.0, box[1] + box[3] / 2.0, now, now, winner.strip(), group['id']])
            self.last_id = group['id']
            self.last_box = group['box'] or box
            # Text corrections create a superseding message ID, never mutate a
            # published snapshot. Each genuinely new confirmation starts v1.
            self.confirmed[group['id']] = ConfirmedMessage(self.session_id, group['id'], 1,
                winner, QueryKey.from_text(winner), now, self.clock_domain, group['first'])
            return winner
        return None

    @_synchronized
    def snapshot_of(self, bubble_id):
        return self.confirmed.get(bubble_id) if self.box_of(bubble_id) is not None else None

    @_synchronized
    def identity_matches(self, message):
        return self.confirmed.get(message.bubble_id) == message

    @_synchronized
    def identity_for_binding(self, bubble_id):
        """An OCR-confirmed version still needs fresh visual validation to live."""
        group = next((g for g in self.groups if g['id'] == bubble_id), None)
        if group is None or group.get('suspended'):
            return None
        return self.confirmed.get(bubble_id)

    def _suspend(self, group):
        group.update(suspended=True, resume_votes=0, visual_missing=None)
        self.live.pop(group['id'], None)

    @_synchronized
    def suspend_visual(self, message):
        if not self.identity_matches(message):
            return False
        group = next((g for g in self.groups if g['id'] == message.bubble_id), None)
        if group is not None:
            self._suspend(group)
        return group is not None

    @_synchronized
    def bind_visual(self, message, captured_at, *, max_age=.25):
        """Visual-bound identities cannot fall back to the old OCR-only TTL."""
        if (not math.isfinite(captured_at) or captured_at > self._now()
                or not math.isfinite(max_age) or max_age <= 0):
            raise ValueError("Invalid visual binding time")
        if not self.identity_matches(message):
            return False
        group = next((g for g in self.groups if g['id'] == message.bubble_id), None)
        if group is None or group.get('suspended'):
            return False
        if (group.get('visual_version') == message.text_version
                and captured_at <= group['visual_observed']):
            return False  # Rebinding cannot reset ordering or a missing streak.
        group.update(visual_version=message.text_version, visual_last=captured_at,
                     visual_observed=captured_at - 1e-9, visual_max_age=max_age,
                     visual_missing=None)
        return True

    @_synchronized
    def observe_visual(self, observation: VisualObservation, *, missing_grace=.08):
        if not math.isfinite(missing_grace) or missing_grace < 0:
            raise ValueError("Invalid visual missing grace")
        message, at = observation.identity, observation.captured_at
        if not self.identity_matches(message) or at > self._now():
            return False
        group = next((g for g in self.groups if g['id'] == message.bubble_id), None)
        if (group is None or group.get('visual_version') != message.text_version
                or at <= group['visual_observed']):
            return False
        group['visual_observed'] = at
        state = observation.presence
        if state is Presence.PRESENT:
            if group.get('suspended'):
                return False  # Only fresh exact OCR confirmation may resume.
            group.update(visual_last=at, visual_missing=None, box=observation.box, last=at)
            self.live[message.bubble_id] = [observation.box, at]
        elif state is Presence.UNKNOWN:
            self._suspend(group)
        elif state is Presence.MISSING:
            if group['visual_missing'] is None:
                group['visual_missing'] = at
            elif at - group['visual_missing'] >= missing_grace:
                self._retire(message.bubble_id)
        else:
            self._retire(message.bubble_id)
        return True

    def _retire(self, bubble_id):
        self.groups = [g for g in self.groups if g['id'] != bubble_id]
        self.live.pop(bubble_id, None)
        self.confirmed.pop(bubble_id, None)
        self.recent_pos = [e for e in self.recent_pos if len(e) <= 5 or e[5] != bubble_id]

    @_synchronized
    def box_for(self, message):
        if not isinstance(message, ConfirmedMessage) or self.confirmed.get(message.bubble_id) != message:
            return None
        return self.box_of(message.bubble_id)

    @_synchronized
    def box_of(self, bubble_id):
        if bubble_id is None:
            return None
        hit = self.live.get(bubble_id)
        if not hit or self._now() - hit[1] >= self.ttl:
            return None
        group = next((g for g in self.groups if g['id'] == bubble_id), None)
        if (group and group.get('visual_version') is not None and
                (group['visual_version'] != self.confirmed[bubble_id].text_version or
                 self._now() - group['visual_last'] >= group['visual_max_age'])):
            return None
        b = hit[0]
        ox, oy = self.origin
        return (int(b[0]) + ox, int(b[1]) + oy) + tuple((int(v) for v in b[2:]))

    def _expire(self, now):
        self.groups = [g for g in self.groups if now - g['last'] < self.ttl]
        active = {g['id'] for g in self.groups}
        self.live = {k: v for k, v in self.live.items() if k in active and now - v[1] < self.ttl}
        self.confirmed = {k: v for k, v in self.confirmed.items() if k in active}
        self.recent_pos = [e for e in self.recent_pos if len(e) <= 5 or e[5] in active]
