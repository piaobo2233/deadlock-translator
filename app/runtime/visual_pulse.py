"""Observe existing identities only; this cannot create text or request an API."""

from dataclasses import dataclass, replace
from collections import Counter, deque
import math
import threading

from app.recognition.messages import ConfirmedMessage
from app.recognition.visual import Presence, VisualObservation
from app.vision.body import locate_body
from app.vision.presence import VisualBody, visual_body, same_glyphs, nearby, distance, context_matches, shifted_context, same_body_box


@dataclass(frozen=True)
class VisualReference:
    identity: ConfirmedMessage | None
    body: VisualBody
    source_verified: bool = False


class VisualPulse:
    def __init__(self, tracker, detect, *, scale=1.0, event=lambda reason: None, activity=None):
        self.tracker, self.detect, self.scale, self.event = tracker, detect, scale, event
        self.activity = activity
        self._lock = threading.RLock()
        self._references = {}
        self._latest = None
        # Store compact body masks, not 64 full screenshots. A late first OCR
        # confirmation must prove continuity, not just match a reappeared word.
        self._history = deque(maxlen=64)
        self._observed_at = -math.inf

    def _bodies(self, image):
        bodies = [getattr(box,'visual',None) or visual_body(image, getattr(box,'region',None) or locate_body(image, box, scale=self.scale))
                  for box in self.detect(image)]
        bodies = self._unique_bodies(body for body in bodies if body is not None)
        with self._lock:
            references=tuple(self._references.values())
        for ref in references:
            if self._select(ref,bodies)[0] in (Presence.PRESENT,Presence.UNKNOWN):
                continue
            fixed = self._fixed_body(image,ref.body,search=ref.source_verified)
            if fixed is not None:
                if not self._extra_text(fixed,bodies):
                    bodies.append(fixed)
                continue
            if self._extra_text(ref.body,bodies):
                continue
            # A world highlight can merge global projection bands while the
            # already confirmed chat remains visible. Recheck only its small
            # neighbourhood against the fixed glyph template; never reuse OCR
            # text or extend a lease when the actual pixels are absent/changed.
            x,y,width,height=ref.body.box[:4]
            margin=max(4,round(8*self.scale))
            local=(max(0,x-margin),max(0,y-height-margin),
                   width+2*margin,height*3+2*margin)
            candidate=visual_body(image,locate_body(image,local,scale=self.scale))
            if (candidate is not None and nearby(ref.body.box,candidate.box)
                    and candidate.box[0]>local[0]+2 and candidate.box[0]+candidate.box[2]<local[0]+local[2]-2
                    and candidate.box[1]+candidate.box[3]<local[1]+local[3]-2
                    and same_glyphs(ref.body.mask,candidate.mask)):
                bodies.append(candidate)
        return self._unique_bodies(bodies)

    def _unique_bodies(self,bodies):
        unique=[]
        for body in bodies:
            duplicate=next((i for i,old in enumerate(unique) if same_body_box(old.box,body.box,scale=self.scale)),None)
            if duplicate is None:
                unique.append(body)
            elif body.mask is None or (unique[duplicate].mask is not None and body.mask.size>unique[duplicate].mask.size):
                unique[duplicate]=body
        return unique

    @staticmethod
    def _extra_text(reference,bodies):
        """A complete current body can disprove an old matching text prefix."""
        if reference.text_box is None:
            return True
        import cv2
        tx,ty,tw,th=reference.text_box
        font_height=max(4,th-8)
        baseline=ty+th-4
        for body in bodies:
            if body.mask is None or body.text_box is None:
                continue
            ax,ay,aw,ah=reference.box[:4]
            bx,by,bw,bh=body.box[:4]
            overlap=max(0,min(ax+aw,bx+bw)-max(ax,bx))*max(0,min(ay+ah,by+bh)-max(ay,by))
            if overlap<.5*aw*ah:
                continue
            x0,y0,_,_=body.text_box
            _,_,components,_=cv2.connectedComponentsWithStats(body.mask,connectivity=8)
            for x,y,width,height,area in components[1:]:
                if area<3 or not max(3,font_height*.4)<=height<=font_height*1.8:
                    continue
                left,top=x0+x,y0+y
                same_line=abs(top+height-baseline)<=max(3,th*.25)
                new_line=ty+th-2<=top and 0<top+height-baseline<=max(30,th*1.5)
                if (same_line and (left>=tx+tw-2 or left+width<=tx+2)) or new_line:
                    return True
        return False

    @staticmethod
    def _fixed_body(image, reference, *, search=False):
        if reference.text_box is None or reference.context is None:
            return None
        import numpy as np
        x,y,_,_=reference.text_box
        height,width=reference.mask.shape
        if x<0 or y<0 or x+width>image.shape[1] or y+height>image.shape[0]:
            return None
        mask=(image[y:y+height,x:x+width].max(axis=2)<120).astype(np.uint8)
        if same_glyphs(reference.mask,mask):
            if not context_matches(image,reference.context):
                return None
            mask.setflags(write=False)
            return VisualBody(reference.box,mask,(x,y,width,height),reference.context)
        if not search:
            return None
        import cv2
        # Propose a small local displacement of the FIXED text mask. A merged
        # portrait/background may defeat body re-location while glyphs move up
        # one stacked row. Correlation proposes only; unchanged component checks
        # still decide, including tiny digits and decimal points.
        margin=3
        vertical=round(reference.box[3]*1.5)
        left,top=max(0,x-margin),max(0,y-vertical)
        right,bottom=min(image.shape[1],x+width+margin),min(image.shape[0],y+height+3)
        area=(image[top:bottom,left:right].max(axis=2)<120).astype(np.uint8)
        if area.shape[0]<height or area.shape[1]<width:
            return None
        scores=cv2.matchTemplate(area,reference.mask,cv2.TM_SQDIFF_NORMED)
        for _ in range(3):
            _,_,position,_=cv2.minMaxLoc(scores)
            sx,sy=position
            found=area[sy:sy+height,sx:sx+width]
            if same_glyphs(reference.mask,found):
                dx,dy=left+sx-x,top+sy-y
                if context_matches(image,reference.context,dx,dy):
                    bx,by,bw,bh,*rest=reference.box
                    found.setflags(write=False)
                    return VisualBody((bx+dx,by+dy,bw,bh,*rest),found,(left+sx,top+sy,width,height),
                                      shifted_context(reference.context,dx,dy))
            scores[max(0,sy-2):sy+3,max(0,sx-2):sx+3]=2.
        return None

    def bind(self, identity, image, region, captured_at):
        body = visual_body(image, region,with_context=True)
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

    def mark_source_verified(self, identity):
        """Only an independent source read can authorize skipping repeated OCR."""
        with self._lock:
            ref = self._references.get(identity.bubble_id)
            if ref is not None and ref.identity == identity:
                self._references[identity.bubble_id] = replace(ref, source_verified=True)

    def project_box(self,image,region,captured_at):
        """Map an old OCR frame to now before it can change live identities."""
        body=visual_body(image,region)
        if body is None:
            return None
        with self._lock:
            if self._latest is None:
                return None
            reference=self._continuous(VisualReference(None,body),tuple(self._history),captured_at)
        return reference.body.box if reference is not None else None

    def feed_current(self,image,region,captured_at,text,first_seen_at,*,source_verified=True):
        with self._lock:
            box=self.project_box(image,region,captured_at)
            if box is None:
                return False,None
            if not source_verified:
                from app.recognition.tracking import same_text
                for ref in self._references.values():
                    if (ref.source_verified and self.tracker.box_for(ref.identity) is not None
                            and self.tracker._near(box,ref.body.box)
                            and not same_text(text,ref.identity.original)):
                        self.event('unverified_ocr_conflict')
                        return True,None
            return True,self.tracker.feed(text,width=region.rect[2],box=box,captured_at=first_seen_at)

    def touch_current(self,image,region,captured_at,text):
        with self._lock:
            box=self.project_box(image,region,captured_at)
            return box is not None and self.tracker.touch(box,text=text)

    def unchanged_verified(self, image, region, captured_at):
        current = visual_body(image, region)
        if current is None:
            return False
        with self._lock:
            latest = self._latest
            references = tuple(self._references.values())
        if latest is None or not 0 <= latest[1] - captured_at < .25:
            return False
        def same_position(ref):
            a,b=ref.body.box,current.box
            overlap=max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))*max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))
            return (overlap>=.9*a[2]*a[3] and b[2]<=1.3*a[2] and b[3]<=1.6*a[3])
        matches = [ref for ref in references if ref.source_verified
            and self.tracker.box_for(ref.identity) is not None
            and same_position(ref)
            and not self._extra_text(ref.body,(current,))
            and self._fixed_body(image,ref.body) is not None]
        return len(matches) == 1

    def update(self, image, captured_at):
        self._validate_time(captured_at)
        if image.flags.writeable:
            image = image.copy()
            image.setflags(write=False)
        bodies = self._bodies(image)
        with self._lock:
            if captured_at <= self._observed_at:
                return
            changed = (self._new_activity(self._latest[2] if self._latest else (), bodies)
                       if self.activity is not None else False)
            self._observed_at = captured_at
            self._latest = (image, captured_at, bodies)
            self._history.append((captured_at, bodies))
            self._references = {bid: ref for bid, ref in self._references.items()
                                if self.tracker.identity_matches(ref.identity)}
            references = list(self._references.values())
            if references:
                self._apply(references, bodies, captured_at)
        if self.activity is not None:
            with self._lock:
                tracking = any(self.tracker.identity_matches(ref.identity) for ref in self._references.values())
            self.activity(captured_at, changed, tracking)

    @staticmethod
    def _new_activity(previous, bodies):
        """Only new/changed accepted bodies wake OCR, not background or removal."""
        remaining = [body for body in previous if body.mask is not None]
        bodies = [body for body in bodies if body.mask is not None]
        for body in bodies:
            candidates = sorted((old for old in remaining if nearby(old.box, body.box)),
                                key=lambda old: distance(old.box, body.box))
            match = next((old for old in candidates if
                          (old.mask.shape == body.mask.shape and old.mask.tobytes() == body.mask.tobytes())
                          or same_glyphs(old.mask, body.mask)), None)
            if match is None:
                return True
            remaining = [old for old in remaining if old is not match]
        return False

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
            reference = replace(reference, body=self._follow_body(reference.body,bodies[chosen]))
        return reference

    @staticmethod
    def _follow_body(reference,current):
        dx=dy=0
        if reference.text_box is not None and current.text_box is not None:
            dx=current.text_box[0]-reference.text_box[0]
            dy=current.text_box[1]-reference.text_box[1]
        return VisualBody(current.box,reference.mask,current.text_box,shifted_context(reference.context,dx,dy))

    @staticmethod
    def _select(ref, bodies):
        near = [index for index, body in enumerate(bodies) if nearby(ref.body.box, body.box)]
        matches = [i for i in near if bodies[i].mask is not None and same_glyphs(ref.body.mask, bodies[i].mask)]
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
                                body=self._follow_body(ref.body,bodies[chosen]))
        return states
