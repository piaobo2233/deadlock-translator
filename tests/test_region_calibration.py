"""Calibration contracts with synthetic pixels and disposable configuration."""

import app.recognition.capture as recognition_capture
import app.recognition.live as live_state
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app.config.validation import normalize_config
from app.recognition.region import (RegionCapture, validate_calibration, resolve_region,
                                    region_details, preset)
from gui.region_editor import RegionDraft, Viewport
from gui.region import import_game_image, CaptureError


def calibration(screen=(1920,1080), rect=(100,80,1200,600)):
    return RegionDraft(screen,rect).record('imported_screenshot',True,False)


class CalibrationTests(unittest.TestCase):
    def test_runtime_application_ack_is_bound_to_entire_record_not_preset(self):
        from app.runtime.config_application import apply_config, value_token
        record=calibration();report={};overlay=Mock()
        overlay._capture_region_applied=lambda value:True
        overlay._capture_calibration_applied=lambda value:False
        cfg=dict(capture_preset='auto',capture_calibration=record)
        apply_config(cfg,overlay=overlay,report=report)
        self.assertEqual(report['capture_preset']['state'],'applied')
        self.assertEqual(report['capture_calibration']['state'],'pending')
        overlay._capture_calibration_applied=lambda value:value==record
        apply_config(cfg,overlay=overlay,report=report)
        self.assertEqual(report['capture_calibration']['state'],'applied')
        self.assertEqual(report['capture_calibration']['token'],value_token('capture_calibration',record))
        changed=dict(record,rect=[200,100,1300,650])
        self.assertNotEqual(report['capture_calibration']['token'],value_token('capture_calibration',changed))

    def test_independent_truth_detects_clipped_margin_and_no_chat_stays_unverified(self):
        from verify.calibration_coverage import coverage_report
        record=calibration()
        truth=dict(screen=[1920,1080],chat_boxes=[[110,90,600,200]],margin=8)
        self.assertEqual(coverage_report(record,truth)['coverage'],'visible_boxes_covered')
        truth['margin']=12
        self.assertEqual(coverage_report(record,truth)['missed_indices'],[0])
        truth['chat_boxes']=[]
        self.assertEqual(coverage_report(record,truth)['coverage'],'unverified_no_chat')

    def test_personal_region_wins_over_preset_without_scaling_or_mutation(self):
        saved=calibration()
        cfg=dict(capture_preset='auto',capture_calibration=saved)
        self.assertEqual(resolve_region(cfg,(1920,1080)),tuple(saved['rect']))
        self.assertEqual(resolve_region(cfg,(2560,1440)),tuple(preset((2560,1440))['rect']))
        self.assertIn('1920×1080',region_details(cfg,(2560,1440))['fallback_reason'])
        self.assertEqual(region_details(cfg,(1920,1080))['region_source'],'personal')
        self.assertEqual(saved['screen'],[1920,1080])
        self.assertEqual(resolve_region(cfg,(1920,1080)),tuple(saved['rect']))

    def test_malformed_records_fall_back_without_rewriting_or_exposing_values(self):
        base=calibration()
        for change in ({'version':True},{'version':2},{'rect':[True,80,1200,600]},
                       {'rect':[-1,80,1200,600]}, {'screen':[900,500]},
                       {'full_frame_confirmed':False},{'method':'unverified_algorithm'},
                       {'coverage':'active'}, {'created_at':'bad'}, {'path':'private.png'}):
            with self.subTest(change=change):
                saved=dict(base,**change)
                with self.assertRaises(ValueError):validate_calibration(saved)
                cfg=dict(capture_preset='auto',capture_calibration=saved,unknown={'keep':1})
                self.assertEqual(resolve_region(cfg,(1920,1080)),tuple(preset((1920,1080))['rect']))
                normalized,invalid=normalize_config(cfg)
                self.assertIn('capture_calibration',invalid)
                self.assertEqual(normalized['capture_calibration'],{})
                self.assertIn('无效',region_details(normalized,(1920,1080))['fallback_reason'])
                self.assertEqual(normalized['unknown'],{'keep':1})
                self.assertEqual(cfg['capture_calibration'],saved)

    def test_full_snapshot_factory_and_ack_include_metadata_and_matching_screen(self):
        cfg=dict(capture_preset='auto',capture_region={},capture_calibration=calibration())
        screen=[1920,1080];stamp=[0];stop=threading.Event();seen=[]
        control=RegionCapture(lambda:cfg,lambda:tuple(screen),stamp=lambda:stamp[0],clock=lambda:0)
        def factory(**kw):
            seen.append(kw)
            self.assertFalse(control.applied(cfg['capture_calibration'],key='capture_calibration'))
            kw['capture_ready']()
            while not kw['capture_changed']():yield kw['capture_config']
        source=control.iter_source(factory,stop)
        self.assertEqual(next(source),cfg)
        self.assertTrue(control.applied(cfg['capture_calibration'],key='capture_calibration'))
        # Mutating the loader's object cannot silently mutate the cached snapshot.
        cfg['capture_calibration']['rect']=[140,100,1300,650]
        self.assertEqual(control.snapshot()[1],(100,80,1200,600))
        stamp[0]+=1
        self.assertFalse(control.current())
        self.assertEqual(next(source)['capture_calibration']['rect'],[140,100,1300,650])
        self.assertTrue(control.applied(cfg['capture_calibration'],key='capture_calibration'))
        screen[:]=[2560,1440]
        next(source)
        self.assertFalse(control.applied(cfg['capture_calibration'],key='capture_calibration'))
        screen[:]=[1920,1080]
        next(source)
        self.assertTrue(control.applied(cfg['capture_calibration'],key='capture_calibration'))
        cfg['capture_calibration']={};stamp[0]+=1
        self.assertFalse(control.applied({},key='capture_calibration'))
        next(source)
        self.assertTrue(control.applied({},key='capture_calibration'))
        source.close()
        self.assertFalse(control.applied({},key='capture_calibration'))

    def test_real_ocr_crop_receipt_waits_for_pixels_and_carries_offset(self):
        import numpy as np
        from app.recognition import ocr
        cfg=dict(capture_preset='auto',capture_calibration=calibration())
        stop=threading.Event();ready=Mock();seen=[]
        def detect(pixels,**kw):
            seen.append((pixels.shape[:2],kw))
            stop.set();return []
        with patch.object(ocr,'set_tesseract',return_value='fixture'), \
             patch.object(ocr,'set_user_words',return_value=''),patch.object(ocr,'find_bubbles',side_effect=detect), \
             patch.object(live_state,'is_paused',return_value=False),patch.dict(live_state._LIVE,{},clear=True):
            list(recognition_capture.iter_ocr_source(interval=0,glossary={},only_in_game=False,verbose=False,stop_event=stop,
                frame_provider=lambda:np.full((1080,1920,3),20,dtype=np.uint8),screen_size=(1920,1080),
                capture_config=cfg,capture_ready=ready))
        ready.assert_called_once()
        self.assertEqual(seen[0][0],(520,1100))
        self.assertEqual(seen[0][1]['offset'],(100,80))
        self.assertEqual(seen[0][1]['capture_rect'],(100,80,1200,600))
        self.assertEqual(seen[0][1]['screen'],(1920,1080))


