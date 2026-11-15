"""Qualified incoming API results become durable personal entries."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.storage import store
from app.translation.client import ApiResponse
from app.translation.desktop_engine import Translator
from app.translation.learning import LearnedGlossary
from app.translation.local_glossary import load_glossary


class AutoGlossaryTests(unittest.TestCase):
    def test_outgoing_wrong_target_is_clear_and_never_enters_personal_dictionary(self):
        from app.translation.manual import ManualEngine, ManualTranslator
        config = dict(api_key='stub', endpoint='https://invalid.example', model='stub',
                      translate_api=True, send_target='ru')
        manual = ManualTranslator(self.temp.name, lambda: config)
        manual.engine = ManualEngine(self.temp.name, config)
        manual.engine.client.request = Mock(return_value=ApiResponse(text='Why English?'))
        result = manual.translate('怎么英语也不行')
        self.assertFalse(result.successful)
        self.assertEqual(result.reason_code, 'target_language')
        self.assertIn('俄语', result.reason)
        self.assertEqual(store.load_glossary_file(str(self.path)), {})
        manual.translate('怎么英语也不行')
        self.assertEqual(manual.engine.client.request.call_count, 1)

    def test_prompt_clarification_preserves_existing_direction_fences(self):
        import hashlib
        from app.translation.directions import LANGUAGES, prompt_for, query_scope, _TARGET_RULE
        for target in LANGUAGES:
            prompt = prompt_for(target)
            legacy = prompt.removesuffix(_TARGET_RULE)
            expected = 'deadlock-manual-' + hashlib.sha256(legacy.encode()).hexdigest()[:16]
            self.assertEqual(query_scope(target, prompt, outgoing=True)['domain'], expected)

    def test_legacy_builtin_edited_value_remains_personal(self):
        from app.translation.glossary_ops import is_reference
        from app.translation.builtins import DEFAULT_GLOSSARY
        word, value = next(iter(DEFAULT_GLOSSARY.items()))
        self.assertTrue(is_reference(dict(word=word, translation=value, source='builtin')))
        self.assertFalse(is_reference(dict(word=word, translation='用户修改', source='builtin')))
        self.assertFalse(is_reference(dict(word='unknown old entry', translation='旧值', source='builtin')))

    def test_disabled_reference_is_browsable_without_enabling_translation(self):
        from app.translation.glossary_ops import rows, is_reference
        entries = rows(str(self.path), config=dict(builtins_enabled=False), include_reference=True)
        self.assertGreater(len(entries), 100)
        self.assertTrue(all(is_reference(row) and row['status']=='已停用' for row in entries))
        self.assertEqual(load_glossary(str(self.path), builtins_enabled=False), {})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'glossary.json'

    def engine(self):
        e = Translator('stub', 'https://invalid.example', 'stub', False,
            allow_popup=False, learning=LearnedGlossary(str(self.path)))
        e.client.request = Mock(return_value=ApiResponse(text='等等我'))
        return e

    def test_fresh_personal_is_empty_but_reference_remains_available(self):
        merged = load_glossary(str(self.path))
        self.assertTrue(merged)
        self.assertEqual(store.load_glossary_file(str(self.path)), {})

    def test_qualified_result_is_formal_and_restart_never_pays_again(self):
        e = self.engine()
        result = e.translate_result('wait for me')
        self.assertTrue(result.successful)
        row = store.load_glossary_file(str(self.path))['wait for me']
        self.assertEqual((row['source'], row['zh']), ('api', '等等我'))
        self.assertEqual(row['request_id'], result.request_id)
        restarted = self.engine()
        self.assertEqual(restarted.translate_result('wait for me').source, 'glossary')
        restarted.client.request.assert_not_called()

    def test_rejected_response_never_enters_personal_dictionary(self):
        e = self.engine()
        e.client.request.return_value = ApiResponse(text='wait for me')
        self.assertFalse(e.translate_result('wait for me').successful)
        self.assertEqual(store.load_glossary_file(str(self.path)), {})

    def test_formal_write_failure_recovers_from_journal_without_http(self):
        e = self.engine()
        with patch('app.translation.learning.store.update_glossary_file', return_value=False):
            result = e.translate_result('wait for me')
        self.assertTrue(result.successful)
        self.assertFalse(result.save_receipt.saved)
        restarted = self.engine()
        recovered = restarted.translate_result('wait for me')
        self.assertTrue(recovered.successful)
        restarted.client.request.assert_not_called()
        self.assertEqual(store.load_glossary_file(str(self.path))['wait for me']['zh'], '等等我')
