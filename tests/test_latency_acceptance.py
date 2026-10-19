"""Release gates reject fast wrong translations, missing samples and synthetic filler."""
import copy
import contextlib
import io
import tempfile
from pathlib import Path
import unittest

from verify.latency_acceptance import main, summarize, template


def passing_sample():
    sessions = [dict(id=str(i), complete=True) for i in range(3)]
    rows = [dict(id=str(i), session_id=str(i % 3), eligible=True, appeared_at=10,
                 drawn_at=11, correct=True, route='api' if i < 30 else 'local',
                 source='natural', resolution='1920x1080' if i < 50 else '2560x1440')
            for i in range(100)]
    return rows, sessions


class LatencyAcceptanceTests(unittest.TestCase):
    def test_complete_natural_sample_passes(self):
        rows, sessions = passing_sample()
        result = summarize(rows, sessions)
        self.assertTrue(result['target_met'])
        self.assertEqual(result['acceptance']['complete_matches'], 3)

    def test_fast_wrong_and_unreviewed_translations_fail(self):
        rows, sessions = passing_sample()
        for i in range(6):
            rows[i]['correct'] = False if i < 3 else None
        result = summarize(rows, sessions)
        score = result['natural_by_resolution']['1920x1080']['all']
        self.assertEqual((score['within_2s'], score['incorrect'], score['correctness_unconfirmed']), (44, 3, 3))
        self.assertFalse(result['target_met'])

    def test_misses_and_two_second_boundary(self):
        rows, sessions = passing_sample()
        for i in range(30, 35):
            rows[i].update(drawn_at=None, correct=None)
        rows[35]['drawn_at'] = 12
        result = summarize(rows, sessions)
        self.assertTrue(result['target_met'])
        self.assertEqual(result['by_resolution']['1920x1080']['all']['within_2s_rate'], .9)
        rows[36]['drawn_at'] = 12.001
        self.assertFalse(summarize(rows, sessions)['target_met'])

    def test_api_cannot_borrow_local_successes(self):
        rows, sessions = passing_sample()
        for i in range(4):
            rows[i]['correct'] = False
        result = summarize(rows, sessions)
        self.assertTrue(result['natural_groups']['all']['target_met'])
        self.assertFalse(result['natural_groups']['api']['target_met'])
        self.assertFalse(result['target_met'])

    def test_controlled_samples_do_not_fill_natural_quota(self):
        rows, sessions = passing_sample()
        rows[-1]['source'] = 'controlled'
        result = summarize(rows, sessions)
        self.assertEqual(result['groups']['all']['eligible'], 100)
        self.assertEqual(result['acceptance']['natural_messages'], 99)
        self.assertFalse(result['target_met'])

    def test_four_second_deadline_includes_controlled_unknown_route(self):
        rows, sessions = passing_sample()
        rows.append(dict(rows[0], id='controlled', source='controlled', route='unknown', drawn_at=14))
        self.assertTrue(summarize(rows, sessions)['target_met'])
        rows[-1]['drawn_at'] = 14.001
        result = summarize(rows, sessions)
        self.assertIn('display_after_4s', result['acceptance']['failures'])
        self.assertEqual(result['groups']['unknown']['over_4s'], 1)

    def test_empty_legacy_and_incomplete_evidence_fail(self):
        self.assertFalse(summarize([])['target_met'])
        legacy = [dict(id='old', eligible=True, appeared_at=0, drawn_at=1)]
        self.assertEqual(summarize(legacy)['groups']['all']['correctness_unconfirmed'], 1)
        self.assertFalse(summarize(legacy)['target_met'])
        rows, sessions = passing_sample()
        sessions[2]['complete'] = False
        self.assertFalse(summarize(rows, sessions)['target_met'])
        sheet = template()
        self.assertFalse(summarize(sheet['messages'], sheet['sessions'])['target_met'])

    def test_unknown_routes_stay_in_denominator(self):
        rows, sessions = passing_sample()
        rows[0]['route'] = 'unknown'
        result = summarize(rows, sessions)
        self.assertEqual(result['natural_groups']['unknown']['eligible'], 1)
        self.assertEqual(result['natural_groups']['all']['eligible'], 100)
        self.assertFalse(result['target_met'])

    def test_missing_resolution_source_or_session_blocks_release(self):
        for changes in (dict(resolution='unknown'), dict(resolution='3840x2160'),
                        dict(source='unknown'), dict(session_id=None)):
            rows, sessions = passing_sample()
            rows[-1].update(changes)
            self.assertFalse(summarize(rows, sessions)['target_met'])

    def test_bad_labels_are_rejected(self):
        rows, sessions = passing_sample()
        for changes in (dict(appeared_at=float('nan')), dict(drawn_at=9), dict(drawn_at=True),
                        dict(correct='yes'), dict(eligible=1), dict(route='cache'),
                        dict(source='synthetic'), dict(resolution=1080), dict(session_id='missing'),
                        dict(stages={'ocr': -1}), dict(drawn_at=None, correct=True)):
            with self.subTest(changes=changes):
                changed = copy.deepcopy(rows)
                changed[0].update(changes)
                with self.assertRaises(ValueError):
                    summarize(changed, sessions)
        with self.assertRaises(ValueError):
            summarize(rows + [rows[0]], sessions)
        with self.assertRaises(ValueError):
            summarize(rows, sessions + [sessions[0]])

    def test_stage_measurements_report_coverage(self):
        rows, sessions = passing_sample()
        rows[0]['stages'] = {'ocr': .2, 'queue': .1, 'api': .5}
        result = summarize(rows, sessions)
        self.assertEqual(result['groups']['api']['stage_seconds']['api']['samples'], 1)
        self.assertEqual(result['groups']['api']['stage_seconds']['api']['maximum'], .5)

    def test_cli_template_is_incomplete_and_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            path = Path(directory) / 'labels.json'
            self.assertEqual(main(['--template', '--output', str(path)]), 0)
            self.assertEqual(main([str(path), '--output', str(Path(directory) / 'score.json')]), 1)
            with self.assertRaises(SystemExit):
                main(['--template', '--output', str(path)])


if __name__ == '__main__':
    unittest.main()
