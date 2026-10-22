"""Real screenshot diagnostics and Tk drawing; no paid API or private data."""

import app.translation.builtins as translation_builtins_module
import gui.overlay as overlay_ui

import app.recognition.capture as recognition_capture
import app.recognition.live as live_state

import app.platform.game_fonts as game_fonts
import app.translation.desktop_engine as desktop_translation
import ctypes
from ctypes import wintypes
import gc
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import app.recognition.ocr as ocr
from app.presentation.layout import below_bubble
from app.presentation.receipts import DisplayMonitor, DisplayState
from app.recognition.selection import select_candidate
from app.recognition.tracking import Consensus
from app.runtime.events import RuntimeHealth
from app.runtime.messages import DisplayMessage
from app.runtime.visual_pulse import VisualPulse
from app.vision.body import locate_body
from gui.workspace import display_feedback

FIXTURES = Path(__file__).resolve().parents[1]/'verify/truth/match_20261013'
CASES = json.loads((FIXTURES/'cases.json').read_text(encoding='utf-8'))


def frame(name):
    case = CASES[name]
    path = FIXTURES/case['file']
    if hashlib.sha256(path.read_bytes()).hexdigest() != case['sha256']:
        raise AssertionError('Match fixture differs from its provenance manifest')
    crop = ocr.imread_unicode(str(path))
    if crop is None:
        raise AssertionError('Match fixture unreadable')
    width,height = case['screen']
    image = np.full((height,width,3),(30,25,32),np.uint8)
    x,y,right,bottom = case['crop']
    if crop.shape[:2] != (bottom-y,right-x):
        raise AssertionError('Match crop size changed')
    image[y:bottom,x:right] = crop
    return image, (width,height)


class MatchOcrTests(unittest.TestCase):
    def setUp(self):
        if not ocr.set_tesseract():
            self.fail('Declared local verification requires real Tesseract')

    def readings(self, name):
        image,screen = frame(name)
        results=[]
        with patch.object(ocr,'_USER_WORDS',''):
            for box in ocr.find_bubbles(image,screen=screen):
                body=locate_body(image,box,scale=screen[1]/1440)
                decision=select_candidate(ocr.ocr_variants(image,box,screen=screen,region=body),
                                          translation_builtins_module.DEFAULT_GLOSSARY,recognition_capture.recognition_rules(),
                                          verify_body=lambda text: ocr.source_evidence(image,body,text))
                results.append((body,decision))
        return results

    def test_real_short_words_reach_exact_local_translation_keys(self):
        translator=desktop_translation.Translator('', 'https://invalid.local', 'fixture', True,
                                 glossary=translation_builtins_module.DEFAULT_GLOSSARY, allow_popup=False)
        seen=set()
        with patch.object(translator.client,'request',side_effect=AssertionError('Unexpected API')):
            for name in CASES:
                with self.subTest(frame=name):
                    readings=self.readings(name)
                    accepted=[d.text.lower() for _,d in readings if d.accepted]
                    self.assertEqual(accepted,CASES[name]['expected_accepted'])
                    for _,decision in readings:
                        if not decision.accepted:
                            continue
                        result=translator.translate_result(decision.text)
                        self.assertTrue(result.successful,result)
                        key=decision.text.lower()
                        self.assertEqual(result.source,'cache' if key in seen else 'glossary')
                        seen.add(key)
                        self.assertEqual(result.text,translation_builtins_module.DEFAULT_GLOSSARY[decision.text.lower()])
                        self.assertFalse(result.network_participated)
        self.assertEqual(translator.api_attempts,0)
        self.assertEqual(seen,{'gg','push','afk'})

    def test_portrait_edge_is_excluded_but_body_tracking_stays_on_the_source(self):
        readings=self.readings('short_stack')
        self.assertEqual(len(readings),3)
        for region,decision in readings:
            self.assertTrue(region.accepted)
            self.assertGreater(region.text_rect[0],region.rect[0]+8)
            self.assertEqual(region.rect[0],921)
        self.assertFalse(readings[1][1].accepted)  # afj has no reliable supported reading.
        self.assertNotEqual(readings[1][1].text.lower(),'afk')

    def test_actual_capture_confirms_each_short_word_with_its_own_identity(self):
        image,screen=frame('short_stack')
        images=iter((image,image))
        stop=Mock();stop.is_set.side_effect=[False,False,True]
        with patch.object(ocr,'set_user_words',return_value=''),patch.object(ocr,'_USER_WORDS',''), \
             patch.object(live_state,'is_paused',return_value=False),patch.dict(live_state._LIVE,{},clear=True):
            records=list(recognition_capture.iter_ocr_source(interval=0,threshold=2,glossary=translation_builtins_module.DEFAULT_GLOSSARY,
                verbose=False,only_in_game=False,allow_popup=False,stop_event=stop,
                message_snapshots=True,frame_provider=lambda:next(images),screen_size=screen))
        self.assertEqual([r.text.lower() for r in records],['push','afk'])
        self.assertEqual([r.identity.query_key.normalized for r in records],['push','afk'])
        self.assertNotEqual(records[0].identity.key,records[1].identity.key)
        self.assertEqual(records[0].position[:2],(921,216))
        self.assertEqual(records[1].position[:2],(921,326))

    def test_visible_punctuation_is_not_discarded_with_the_portrait_fragment(self):
        image=Image.new('RGB',(220,60),(216,214,205))
        draw=ImageDraw.Draw(image)
        font=ImageFont.truetype(r'C:\Windows\Fonts\arialbd.ttf',18)
        draw.line((0,12,7,25),fill=(10,10,10),width=2)  # Crossing left border.
        draw.text((15,12),'/push!',font=font,fill=(10,10,10))
        pixels=cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR)
        body=locate_body(pixels,(0,0,220,60))
        self.assertTrue(body.accepted)
        self.assertGreater(body.text_rect[0],7)
        self.assertLessEqual(body.text_rect[0],15)  # Real slash remains in the crop.
        self.assertGreaterEqual(body.text_rect[0]+body.text_rect[2],70)


