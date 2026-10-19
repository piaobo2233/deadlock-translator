"""Failed, interrupted and background-error suites must leave truthful reports."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check


class CheckRunnerTests(unittest.TestCase):
    def run_fixture(self, content, *, code=0, timed_out=False):
        with tempfile.TemporaryDirectory() as directory:
            def run(*args, **kwargs):
                kwargs['stdout'].write(content)
                if timed_out:
                    raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
                return subprocess.CompletedProcess(args[0], code)

            with patch.object(check, 'ROOT', Path(directory)), patch.object(check.subprocess, 'run', run), \
                    contextlib.redirect_stdout(io.StringIO()):
                successful = check.run_suite('unit', ['fixture.py'], timeout=5)
            report = json.loads((Path(directory) / '.temp/check/suites.json').read_text(encoding='utf-8'))
            return successful, report['unit']

    def test_timeout_preserves_partial_log_and_structured_failure(self):
        successful, report = self.run_fixture('test_in_progress ...\n', timed_out=True)
        self.assertFalse(successful)
        self.assertTrue(report['timed_out'])
        self.assertIsNone(report['code'])
        self.assertIsNone(report['tests'])
        self.assertEqual(report['timeout_seconds'], 5)

    def test_failed_suite_still_reports_skips(self):
        successful, report = self.run_fixture('Ran 9 tests in 1s\nFAILED (failures=1, skipped=2)\n', code=1)
        self.assertFalse(successful)
        self.assertEqual(report['tests'], 9)
        self.assertEqual(report['skipped'], 2)

    def test_background_callback_error_cannot_pass_with_zero_exit(self):
        successful, report = self.run_fixture('Ran 2 tests in 1s\nOK\nException in Tkinter callback\n')
        self.assertFalse(successful)
        self.assertTrue(report['callback_error'])

    def test_successful_skipped_suite_is_reported_without_inventing_pass_count(self):
        successful, report = self.run_fixture('Ran 7 tests in 1s\nOK (skipped=1)\n')
        self.assertTrue(successful)
        self.assertEqual(report['skipped'], 1)
        self.assertIsNone(report['passed'])


if __name__ == '__main__':
    unittest.main()
