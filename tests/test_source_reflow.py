"""Pixel reflow cannot drop a row or bypass full source agreement."""
import unittest
import numpy as np
from app.recognition.ocr import _source_single_line
from app.recognition.source import assess_source


class SourceReflowTests(unittest.TestCase):
    def test_all_rows_including_a_short_final_row_are_preserved(self):
        image = np.full((50, 120), 255, np.uint8)
        image[5:20, 5:100] = 0
        image[30:43, 5:14] = 0
        result = _source_single_line(image, 2)
        self.assertIsNotNone(result)
        self.assertEqual(np.count_nonzero(result < 100), np.count_nonzero(image < 100))
        self.assertIsNone(_source_single_line(image, 3))
        self.assertIsNone(_source_single_line(image, 1))
        self.assertIsNone(_source_single_line(np.full_like(image, 255), 2))

    def test_source_still_requires_confidence_and_the_complete_negated_sentence(self):
        def tsv(text, confidence):
            return 'text\tconf\n' + text + '\t' + str(confidence) + '\n'
        expected = '日本語ではない'
        self.assertTrue(assess_source(tsv(expected, 87), expected).verified)
        self.assertFalse(assess_source(tsv(expected, 0), expected).verified)
        self.assertFalse(assess_source(tsv('日本語', 99), expected).verified)
