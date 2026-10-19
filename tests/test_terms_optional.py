"""Missing derived game data must not prevent importing or using translation helpers."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app.translation import terms


class OptionalTermsTests(unittest.TestCase):
    def tearDown(self):
        terms.records.cache_clear()

    def test_missing_or_malformed_optional_catalogue_degrades_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terms.json'
            for body in (None, '{', '{}', '{"terms":{}}', '{"terms":[null]}',
                         '{"terms":[{"english":1,"chinese":"value"}]}'):
                if body is not None:
                    path.write_text(body, encoding='utf-8')
                terms.records.cache_clear()
                data = json.loads(body) if body not in (None, '{') else {}
                with patch.object(terms, 'catalogue', return_value=data):
                    self.assertEqual(terms.exact_terms(), {})
                    self.assertEqual(terms.hint('synthetic test'), '')

    def test_valid_terms_keep_existing_matching_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terms.json'
            path.write_text(json.dumps({'terms': [
                {'english': 'Test Beacon', 'chinese': '测试信标'},
                {'english': 'Pocket', 'chinese': '测试歧义名'}]}), encoding='utf-8')
            terms.records.cache_clear()
            with patch.object(terms, 'catalogue', return_value=json.loads(path.read_text(encoding='utf-8'))):
                self.assertEqual(terms.exact_terms(), {'test beacon': '测试信标'})
                self.assertIn('Test Beacon = 测试信标', terms.hint('Use Test Beacon'))
                self.assertEqual(terms.hint('Test Beacons'), '')


if __name__ == '__main__':
    unittest.main()
