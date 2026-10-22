"""Native Tk region editing against disposable images; no game/API required."""
import gc
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.config.repository import ConfigRepository
from app.storage import store
from gui.app import App
from gui.region_editor import RegionEditor
import gui.pages


class RegionEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=ConfigRepository(store.data_path(self.root,'config.json'))
        self.repo.save(dict(unknown={'keep':1},capture_preset='auto'))
        self.running=patch('app.platform.desktop.translator_is_running',return_value=False)
        self.running.start();self.addCleanup(self.running.stop)
        self.app=App(str(self.root));self.errors=[]
        self.app.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        self.app.open_page('recognition');self.app.root.update()
        def close():
            self.app.root.destroy();self.app=None;gc.collect()
            self.assertFalse(self.errors)
        self.addCleanup(close)

    def wait(self, predicate):
        end=time.monotonic()+4
        while predicate() and time.monotonic()<end:
            self.app.root.update();time.sleep(.01)
        self.assertFalse(predicate())

    def open_editor(self, size=(1920,1080), **kwargs):
        editor=RegionEditor(self.app.page,self.app,Image.new('RGB',size,'white'),
                            'imported_screenshot',**kwargs)
        self.app.game_settings['editor'][0]=editor
        self.app.root.update();return editor

    def test_import_without_game_saves_original_pixels_pending_match_and_merges_other_edits(self):
        path=self.root/'original.png';Image.new('RGB',(2560,1440),'white').save(path)
        before=path.read_bytes()
        with patch('gui.region.screen_size',return_value=(1920,1080)), \
             patch('gui.region.capture_game_frame',side_effect=AssertionError('no game capture')), \
             patch('tkinter.filedialog.askopenfilename',return_value=str(path)):
            self.app.game_settings['import_image']()
            self.wait(lambda:self.app.game_settings['state']['busy'])
            editor=self.app.game_settings['editor'][0]
            self.assertEqual(editor.image.size,(2560,1440))
            self.assertEqual(str(editor.save_button.cget('state')),'disabled')
            self.assertNotIn('capture_calibration',store.read_json(self.repo.path))
            editor.set_rect([100,200,2200,800]);editor.confirmed.set(True);editor.save_state()
            self.repo.save(dict(font_size=22))
            editor.save()
            saved=store.read_json(self.repo.path)
            self.assertEqual(saved['capture_calibration']['screen'],[2560,1440])
            self.assertEqual(saved['capture_calibration']['rect'],[100,200,2200,800])
            self.assertEqual(saved['capture_calibration']['coverage'],'unverified')
            self.assertEqual(saved['font_size'],22);self.assertEqual(saved['unknown'],{'keep':1})
            self.assertNotIn(str(path),Path(self.repo.path).read_text())
            self.assertIn('参考范围',self.app.game_settings['region_status'].cget('text'))
            self.assertEqual(path.read_bytes(),before)
            self.assertFalse(editor.alive);self.assertIsNone(editor.image)
            self.assertIsNone(editor.photo);self.assertIsNone(editor.local_photo)

    def test_drag_keyboard_reference_undo_and_cancel_never_write(self):
        original=Path(self.repo.path).read_bytes();editor=self.open_editor()
        x,y=editor.view.display(*editor.draft.rect[:2])
        editor.press(SimpleNamespace(x=x,y=y))
        editor.motion(SimpleNamespace(x=x+20,y=y+10))
        self.assertNotEqual(editor.draft.rect,editor.draft.initial)
        editor.drag=None;editor.handle='move'
        basis=editor.draft.pixels()
        editor.key(SimpleNamespace(keysym='Right',state=1))
        self.assertEqual(editor.draft.pixels()[0],basis[0]+10)
        editor.set_rect(editor.reference)
        editor.set_rect(editor.draft.initial)
        self.assertEqual(editor.draft.rect,editor.draft.initial)
        editor.close();self.assertEqual(Path(self.repo.path).read_bytes(),original)

    def test_diagnostic_uses_unsaved_draft_and_discards_result_after_edit(self):
        editor=self.open_editor();entered=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        def blocked(image,config,*,rect):
            entered.set();release.wait(3)
            self.assertEqual(rect,[100,80,1200,600])
            return image.crop(rect),'STALE OCR TEXT'
        before=Path(self.repo.path).read_bytes()
        with patch('gui.game_settings.diagnostic_read',side_effect=blocked):
            editor.set_rect([100,80,1200,600]);editor.test()
            self.assertTrue(entered.wait(1))
            editor.set_rect([140,100,1300,650]);release.set()
            self.wait(lambda:editor.busy)
        self.assertIn('已丢弃',editor.note.cget('text'))
        self.assertNotIn('STALE OCR TEXT',editor.note.cget('text'))
        self.assertEqual(editor.draft.pixels(),[140,100,1300,650])
        self.assertEqual(Path(self.repo.path).read_bytes(),before)

    def test_late_result_after_navigation_cannot_recreate_editor(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def blocked(path):
            entered.set();release.wait(3)
            return Image.new('RGB',(1920,1080),'white'),False
        controls=self.app.game_settings
        with patch('gui.region.import_game_image',side_effect=blocked):
            controls['begin']('import','fake.png');self.app.root.update()
            self.assertTrue(entered.wait(1))
            self.app.open_page('home');release.set();self.app.root.update()
        self.assertFalse(controls['state']['alive'])
        self.assertIsNone(controls['editor'][0])
        self.assertNotIn('capture_calibration',store.read_json(self.repo.path))

    def test_small_window_buttons_visible_letterbox_inert_and_rotation_blocks_save(self):
        editor=self.open_editor(rotated=True)
        editor.window.geometry('640x450');self.app.root.update()
        self.assertTrue(editor.save_button.winfo_ismapped())
        self.assertLess(editor.save_button.winfo_rooty()+editor.save_button.winfo_height(),
                        editor.window.winfo_rooty()+editor.window.winfo_height()+1)
        self.assertIsNone(editor.hit(editor.view.ox-15,editor.view.oy))
        editor.confirmed.set(True);editor.save_state()
        self.assertEqual(str(editor.save_button.cget('state')),'disabled')
        editor.save();self.assertNotIn('capture_calibration',store.read_json(self.repo.path))
        editor.close()
        editor=self.open_editor(size=(640,480))
        self.assertTrue(editor.reason)
        self.assertEqual(str(editor.save_button.cget('state')),'disabled')

    def test_old_runtime_receipt_cannot_claim_new_record_or_reset_enabled(self):
        import os
        from app.runtime.config_application import result
        from app.runtime.panel_instance import begin_translator,end_translator
        from tests.test_region_calibration import calibration
        old=calibration();new=dict(old,rect=[200,100,1300,650])
        self.repo.save(dict(capture_calibration=new))
        owner=begin_translator(self.root);self.addCleanup(lambda:end_translator(self.root,owner))
        runtime=dict(pid=os.getpid(),at=time.time(),config_session=owner['token'],
                     config_application=dict(capture_calibration=result('capture_calibration',old)))
        target=store.data_path(self.root,'runtime.json');store.atomic_write_json(target,runtime)
        with patch('app.platform.desktop.translator_is_running',return_value=True), \
             patch('app.platform.desktop.game_in_foreground',return_value=True), \
             patch('gui.region.screen_size',return_value=(1920,1080)):
            self.app.game_settings['refresh']()
            self.assertIn('等待',self.app.game_settings['region_status'].cget('text'))
            runtime['config_application']['capture_calibration']=result('capture_calibration',new)
            store.atomic_write_json(target,runtime);self.app.game_settings['refresh']()
            self.assertIn('正在使用此区域',self.app.game_settings['region_status'].cget('text'))
            self.app.game_settings['reset']()
            self.assertIn('等待',self.app.game_settings['region_status'].cget('text'))
