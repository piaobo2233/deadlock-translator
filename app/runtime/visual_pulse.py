"""Observe existing identities only; this cannot create text or request an API."""

from dataclasses import dataclass, replace
from collections import Counter, deque
import math
import threading

from app.recognition.messages import ConfirmedMessage
from app.recognition.visual import Presence, VisualObservation
from app.vision.body import locate_body
from app.vision.presence import VisualBody, visual_body, same_glyphs, nearby, distance


@dataclass(frozen=True)
class VisualReference:
    identity: ConfirmedMessage
    body: VisualBody


class VisualPulse:
    def __init__(self, tracker, detect, *, scale=1.0, event=lambda reason: None):
        self.tracker, self.detect, self.scale, self.event = tracker, detect, scale, event
        self._lock = threading.RLock()
        self._references = {}
        self._latest = None
        # Store compact body masks, not 64 full screenshots. A late first OCR
        # confirmation must prove continuity, not just match a reappeared word.
        self._history = deque(maxlen=64)
        self._observed_at = -math.inf

    def _bodies(self, image):
        bodies = [visual_body(image, locate_body(image, box, scale=self.scale))
                  for box in self.detect(image)]
        return [body for body in bodies if body is not None]

    def bind(self, identity, image, region, captured_at):
        body = visual_body(image, region)
        if body is None:
            return False
        reference = VisualReference(identity, body)
        with self._lock:
            latest = self._latest
            reference = self._continuous(reference, tuple(self._history), captured_at) if latest else None
            if reference is None:
                self.tracker.suspend_visual(identity)
                return False
            if not self.tracker.bind_visual(identity, captured_at):
                return False
            self._references[identity.bubble_id] = reference
            references = list(self._references.values())
            states = self._apply(references, latest[2], latest[1])
            # Other bindings may already have observed this same frame. Collision
            # evidence still hides them immediately, without replaying a timestamp.
            for ref in references:
                if states.get(ref.identity.bubble_id) is Presence.UNKNOWN:
                    self.tracker.suspend_visual(ref.identity)
            present = states.get(identity.bubble_id) is Presence.PRESENT
            if not present or self.tracker.box_for(identity) is None:
                self.tracker.suspend_visual(identity)
                return False
            return True

    def update(self, image, captured_at):
        self._validate_time(captured_at)
        if image.flags.writeable:
            image = image.copy()
            image.setflags(write=False)
        bodies = self._bodies(image)
        with self._lock:
            if captured_at <= self._observed_at:
                return
            self._observed_at = captured_at
            self._latest = (image, captured_at, bodies)
            self._history.append((captured_at, bodies))
            self._references = {bid: ref for bid, ref in self._references.items()
                                if self.tracker.identity_matches(ref.identity)}
            references = list(self._references.values())
        if references:
            self._apply(references, bodies, captured_at)

    def unavailable(self, observed_at):
        self._validate_time(observed_at)
        with self._lock:
            if observed_at <= self._observed_at:
                return
            self._observed_at = observed_at
            self._latest = None
            self._history.append((observed_at, None))
            references = list(self._references.values())
        for ref in references:
            if self.tracker.observe_visual(VisualObservation(ref.identity, observed_at, Presence.UNKNOWN)):
                self.event("visual_unknown")

    def visible_boxes(self, now, *, max_age=.25):
        """Fresh screen rectangles, including untranslated bodies, for layout.

        This snapshot cannot confirm text or renew an identity. Its lease is
        independent of slow OCR and ends immediately on capture failure.
        """
        self._validate_time(now)
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age <= 0:
            raise ValueError('Positive geometry lease required')
        with self._lock:
            latest = self._latest
            if latest is None or not 0 <= now - latest[1] < max_age:
                return ()
            ox, oy = self.tracker.origin
            return tuple((x + ox, y + oy, x + ox + width, y + oy + height)
                         for x, y, width, height, *_ in (body.box for body in latest[2]))

    @staticmethod
    def _validate_time(at):
        if type(at) not in (int, float) or not math.isfinite(at):
            raise ValueError("Finite visual time required")

    def _continuous(self, reference, history, captured_at):
        self._validate_time(captured_at)
        if not history or captured_at < history[0][0] or captured_at > history[-1][0]:
            return None
        start = max(i for i,(at,_) in enumerate(history) if at <= captured_at)
        for _,bodies in history[start:]:
            if bodies is None:
                return None
            state,chosen = self._select(reference,bodies)
            if state is not Presence.PRESENT:
                return None
            reference = replace(reference, body=VisualBody(bodies[chosen].box, reference.body.mask))
        return reference

    @staticmethod
    def _select(ref, bodies):
        near = [index for index, body in enumerate(bodies) if nearby(ref.body.box, body.box)]
        matches = [i for i in near if same_glyphs(ref.body.mask, bodies[i].mask)]
        matches.sort(key=lambda i: distance(ref.body.box, bodies[i].box))
        if not matches:
            return (Presence.CHANGED if near else Presence.MISSING), None
        if len(matches) > 1 and abs(distance(ref.body.box, bodies[matches[0]].box)
                                  - distance(ref.body.box, bodies[matches[1]].box)) <= 1:
            return Presence.UNKNOWN, None
        return Presence.PRESENT, matches[0]

    def _apply(self, references, bodies, at):
        selected = {ref.identity.bubble_id: self._select(ref,bodies) for ref in references}
        claims = Counter(chosen for _,chosen in selected.values() if chosen is not None)
        states = {}
        for ref in references:
            bid = ref.identity.bubble_id
            state,chosen = selected[bid]
            if chosen is not None and claims[chosen] > 1:
                state = Presence.UNKNOWN
            states[bid] = state
            box = bodies[chosen].box if state is Presence.PRESENT else None
            accepted = self.tracker.observe_visual(VisualObservation(ref.identity, at, state, box))
            if accepted:
                self.event("visual_" + state.value)
                if state is Presence.PRESENT:
                    with self._lock:
                        if self._references.get(bid) is ref:
                            # Follow geometry, never adapt the confirmed glyph
                            # template toward a gradual fade or changed word.
                            self._references[bid] = replace(ref,
                                body=VisualBody(bodies[chosen].box, ref.body.mask))
        return states
