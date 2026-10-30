"""Fullscreen settings and game-only translation, without panel input/API work."""
import gc
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from gui.app import App
from gui.region_preview import save_preview,load_preview
import gui.pages


class FullscreenSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch('app.platform.desktop.translator_is_running',return_value=False).start()
        patch('gui.game_resources.GameResources.inspect').start()
        self.app=App(self.temp.name)
        self.addCleanup(self.close)
        self.errors=[]
        self.app.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        self.app.open_page('send');self.app.root.update()

    def close(self):
        self.app.root.destroy();self.app=None;gc.collect()
        self.assertFalse(self.errors)

    def test_default_is_maximized_and_restore_cannot_leave_a_small_window(self):
        a=self.app
        self.assertEqual(a.root.state(),'zoomed')
        a.root.state('normal');a.root.update()
        self.assertEqual(a.root.state(),'zoomed')
        a.root.iconify();a.root.update()
        self.assertEqual(a.root.state(),'iconic')
        a.root.deiconify();a.root.update()
        self.assertEqual(a.root.state(),'zoomed')

    def test_send_page_has_only_game_controls_and_keeps_clipboard_service(self):
        a=self.app
        self.assertFalse(hasattr(a,'composer'))
        self.assertFalse(hasattr(a,'_manual_service'))
        self.assertFalse(hasattr(a,'send_language'))
        self.assertEqual([name for name,_ in a.workspace.sections['send']],['游戏内发言'])
        self.assertIsNotNone(a.input_translate_control['value'])
        self.assertFalse(hasattr(a,'input_recovery'))
        self.assertFalse(hasattr(a,'_feedback_refreshes'))
        from app.translation.clipboard import ClipboardTranslator
        self.assertTrue(callable(ClipboardTranslator))

    def test_font_preview_is_local_and_changes_with_setting(self):
        a=self.app;a.open_page('recognition');a.root.update()
        preview=a.display_preview
        preview.value.set(23);a.root.update()
        self.assertEqual(preview.font.cget('size'),-23)
        labels=[preview.itemcget(item,'text') for item in preview.find_all() if preview.type(item)=='text']
        self.assertTrue(any('23 px' in label for label in labels))
        labels=[a.game_settings['schematic'].itemcget(item,'text') for item in a.game_settings['schematic'].find_all() if a.game_settings['schematic'].type(item)=='text']
        self.assertIn('尚未选择局内截图',labels)


    def test_region_outline_uses_the_same_coordinates_as_capture(self):
        from app.config.repository import ConfigRepository
        from app.recognition.region import preset,resolve_region
        from app.storage import store
        from gui.region_editor import RegionDraft
        a=self.app;a.open_page('recognition');a.root.update()
        repo=ConfigRepository(store.data_path(a.root_dir,'config.json'))
        for rect in (preset((2560,1440))['rect'],[400,210,2100,700]):
            record=RegionDraft((2560,1440),rect).record('imported_screenshot',True,False)
            save_preview(a.root_dir,Image.new('RGB',(2560,1440),'#123456'),record)
            repo.save(dict(capture_calibration=record))
            a.game_settings['refresh']();a.root.update()
            canvas=a.game_settings['schematic']
            picture=next(item for item in canvas.find_all() if canvas.type(item)=='image')
            ox,oy=canvas.coords(picture)
            shown=canvas.coords(canvas.find_withtag('capture-region')[0])
            physical=[(value-(ox if i%2==0 else oy))*2560/canvas._photo.width() for i,value in enumerate(shown)]
            expected=resolve_region(repo.load(),(2560,1440))
            for observed,wanted in zip(physical,expected):self.assertAlmostEqual(observed,wanted,delta=3)
            self.assertNotIn('已由用户核对',a.game_settings['region_status'].cget('text'))


class RegionPreviewTests(unittest.TestCase):
    def test_only_matching_real_source_thumbnail_is_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            record=dict(screen=[2560,1440],rect=[10,20,900,400],created_at='fixture')
            self.assertIsNone(load_preview(root,record))
            self.assertTrue(save_preview(root,Image.new('RGB',(2560,1440),'#123456'),record))
            shown=load_preview(root,record)
            self.assertEqual(shown.size,(1600,900))
            self.assertEqual(shown.getpixel((20,20)),(18,52,86))
            self.assertIsNone(load_preview(root,dict(record,created_at='new-shot')))
            self.assertIsNone(load_preview(root,{}))

    def test_corrupt_or_failed_cache_never_substitutes_a_scene(self):
        from app.storage import store
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            target=Path(store.data_path(root,'ui/region-preview.png'))
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'corrupt')
            self.assertIsNone(load_preview(root,{'screen':[2560,1440]}))
            with patch.object(store,'atomic_write_bytes',return_value=False):
                self.assertFalse(save_preview(root,Image.new('RGB',(100,60)),{'screen':[100,60]}))
