"""Screenshot selection and live capture geometry; no real desktop capture or API."""
import gc
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app.recognition.region import RegionCapture, resolve_region
from app.runtime.config_application import apply_config
from gui import region


class RegionGeometryTests(unittest.TestCase):
    def test_config_write_is_not_delayed_by_the_poll_cache(self):
        config=dict(capture_region={})
        stamp=[1]
        load=Mock(side_effect=lambda:config)
        control=RegionCapture(load,lambda:(1920,1080),clock=lambda:0,stamp=lambda:stamp[0])
        before=control.snapshot()
        self.assertEqual(control.snapshot(),before)
        load.assert_called_once()
        config['capture_region']=dict(rect=[40,50,800,500],screen=[1920,1080])
        stamp[0]+=1
        self.assertNotEqual(control.snapshot(),before)
        self.assertEqual(load.call_count,2)

    def test_scaling_preserves_basis_and_clamps_tiny_regions(self):
        original=dict(rect=[300,180,900,600],screen=[1920,1080])
        config=dict(capture_region=original)
        self.assertEqual(resolve_region(config,(2560,1440)),(400,240,1200,800))
        self.assertEqual(resolve_region(config,(1920,1080)),tuple(original['rect']))
        self.assertEqual(original['screen'],[1920,1080])
        small=dict(capture_region=dict(rect=[1888,1058,1920,1080],screen=[1920,1080]))
        rect=resolve_region(small,(800,600))
        self.assertEqual(rect[2:],(800,600))
        self.assertGreaterEqual(rect[2]-rect[0],31)
        self.assertGreaterEqual(rect[3]-rect[1],21)

    def test_live_region_and_resolution_updates_close_old_source_before_new_ready(self):
        config=dict(capture_region={},region_reselect=True)
        screen=[1920,1080]
        clock=[0]
        notify=Mock()
        control=RegionCapture(lambda:config,lambda:tuple(screen),clock=lambda:clock[0],notify=notify)
        stop=threading.Event()
        closed=[]
        def factory(**kw):
            kw['capture_ready']()
            try:
                while not kw['capture_changed']():
                    yield kw['screen_size'],resolve_region(kw['capture_config'],kw['screen_size'])
            finally:
                closed.append(kw['screen_size'])
        source=control.iter_source(factory,stop)
        first=next(source)
        self.assertTrue(control.applied({}))
        config['capture_region']=dict(rect=[100,100,600,400],screen=list(screen))
        clock[0]+=1
        self.assertFalse(control.current())
        second=next(source)
        self.assertNotEqual(first,second)
        self.assertEqual(len(closed),1)
        self.assertTrue(control.applied(config['capture_region']))
        for width in (2560,1600,1920,2560,1920):
            screen[:]=[width,1440 if width==2560 else 1080]
            clock[0]+=1
            self.assertFalse(control.current())
            size,rect=next(source)
            self.assertEqual(size,tuple(screen))
            self.assertEqual(rect,resolve_region(config,size))
            self.assertTrue(control.current())
        source.close()
        self.assertFalse(control.current())
        self.assertEqual(len(closed),7)
        self.assertEqual(notify.call_count,5)

    def test_application_acknowledges_actual_capture_not_only_file_save(self):
        overlay=Mock()
        overlay._capture_region_applied=lambda value:False
        report={}
        self.assertEqual(apply_config(dict(capture_preset='auto'),overlay=overlay,report=report),[])
        self.assertEqual(report['capture_preset']['state'],'pending')
        overlay._capture_region_applied=lambda value:True
        self.assertIn('capture_preset',apply_config(dict(capture_preset='auto'),overlay=overlay,report=report))
        self.assertEqual(report['capture_preset']['state'],'applied')

    def test_running_ocr_source_uses_new_crop_on_next_geometry(self):
        import numpy as np
        import app.main as dt
        from app.recognition import ocr
        config=dict(capture_region=dict(rect=[10,20,600,300],screen=[1920,1080]))
        clock=[0]
        control=RegionCapture(lambda:config,lambda:(1920,1080),clock=lambda:clock[0])
        stop=threading.Event()
        seen=[]
        def detect(image,**kw):
            seen.append((image.shape[:2],kw['offset']))
            if len(seen)==1:
                config['capture_region']=dict(rect=[40,50,800,500],screen=[1920,1080])
                clock[0]+=1
            else:
                stop.set()
            return []
        def factory(**geometry):
            return dt.iter_ocr_source(interval=0,glossary={},only_in_game=False,verbose=False,
                stop_event=stop,frame_provider=lambda:np.zeros((1080,1920,3),dtype=np.uint8),**geometry)
        with patch.object(ocr,'set_tesseract',return_value='fixture'), \
                patch.object(ocr,'set_user_words',return_value=''),patch.object(ocr,'find_bubbles',side_effect=detect), \
                patch.object(dt,'is_paused',return_value=False),patch.dict(dt._LIVE,{},clear=True):
            self.assertEqual(list(control.iter_source(factory,stop)),[])
        self.assertEqual(seen,[((280,590),(10,20)),((450,760),(40,50))])


class CaptureGuardTests(unittest.TestCase):
    def test_only_foreground_game_is_captured_and_geometry_is_stable(self):
        image=Image.new('RGB',(1920,1080),'white')
        with patch('app.main.game_in_foreground',return_value=False),patch('PIL.ImageGrab.grab') as capture:
            with self.assertRaises(region.CaptureError):
                region.capture_game_frame()
            capture.assert_not_called()
        with patch('app.main.game_in_foreground',return_value=True), \
                patch.object(region,'primary_game_screen',return_value=(1920,1080)), \
                patch('PIL.ImageGrab.grab',return_value=image) as capture:
            self.assertIs(region.capture_game_frame(),image)
            capture.assert_called_once_with(bbox=(0,0,1920,1080))
        with patch('app.main.game_in_foreground',side_effect=[True,False]), \
                patch.object(region,'primary_game_screen',return_value=(1920,1080)),patch('PIL.ImageGrab.grab',return_value=image):
            with self.assertRaises(region.CaptureError):
                region.capture_game_frame()
        with patch('app.main.game_in_foreground',return_value=True), \
                patch.object(region,'primary_game_screen',side_effect=[(1920,1080),(2560,1440)]),patch('PIL.ImageGrab.grab',return_value=image):
            with self.assertRaises(region.CaptureError):
                region.capture_game_frame()

    def test_black_frame_is_not_offered_as_an_editable_game_snapshot(self):
        with patch('app.main.game_in_foreground',return_value=True), \
                patch.object(region,'primary_game_screen',return_value=(1920,1080)), \
                patch('PIL.ImageGrab.grab',return_value=Image.new('RGB',(1920,1080),'black')):
            with self.assertRaises(region.CaptureError):
                region.capture_game_frame()


class ReadOnlyRegionTests(unittest.TestCase):
    def test_free_selection_entry_is_removed(self):
        self.assertFalse(hasattr(region,'select_region'))

