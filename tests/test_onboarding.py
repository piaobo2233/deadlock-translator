"""First-run onboarding marker and gate logic; no Tk and no network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import onboarding
from app.config.repository import ConfigRepository
from app.storage import store


class OnboardingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = store.data_path(self.root, 'config.json')

    def load(self):
        return ConfigRepository(self.config, log=lambda *args: None).load(environment={})

    def test_show_on_fresh_install_and_when_existing_key_has_no_calibration(self):
        self.assertTrue(onboarding.should_show(self.root, self.load()))
        store.atomic_write_json(self.config, dict(api_key='fixture-key'))
        from gui.onboarding import gate_initial_page
        self.assertEqual(gate_initial_page(self.root, 'home'), 'onboarding')

    def test_environment_key_alone_does_not_suppress_the_guide(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fixture-environment-key'}):
            config = ConfigRepository(self.config, log=lambda *args: None).load()
        self.assertEqual(config.key_source, 'environment')
        self.assertTrue(onboarding.should_show(self.root, config))

    def test_marker_records_outcome_and_survives_config_rewrites(self):
        self.assertTrue(onboarding.mark_complete(self.root, outcome='saved'))
        self.assertTrue(onboarding.is_complete(self.root))
        state = json.loads(Path(onboarding.state_path(self.root)).read_text(encoding='utf-8'))
        self.assertEqual(state['outcome'], 'saved')
        self.assertEqual(state['schema'], onboarding.SCHEMA)
        self.assertTrue((self.root / 'data' / 'onboarding.json').is_file())
        self.assertFalse(onboarding.should_show(self.root, self.load()))
        store.atomic_write_json(self.config, dict(unknown={'keep': 1}, font_size=19))
        self.assertTrue(onboarding.is_complete(self.root))
        self.assertEqual(store.read_json(self.config)['unknown'], {'keep': 1})

    def test_mark_complete_rejects_unknown_outcome_and_reports_write_failure(self):
        with self.assertRaises(ValueError):
            onboarding.mark_complete(self.root, outcome='whatever')
        with patch.object(store, 'atomic_write_json', return_value=False):
            self.assertFalse(onboarding.mark_complete(self.root, outcome='skipped'))
        self.assertFalse(onboarding.is_complete(self.root))

    def test_broken_marker_is_treated_as_not_complete(self):
        config_path = store.data_path(self.root, 'config.json')
        store.atomic_write_json(config_path, {})
        Path(onboarding.state_path(self.root)).write_text('not json', encoding='utf-8')
        self.assertFalse(onboarding.is_complete(self.root))
        self.assertTrue(onboarding.should_show(self.root, self.load()))

    def test_old_user_with_existing_interface_and_calibration_is_not_interrupted(self):
        from tests.test_region_calibration import calibration
        store.atomic_write_json(self.config, dict(model='legacy-model', capture_calibration=calibration()))
        self.assertFalse(onboarding.should_show(self.root, self.load()))

    def test_gate_only_redirects_the_default_home_page(self):
        from gui.onboarding import gate_initial_page
        with patch('gui.pages._load_config', return_value=self.load()):
            self.assertEqual(gate_initial_page(self.root, 'home'), 'onboarding')
            self.assertEqual(gate_initial_page(self.root, 'settings'), 'settings')
            self.assertEqual(gate_initial_page(self.root, 'api'), 'api')
        onboarding.mark_complete(self.root, outcome='skipped')
        with patch('gui.pages._load_config', return_value=self.load()):
            self.assertEqual(gate_initial_page(self.root, 'home'), 'home')

    def test_gate_failure_does_not_block_startup(self):
        from gui.onboarding import gate_initial_page
        with patch('gui.pages._load_config', side_effect=OSError('broken')):
            self.assertEqual(gate_initial_page(self.root, 'home'), 'home')


if __name__ == '__main__':
    unittest.main()