class DraftTests(unittest.TestCase):
    def test_viewport_roundtrip_at_dpi_zoom_and_letterbox_offsets(self):
        for logical in ((980,500),(640,260),(1280,720),(850,420)):
            view=Viewport((2560,1440));view.fit(*logical)
            for factor in (.5,1.25,2,1.1):
                view.zoom(factor,127,93)
                for x,y in ((0,0),(339,190),(2220,766),(2560,1440)):
                    px,py=view.point(*view.display(x,y))
                    self.assertAlmostEqual(px,x);self.assertAlmostEqual(py,y)
            self.assertLess(view.point(view.ox-10,view.oy)[0],0)

    def test_drag_clamps_edges_and_does_not_accumulate_preview_rounding(self):
        draft=RegionDraft((1920,1080),(100,80,1200,600))
        for handle in ('nw','n','ne','e','se','s','sw','w','move'):
            draft.set(draft.initial);draft.adjust(handle,-10000,-10000)
            self.assertGreaterEqual(draft.pixels()[0],0);self.assertGreaterEqual(draft.pixels()[1],0)
            draft.adjust(handle,10000,10000)
            validate_calibration(draft.record('game_capture',True,False))
        draft.set(draft.initial)
        for _ in range(100):draft.adjust('move',.1,.1)
        for a,b in zip(draft.pixels(),[110,90,1210,610]):self.assertLessEqual(abs(a-b),1)
        draft.set(draft.initial)
        self.assertEqual(draft.pixels(),[100,80,1200,600])
        with self.assertRaises(ValueError):draft.record('imported_screenshot',False,False)
        with self.assertRaises(ValueError):draft.set([10,20,5,30])