class MatchLayoutTests(unittest.TestCase):
    def test_three_tightly_stacked_rows_use_their_own_right_positions(self):
        sources=((921,216,116,41),(921,271,116,41),(921,326,116,40))
        occupied=[(x,y,x+w,y+h) for x,y,w,h in sources]
        results=[]
        for source in sources:
            result=below_bubble(source,(130,38),(2559,1439),occupied=occupied,pointer=6)
            self.assertIsNotNone(result.rect,result)
            results.append(result)
            left,top,right,bottom=result.rect
            occupied.append((left-(6 if result.side=='right' else 0),
                             top-(6 if result.side=='below' else 0),right,bottom))
        self.assertEqual([r.side for r in results],['right','right','below'])
        for source,result in zip(sources,results):
            if result.side=='right':
                self.assertEqual(result.rect[0],source[0]+source[2]+10)
                self.assertLessEqual(abs((result.rect[1]+result.rect[3])/2-(source[1]+source[3]/2)),.5)

    def test_arrow_and_invalid_obstacles_cannot_bypass_collision_checks(self):
        source=(100,200,180,35)
        # Below arrow would hit this thin obstacle, even if its rectangle fits.
        result=below_bubble(source,(100,30),(1920,1080),occupied=((100,235,280,242),),pointer=6)
        self.assertEqual(result.side,'right')
        for occupied in (((1,2,3),),((1,2,float('nan'),4),),((1,2,0,4),)):
            self.assertEqual(below_bubble(source,(100,30),(1920,1080),occupied=occupied).reason,'invalid_geometry')

    def test_bottom_can_use_right_but_not_a_disconnected_fallback(self):
        result=below_bubble((100,1030,180,35),(200,50),(1920,1080))
        self.assertEqual(result.side,'right')
        self.assertEqual(result.rect,(290,1022,490,1072))
        self.assertEqual(below_bubble((1710,1030,180,35),(200,50),(1920,1080)).reason,'layout_unavailable')

    def test_untranslated_native_bodies_are_fresh_obstacles_only(self):
        image,screen=frame('afk_chinese_stack')
        tracker=Consensus(threshold=1,clock=lambda:10)
        tracker.origin=(0,0)
        pulse=VisualPulse(tracker,lambda pixels:ocr.find_bubbles(pixels,screen=screen),scale=1)
        pulse.update(image,10)
        boxes=pulse.visible_boxes(10.1)
        self.assertEqual(len(boxes),3)
        self.assertFalse(tracker.confirmed)  # Geometry cannot invent confirmed text.
        self.assertFalse(pulse.visible_boxes(10.25))
        self.assertFalse(pulse.visible_boxes(9.9))
        tracker.origin=(50,80)
        self.assertEqual(pulse.visible_boxes(10.1)[0],tuple(v+d for v,d in zip(boxes[0],(50,80,50,80))))
        pulse.unavailable(10.2)
        self.assertFalse(pulse.visible_boxes(10.21))


