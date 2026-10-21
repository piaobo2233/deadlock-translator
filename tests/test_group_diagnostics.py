"""Support report privacy and missing-state behavior; fake app data, no API calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/collect_group_diagnostics.ps1'


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell and Win32 diagnostics')
class GroupDiagnosticsTests(unittest.TestCase):
    def collect(self, folder):
        script = folder / SCRIPT.name
        shutil.copy2(SCRIPT, script)
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
            capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        reports = list(folder.glob('LockLingo-diagnostic-*/diagnostic.json'))
        self.assertEqual(len(reports), 1)
        raw = reports[0].read_text(encoding='utf-8-sig')
        return json.loads(raw), raw, result.stdout.decode('utf-8', 'replace')

    def test_ocr_details_survive_without_secrets_or_chat(self):
        with tempfile.TemporaryDirectory(prefix='group-diagnostic-') as temporary:
            folder = Path(temporary) / '假程序'
            (folder / 'data').mkdir(parents=True)
            (folder / 'LockLingo.exe').write_bytes(b'fixture; never executed')
            (folder / 'VERSION').write_text('1.0.0-beta.1\n', encoding='utf-8')
            secret = 'sk-support-fixture-never-export-this'
            chat = 'PRIVATE CHAT FIXTURE NEVER EXPORT'
            config = dict(api_key=secret, endpoint='https://private.example/'+secret,
                          translate_api=True, only_in_game=True, capture_preset='auto',
                          recognition_mode='realtime', arbitrary=chat)
            runtime = dict(at=time.time(), game=True, game_foreground=False,
                           translated=1, raw_text=chat, consumption={'private':secret},
                           recognition_ocr=dict(backend='cli', last_failure='OSError', text=chat,
                               counts=dict(source_ocr_timeout=3, backend_failures=1, private=secret),
                               stages=dict(source=dict(calls=2, mean_ms=850.5, text=chat),
                                           private=dict(text=chat))),
                           translation_unavailable=dict(source_unverified=2, api_failed=1,
                                                        private=chat))
            (folder / 'data/config.json').write_text(json.dumps(config), encoding='utf-8')
            (folder / 'data/runtime.json').write_text(json.dumps(runtime), encoding='utf-8')
            report, raw, output = self.collect(folder)
            for private in (secret, chat, 'private.example', str(folder)):
                self.assertNotIn(private, raw)
            self.assertNotIn(secret, output)
            self.assertNotIn(chat, output)
            self.assertTrue(report['app_path_has_non_ascii'])
            self.assertEqual(report['recognition_ocr']['backend'], 'cli')
            self.assertEqual(report['recognition_ocr']['counts']['source_ocr_timeout'], 3)
            self.assertEqual(report['recognition_ocr']['stages']['source']['mean_ms'], 850.5)
            self.assertEqual(report['translation_unavailable'], {'source_unverified':2,'api_failed':1})
            self.assertEqual(len(report['foreground_samples']), 5)
            for sample in report['foreground_samples']:
                self.assertEqual(set(sample), {'IsGame','Stage','Error','ClientRect'})

    def test_missing_app_and_state_still_produce_a_report(self):
        with tempfile.TemporaryDirectory(prefix='group-diagnostic-missing-') as temporary:
            report, _, _ = self.collect(Path(temporary))
            self.assertFalse(report['app_found'])
            self.assertFalse(report['runtime_readable'])
            self.assertIsNone(report['runtime_age_seconds'])
            self.assertEqual(report['recognition_ocr'], {})
            self.assertTrue(any('LockLingo.exe' in note for note in report['check_notes']))


if __name__ == '__main__':
    unittest.main()
