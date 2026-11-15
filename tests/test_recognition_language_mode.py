"""Default multilingual receive, explicit English mode and Chinese source checks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.config.specification import BY_KEY
from app.recognition import ocr
from app.storage import store


class LanguageModeTests(unittest.TestCase):
    def test_verified_body_language_hint_avoids_combined_read(self):
        from verify.multilingual_probe import sample_image
        text='一緒に攻撃しよう'
        image=sample_image(text,'msgothic.ttc')
        with patch.object(ocr,'_RECEIVE_MODEL','eng+jpn+kor+rus'), \
                patch.object(ocr,'_preferred_read') as preferred, \
                patch.object(ocr,'_read_script',return_value=text):
            variants=ocr.ocr_variants(image,(0,0,image.shape[1],image.shape[0]),language_hint='jpn')
            try:
                self.assertEqual(next(variants),('body_jpn',text))
                preferred.assert_not_called()
            finally:variants.close()

    def test_detected_script_uses_language_reader_before_combined_guess(self):
        from verify.multilingual_probe import sample_image
        for language,text,font in (('jpn','一緒に攻撃しよう','msgothic.ttc'),
                                   ('kor','같이 공격해요','malgunbd.ttf'),
                                   ('rus','Ждите меня','arialbd.ttf')):
            with self.subTest(language=language):
                image=sample_image(text,font)
                with patch.object(ocr,'_RECEIVE_MODEL','eng+jpn+kor+rus'), \
                        patch.object(ocr,'_preferred_read',return_value=text), \
                        patch.object(ocr,'_read_script',return_value=text) as reader, \
                        patch.object(ocr,'_read_fallback') as fallback:
                    variants=ocr.ocr_variants(image,(0,0,image.shape[1],image.shape[0]),max_workers=1)
                    try:
                        self.assertEqual(next(variants),('body_'+language,text))
                        self.assertEqual(reader.call_args.args[2],language)
                        self.assertEqual(reader.call_count,1)
                        fallback.assert_not_called()
                    finally:variants.close()

    def test_new_and_missing_language_choices_default_to_multilingual_without_writing(self):
        self.assertTrue(default_config()['experimental_multilingual'])
        self.assertFalse(BY_KEY['experimental_multilingual']['live'])
        for raw in (None, {}, {'font_size': 24, 'unknown': {'keep': 1}}):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/'config.json'
                if raw is not None:
                    self.assertTrue(store.atomic_write_json(path, raw))
                before = path.read_bytes() if path.exists() else None
                self.assertTrue(ConfigRepository(path).load(environment={})['experimental_multilingual'])
                self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_template_and_explicit_language_choices_survive_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = ConfigRepository(Path(folder)/'config.json')
            self.assertTrue(repo.ensure_template())
            self.assertTrue(repo.load(environment={})['experimental_multilingual'])
            for enabled in (False, True):
                self.assertTrue(repo.save({'experimental_multilingual': enabled}))
                self.assertEqual(ConfigRepository(repo.path).load(environment={})['experimental_multilingual'], enabled)

    def test_english_keeps_resident_acceleration_and_independent_chinese_model(self):
        previous = ocr._RECEIVE_MODEL
        reader = Mock()
        with patch('app.recognition.resident.ResidentReader', return_value=reader) as start:
            try:
                ocr.configure_receive_languages(False)
                self.assertIs(ocr._RESIDENT, reader)
                self.assertEqual(start.call_args.args[2], 'eng')
                self.assertEqual(ocr.source_model_key('wait for me'), 'chi_sim+eng')
            finally:
                ocr.close_receive_backend()
                ocr._RECEIVE_MODEL = previous
        reader.close.assert_called_once()

    def test_english_never_starts_script_fallbacks(self):
        from verify.multilingual_probe import sample_image
        image = sample_image('wait for me', 'arialbd.ttf')
        with patch.object(ocr, '_RECEIVE_MODEL', 'eng'), \
                patch.object(ocr, '_preferred_read', return_value=''), \
                patch.object(ocr, '_read_fallback', return_value=''), \
                patch.object(ocr, '_read_script') as scripts:
            list(ocr.ocr_variants(image, (0,0,image.shape[1],image.shape[0]), max_workers=1,language_hint='jpn'))
        scripts.assert_not_called()


class LanguageModeUiTests(unittest.TestCase):
    def test_switch_starts_enabled_and_preserves_explicit_english_choice(self):
        from tests import test_workspace_ui as workspace_tests
        fixture = workspace_tests.WorkspaceTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.app.open_page('recognition')
        fixture.app.root.update()
        def walk(parent):
            for child in parent.winfo_children():
                yield child
                yield from walk(child)
        label = next(widget for widget in walk(fixture.app.page)
                     if widget.winfo_class() == 'Label' and str(widget.cget('text')).startswith('多语言识别'))
        switches = [widget for widget in walk(label.master.master) if widget.winfo_class() == 'TCheckbutton']
        self.assertEqual(len(switches), 1)
        self.assertTrue(switches[0].instate(['selected']))
        switches[0].invoke()
        fixture.app.root.update()
        repo = ConfigRepository(fixture.config)
        self.assertFalse(repo.load(environment={})['experimental_multilingual'])
        switches[0].invoke()
        fixture.app.root.update()
        self.assertTrue(repo.load(environment={})['experimental_multilingual'])


if __name__ == '__main__':
    unittest.main()
