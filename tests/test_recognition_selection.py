"""Deterministic selection policy tests; no Tesseract or paid API calls."""

import unittest

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
    def test_human_laughter_extension_passes_shared_gates_without_changing_query(self):
        import app.main as dt
        variants = [('body7', '»b;lolll'), ('otsu3x13', '“brlolt'),
                    ('tight2x8', 'b;lolll'), ('gray2x', 'sbzlolll')]
        decision = select_candidate(variants, dt.DEFAULT_GLOSSARY, dt.recognition_rules())
        self.assertEqual(decision.text, 'b;lolll')
        report = dt._translation_quality_report(decision.text, '哈哈', {})
        self.assertEqual(set(report['checks'].values()), {'pass'})
        self.assertEqual(dt.normalize_query(decision.text), 'b;lolll')
        self.assertNotEqual(dt.normalize_query('lolll'), dt.normalize_query('lol'))
        for noise in ('eeeee', 'eooooeeeeeeeeeooe', 'rirrs', '»b;lolll',
                      'lolll | noise', 'loll1', 'lolllllllllllllll'):
            with self.subTest(noise=noise):
                self.assertTrue(dt.is_obvious_garbage(noise, {}))

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
        import app.main as dt
        glossary = dt.DEFAULT_GLOSSARY
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
                    text = dt.fix_digit_confusion(raw, glossary)
                    if text != raw:
                        tried.append(text)
                    if not dt.is_chat_text(text, glossary) or dt.is_obvious_garbage(text, glossary):
                        continue
                    tokens = dt.normalize_text(text).split()
                    score = (sum(t in glossary for t in tokens),
                             sum(t in dt.COMMON_WORDS for t in tokens), len(dt._WORDISH.findall(text)))
                    if best is None or score > best[0]:
                        best = score, text, kind
                    if score[0] >= 3:
                        break
                result = select_candidate(iter(variants), glossary, dt.recognition_rules())
                self.assertEqual(result.tried, tuple(tried))
                self.assertEqual(result.accepted, best is not None)
                if best is not None:
                    self.assertEqual((result.score, result.text, result.kind),
                                     (best[0], dt.trim_tail(best[1], glossary), best[2]))

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