class ImportedImageTests(unittest.TestCase):
    def test_png_jpeg_pixels_ignore_dpi_and_original_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extension in ('png','jpg'):
                path=Path(tmp)/('image.'+extension)
                Image.new('RGB',(1280,720),'white').save(path,dpi=(300,300))
                original=path.read_bytes();image,rotated=import_game_image(path)
                self.assertEqual(image.size,(1280,720));self.assertFalse(rotated)
                self.assertEqual(image.info,{})
                self.assertEqual(path.read_bytes(),original)

    def test_bad_animation_rotation_and_resource_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.png'
            path.write_bytes(b'not a png')
            with self.assertRaises(CaptureError):import_game_image(path)
            Image.new('RGB',(10,10),'red').save(path,save_all=True,
                append_images=[Image.new('RGB',(10,10),'white')],duration=100)
            with self.assertRaisesRegex(CaptureError,'多帧'):import_game_image(path)
            path=Path(tmp)/'rotated.jpg';exif=Image.Exif();exif[274]=6
            Image.new('RGB',(720,1280),'white').save(path,exif=exif)
            image,rotated=import_game_image(path)
            self.assertEqual(image.size,(1280,720));self.assertTrue(rotated)
            with path.open('wb') as stream:stream.truncate(50*1024*1024+1)
            with self.assertRaisesRegex(CaptureError,'50 MB'):import_game_image(path)
            path.write_bytes(b'x')
            opened=Mock(format='PNG',size=(12001,100),n_frames=1)
            with patch('PIL.Image.open') as load:
                load.return_value.__enter__.return_value=opened
                with self.assertRaisesRegex(CaptureError,'资源上限'):import_game_image(path)
                opened.load.assert_not_called()

    def test_explicit_draft_diagnostic_never_resolves_saved_region_or_writes(self):
        from gui.game_settings import diagnostic_read
        from app.recognition import ocr
        import numpy as np
        image=Image.new('RGB',(1920,1080),'white')
        with tempfile.TemporaryDirectory() as tmp,patch.object(ocr,'set_tesseract',return_value='fixture'), \
             patch.object(ocr,'find_bubbles',return_value=[]) as bubbles, \
             patch('app.recognition.region.resolve_region',side_effect=AssertionError('must use draft')), \
             patch('app.storage.store.atomic_write_json',side_effect=AssertionError('read only')), \
             patch('socket.create_connection',side_effect=AssertionError('no network')):
            crop,note=diagnostic_read(image,dict(_root=tmp),rect=(10,20,600,300))
            self.assertEqual(crop.size,(590,280));self.assertIn('未找到可靠聊天',note)
            self.assertEqual(bubbles.call_args.kwargs,dict(offset=(10,20),screen=(1920,1080),capture_rect=(10,20,600,300)))
            self.assertEqual(list(Path(tmp).rglob('*')),[])