class MatchTkTests(unittest.TestCase):
    def setUp(self):
        with patch.object(game_fonts,'register_game_font',return_value=[]):
            self.view=overlay_ui.Overlay(dict(width=300,font_size=18,max_messages=6))
        self.errors=[]
        self.view.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        self.monitor=DisplayMonitor(RuntimeHealth(),notify=lambda text:None)
        self.view._display_observer=self.monitor.observe
        self.tracker=Consensus(threshold=1,clock=time.monotonic,clock_domain='monotonic')
        self.source=(250,230,180,35)
        self.tracker.feed('afk',box=self.source)
        identity=self.tracker.snapshot_of(self.tracker.last_id)
        self.message=DisplayMessage('', 'afk', '挂机中', 'team', time.time(), self.source,
                                    identity.bubble_id,identity=identity)
        self.live=patch.dict(live_state._LIVE,{'con':self.tracker,'pulse':None},clear=True)
        self.live.start();self.addCleanup(self.live.stop)
        def close():
            self.view.root.update_idletasks()
            self.view.root.destroy()
            self.assertFalse(self.errors)
            self.view=None;gc.collect()
        self.addCleanup(close)

    def test_actual_tk_failure_retries_and_cannot_revive_a_changed_message(self):
        sw,sh=self.view.root.winfo_screenwidth(),self.view.root.winfo_screenheight()
        blocked=((0,265,sw,sh),(430,0,sw,sh))
        with patch.object(live_state,'live_obstacles',return_value=blocked):
            self.view.render([self.message]);self.view.root.update()
        self.assertFalse(self.view.label_rects)
        self.assertEqual(self.monitor.layout_waiting,1)
        self.assertEqual(self.monitor.events[-1].reason,'layout_unavailable')
        with patch.object(live_state,'live_obstacles',return_value=()):
            self.view.render([self.message]);self.view.root.update()
        self.assertEqual(self.monitor.layout_waiting,0)
        self.assertEqual(self.monitor.events[-1].state,DisplayState.TK_DRAWN)
        self.assertTrue(self.view.canvas.find_withtag('m0'))
        self.tracker.feed('push',box=self.source)
        self.view.render([self.message]);self.view.root.update()
        self.assertFalse(self.view.canvas.find_withtag('m0'))
        self.assertEqual(self.monitor.events[-1].reason,'not_current')

    def test_real_window_coordinates_match_receipts_at_two_tk_scalings(self):
        for scaling in (1.33,1.67):
            with self.subTest(scaling=scaling):
                self.view.root.tk.call('tk','scaling',scaling)
                blocker=(250,265,430,320)
                with patch.object(live_state,'live_obstacles',return_value=(blocker,)):
                    self.view.render([self.message]);self.view.root.update()
                entry=self.monitor.events[-1]
                self.assertEqual(entry.state,DisplayState.TK_DRAWN)
                self.assertEqual(entry.rect[0],440)
                self.assertEqual(self.view.label_rects[0][0],434)  # Left arrow is masked too.
                rectangle=wintypes.RECT()
                self.assertTrue(ctypes.windll.user32.GetWindowRect(self.view.root.winfo_id(),ctypes.byref(rectangle)))
                self.assertEqual(rectangle.left,self.view.root.winfo_rootx())
                self.assertEqual(rectangle.top,self.view.root.winfo_rooty())
                arrows=[item for item in self.view.canvas.find_withtag('m0')
                        if self.view.canvas.type(item)=='polygon' and len(self.view.canvas.coords(item))==6]
                self.assertEqual(len(arrows),1)
                self.assertEqual(rectangle.left+min(self.view.canvas.coords(arrows[0])[::2]),434)

    def test_large_font_multiline_text_stays_inside_the_placed_bubble(self):
        self.view.font_size=36;self.view.width=250;self.view._build_fonts()
        message=self.message._replace(translated='请大家一起推进中路，等待队友到齐后再战斗。')
        with patch.object(live_state,'live_obstacles',return_value=()):
            self.view.render([message]);self.view.root.update()
        entry=self.monitor.events[-1]
        self.assertEqual(entry.state,DisplayState.TK_DRAWN)
        left,top=self.view.root.winfo_rootx(),self.view.root.winfo_rooty()
        boxes=[self.view.canvas.bbox(item) for item in self.view.canvas.find_withtag('m0')
               if self.view.canvas.type(item)=='text']
        self.assertGreaterEqual(len(boxes),2)
        for x0,y0,x1,y1 in boxes:
            self.assertGreaterEqual(left+x0,entry.rect[0])
            self.assertGreaterEqual(top+y0,entry.rect[1])
            self.assertLessEqual(left+x1,entry.rect[2]-overlay_ui.BUBBLE_SHADOW)
            self.assertLessEqual(top+y1,entry.rect[3]-overlay_ui.BUBBLE_SHADOW)
        for before,after in zip(boxes,boxes[1:]):
            self.assertLessEqual(before[3],after[1]+1)


class MatchFeedbackTests(unittest.TestCase):
    def test_translation_count_is_not_a_draw_acknowledgement(self):
        data=dict(at=100,ocr_confirmed=3,translated=2,display_translation_drawn=1,display_layout_waiting=1)
        counts,feedback=display_feedback(data,running=True,now=101)
        self.assertIn('得到译文 2',counts)
        self.assertIn('已绘制译文 1',counts)
        self.assertIn('1 条译文暂未显示',feedback)
        self.assertEqual(display_feedback(data,running=False,now=101),('',''))
        self.assertNotIn('得到译文',display_feedback(data,running=True,now=131)[0])
        self.assertNotIn('得到译文',display_feedback({**data,'translated':-1},running=True,now=101)[0])
        self.assertIn('已绘制译文 —',display_feedback({'at':100,'translated':2},running=True,now=101)[0])

    def test_old_failure_notice_does_not_claim_an_expired_message_is_waiting(self):
        data=dict(at=100,display_layout_waiting=0,ui_status='译文未显示：显示空间不足')
        self.assertEqual(display_feedback(data,running=True,now=101)[1],'')


if __name__=='__main__':
    unittest.main()
