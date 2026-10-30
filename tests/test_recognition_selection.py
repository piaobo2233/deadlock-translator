"""Deterministic selection policy tests; no Tesseract or paid API calls."""

import app.translation.builtins as translation_builtins_module
import app.translation.query as translation_query_module
import app.translation.text as translation_text_module

import app.recognition.capture as recognition_capture

import app.translation.policy as chat_policy

import unittest
from unittest.mock import Mock
from app.recognition.source import SourceEvidence

from app.recognition.selection import RecognitionRules, select_candidate


def rules(**changes):
    values = dict(normalize=lambda text: text.lower().strip(),
                  fix_digits=lambda text, glossary: text,
                  is_chat=lambda text, glossary: True,
                  is_garbage=lambda text, glossary: False,
                  clean=lambda text, glossary: text.strip(),
                  word_count=lambda text: len(text.split()),
                  common_words={"push", "mid", "now", "wait", "for", "me"})
    values.update(changes)
    return RecognitionRules(**values)


class RecognitionSelectionTests(unittest.TestCase):
    def test_chinese_source_stops_fallbacks_when_preferred_latin_text_is_rejected(self):
        def variants():
            yield 'body7', 'MY'
            self.fail('Chinese source must not start Latin fallbacks')
        verify = Mock(return_value=SourceEvidence(False,'native_chinese',reading='谢了'))
        result = select_candidate(variants(), {}, rules(is_chat=lambda *_: False), verify_body=verify)
        self.assertEqual(result.reason,'native_chinese')
        self.assertFalse(result.accepted)
        verify.assert_called_once_with('MY')

    def test_source_verification_does_not_bypass_text_rules(self):
        verify = Mock(return_value=SourceEvidence(True, ''))
        result = select_candidate([('body7','invalid')],{},rules(is_chat=lambda *_: False),verify_body=verify)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason,'bad_ocr')

    def test_verified_sentence_case_without_known_vocabulary_preserves_original(self):
        text='Evaluate intricate statements.'
        glossary=translation_builtins_module.DEFAULT_GLOSSARY
        policy=recognition_capture.recognition_rules()
        self.assertFalse(policy.is_chat(text,glossary))
        self.assertTrue(policy.is_chat(text.lower(),glossary))
        verify=Mock(return_value=SourceEvidence(True,'',reading=text))
        result=select_candidate([('body7',text)],glossary,policy,verify_body=verify)
        self.assertTrue(result.accepted)
        self.assertEqual(result.text,policy.clean(text,glossary))
        self.assertTrue(result.text.startswith('Evaluate'))

    def test_sentence_case_retry_requires_verified_body_and_keeps_noise_gates(self):
        policy=recognition_capture.recognition_rules()
        glossary=translation_builtins_module.DEFAULT_GLOSSARY
        for kind,text,source in (
                ('body7','Evaluate intricate statements.',None),
                ('body7','Evaluate intricate statements.',SourceEvidence(False,'source_disagrees')),
                ('gray','Evaluate intricate statements.',SourceEvidence(True,'')),
                ('body7','Continuous',SourceEvidence(True,'')),
                ('body7','ESAS TUE',SourceEvidence(True,'')),
                ('body7','HimMmavuy issoY',SourceEvidence(True,'')),
                ('body7','Eeeeeeeee oooooooo',SourceEvidence(True,''))):
            with self.subTest(kind=kind,text=text,source=source):
                verify=Mock(return_value=source) if source is not None else None
                result=select_candidate([(kind,text)],glossary,policy,verify_body=verify)
                self.assertFalse(result.accepted)

    def test_human_laughter_extension_passes_shared_gates_without_changing_query(self):
        variants = [('body7', '»b;lolll'), ('otsu3x13', '“brlolt'),
                    ('tight2x8', 'b;lolll'), ('gray2x', 'sbzlolll')]
        decision = select_candidate(variants, translation_builtins_module.DEFAULT_GLOSSARY, recognition_capture.recognition_rules())
        self.assertEqual(decision.text, 'b;lolll')
        report = chat_policy._translation_quality_report(decision.text, '哈哈', {})
        self.assertEqual(set(report['checks'].values()), {'pass'})
        self.assertEqual(translation_query_module.normalize_query(decision.text), 'b;lolll')
        self.assertNotEqual(translation_query_module.normalize_query('lolll'), translation_query_module.normalize_query('lol'))
        for noise in ('eeeee', 'eooooeeeeeeeeeooe', 'rirrs', '»b;lolll',
                      'lolll | noise', 'loll1', 'lolllllllllllllll'):
            with self.subTest(noise=noise):
                self.assertTrue(chat_policy.is_obvious_garbage(noise, {}))

    def test_empty_variants_have_no_ocr_reason(self):
        result = select_candidate([("gray", ""), ("otsu", "")], {}, rules())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "no_ocr")
        self.assertEqual(result.variants, (("gray", ""), ("otsu", "")))
        self.assertEqual(result.rejected_text, "")

    def test_text_rejected_by_language_rules_is_bad_ocr(self):
        result = select_candidate([("gray", "???"), ("otsu", "longer noise")], {},
                                  rules(is_chat=lambda text, glossary: False))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "bad_ocr")
        self.assertEqual(result.rejected_text, "longer noise")

    def test_garbage_gate_cannot_be_bypassed_by_known_word(self):
        result = select_candidate([("gray", "ff")], {"ff": "投降"},
                                  rules(is_garbage=lambda text, glossary: True))
        self.assertFalse(result.accepted)

    def test_digit_fix_preserves_original_and_corrected_diagnostics(self):
        result = select_candidate([("gray", "99")], {"gg": "打得好"},
                                  rules(fix_digits=lambda text, glossary: "gg"))
        self.assertEqual(result.text, "gg")
        self.assertEqual(result.tried, ("99", "gg"))
        self.assertEqual(result.variants, (("gray", "99"),))

    def test_ranking_uses_common_words_after_glossary_tie(self):
        result = select_candidate([("otsu", "B sic for me tofighe"),
                                   ("gray", "wait for me")], {"for": "为", "me": "我"}, rules())
        self.assertEqual(result.text, "wait for me")
        self.assertEqual(result.kind, "gray")

    def test_word_count_is_third_ranking_key(self):
        result = select_candidate([("otsu", "wait"), ("gray", "wait stranger")], {}, rules())
        self.assertEqual(result.text, "wait stranger")

    def test_equal_scores_keep_first_reading(self):
        result = select_candidate([("otsu", "first"), ("gray", "other")], {}, rules())
        self.assertEqual(result.text, "first")

    def test_production_early_exit_does_not_consume_later_variants(self):
        def variants():
            yield "otsu", "push mid now"
            raise AssertionError("Early exit consumed a later variant")
        result = select_candidate(variants(), dict.fromkeys(("push", "mid", "now")), rules())
        self.assertEqual(result.text, "push mid now")
        self.assertEqual(len(result.variants), 1)

    def test_two_known_words_do_not_trigger_early_exit(self):
        result = select_candidate([("otsu", "push mid new"), ("gray", "push mid now")],
                                  {"push": "推", "mid": "中路"}, rules())
        self.assertEqual(result.text, "push mid now")

    def test_normal_short_words_are_passed_to_cleaner_unchanged(self):
        seen = []
        def clean(text, glossary):
            seen.append(text)
            return text
        result = select_candidate([("gray", "wait me")], {}, rules(clean=clean))
        self.assertEqual(result.text, "wait me")
        self.assertEqual(seen, ["wait me"])

    def test_desktop_rules_preserve_previous_live_ranking(self):
        glossary = translation_builtins_module.DEFAULT_GLOSSARY
        cases = [[], [("gray", "")], [("gray", "99")],
                 [("otsu", "push mid new"), ("gray", "push mid now")],
                 [("otsu", "sieme"), ("gray", "wait me"), ("tight", "vaitme")],
                 [("otsu", "of =F. Gauw"), ("gray", "aoe | mame omw | exer")],
                 [("otsu", "push mid now"), ("gray", "push mid now please")]]
        for variants in cases:
            with self.subTest(variants=variants):
                best, tried = None, []
                for kind, raw in variants:
                    if not raw:
                        continue
                    tried.append(raw)
                    text = chat_policy.fix_digit_confusion(raw, glossary)
                    if text != raw:
                        tried.append(text)
                    if not chat_policy.is_chat_text(text, glossary) or chat_policy.is_obvious_garbage(text, glossary):
                        continue
                    tokens = translation_text_module.normalize_text(text).split()
                    score = (sum(t in glossary for t in tokens),
                             sum(t in chat_policy.COMMON_WORDS for t in tokens), len(chat_policy._WORDISH.findall(text)))
                    if best is None or score > best[0]:
                        best = score, text, kind
                    if score[0] >= 3:
                        break
                result = select_candidate(iter(variants), glossary, recognition_capture.recognition_rules())
                self.assertEqual(result.tried, tuple(tried))
                self.assertEqual(result.accepted, best is not None)
                if best is not None:
                    self.assertEqual((result.score, result.text, result.kind),
                                     (best[0], translation_text_module.clean_ocr_text(best[1], glossary), best[2]))

    def test_lone_fallback_short_word_needs_corroboration(self):
        policy = rules(is_chat=lambda text, glossary: text in glossary)
        result = select_candidate([("body7", "dtr"), ("otsu3x", "ty"), ("tight2x", "LY")],
                                  {"ty": "谢谢"}, policy)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "uncorroborated_short")

    def test_preferred_short_words_do_not_require_another_successful_variant(self):
        policy = rules(is_chat=lambda text, glossary: text in glossary)
        for word in ("go", "gg", "ty", "ff", "ok", "hi"):
            with self.subTest(word=word):
                result = select_candidate([("body7", word), ("otsu3x", "noise")], {word: "中文"}, policy)
                self.assertEqual(result.text, word)

    def test_two_different_fallbacks_can_recover_short_word(self):
        policy = rules(is_chat=lambda text, glossary: text in glossary)
        result = select_candidate([("body7", "noise"), ("otsu3x", "gg"), ("gray2x", "gg")],
                                  {"gg": "打得好"}, policy)
        self.assertEqual(result.text, "gg")

    def test_duplicate_variant_name_does_not_count_as_independent_evidence(self):
        policy = rules(is_chat=lambda text, glossary: text in glossary)
        result = select_candidate([("body7", "noise"), ("otsu3x", "ty"), ("otsu3x", "ty")],
                                  {"ty": "谢谢"}, policy)
        self.assertFalse(result.accepted)


if __name__ == "__main__":
    unittest.main()
