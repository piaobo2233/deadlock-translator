"""English default, opt-in multilingual mode, and independent Chinese source checks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.config.specification import BY_KEY
from app.recognition import ocr


class LanguageModeTests(unittest.TestCase):
    def test_existing_configs_default_to_english_and_preserve_opt_in(self):
        self.assertFalse(default_config()['experimental_multilingual'])
        self.assertFalse(BY_KEY['experimental_multilingual']['live'])
        with tempfile.TemporaryDirectory() as folder:
            repo = ConfigRepository(Path(folder)/'config.json')
            self.assertFalse(repo.load(environment={})['experimental_multilingual'])
            self.assertTrue(repo.save({'experimental_multilingual': True}))
            self.assertTrue(repo.load(environment={})['experimental_multilingual'])
            self.assertTrue(repo.save({'experimental_multilingual': False}))
            self.assertFalse(repo.load(environment={})['experimental_multilingual'])

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
        import numpy as np
        from app.vision.body import BodyRegion

        image = np.full((42, 180, 3), 215, np.uint8)
        body = BodyRegion(rect=(0, 0, 180, 42), text_rect=(12, 8, 155, 25),
                          glyph_count=9, line_count=1)
        with patch.object(ocr, '_RECEIVE_MODEL', 'eng'), \
                patch.object(ocr, '_preferred_read', return_value=''), \
                patch.object(ocr, '_read_fallback', return_value=''), \
                patch.object(ocr, '_read_script') as scripts:
            list(ocr.ocr_variants(image, body.rect, region=body, max_workers=1))
        scripts.assert_not_called()


class LanguageModeUiTests(unittest.TestCase):
    def test_switch_saves_only_explicit_experimental_choice(self):
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
                     if widget.winfo_class() == 'Label' and '多语言识别（实验）' in str(widget.cget('text')))
        switches = [widget for widget in walk(label.master.master) if widget.winfo_class() == 'TCheckbutton']
        self.assertEqual(len(switches), 1)
        switches[0].invoke()
        fixture.app.root.update()
        repo = ConfigRepository(fixture.config)
        self.assertTrue(repo.load(environment={})['experimental_multilingual'])
        switches[0].invoke()
        fixture.app.root.update()
        self.assertFalse(repo.load(environment={})['experimental_multilingual'])


if __name__ == '__main__':
    unittest.main()
