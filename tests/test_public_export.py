"""Public snapshots reject private paths and real secrets before any publication."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_export', ROOT / 'scripts/prepare_public_source.py')
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class PublicExportTests(unittest.TestCase):
    def test_private_tree_and_credential_are_rejected_without_revealing_the_value(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(export, 'ConfigRepository') as repository:
            repository.return_value.load.return_value = {'api_key': 'fixture-secret-for-export'}
            source = Path(directory) / 'safe.py'
            source.write_text('print(42)\n', encoding='utf-8')
            self.assertEqual(export.checked_payloads({'app/safe.py': source}), {'app/safe.py': b'print(42)\n'})
            for name in ('data/config.json', 'docs/plans/private.md', '../escape.py'):
                with self.assertRaises(ValueError):
                    export.checked_payloads({name: source})
            source.write_text('fixture-secret-for-export', encoding='utf-8')
            with self.assertRaises(ValueError) as failure:
                export.checked_payloads({'app/safe.py': source})
            self.assertNotIn('fixture-secret-for-export', str(failure.exception))

    def test_current_machine_paths_are_rejected_and_line_endings_normalized(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(export, 'ConfigRepository') as repository:
            repository.return_value.load.return_value = {}
            source = Path(directory) / 'safe.md'
            source.write_bytes(b'hello\r\n')
            self.assertEqual(export.checked_payloads({'README.md': source})['README.md'], b'hello\n')
            source.write_text(str(export.ROOT), encoding='utf-8')
            with self.assertRaises(ValueError):
                export.checked_payloads({'README.md': source})


if __name__ == '__main__':
    unittest.main()
