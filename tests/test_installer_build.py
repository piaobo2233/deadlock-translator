"""Installer inputs must be complete, private-data-free and match their manifest."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_build', ROOT / 'scripts/build_installer.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class InstallerInputTests(unittest.TestCase):
    def test_recovery_helper_cannot_be_paired_with_a_stale_program_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)
            with self.assertRaisesRegex(ValueError,'源码不匹配'):build.verify_recovery_source(source)
            names=('app/platform/rollback.py','app/platform/update_install.py','app/platform/updates.py',
                   'app/storage/store.py','app/storage/locking.py','gui/updates.py','scripts/run.py')
            rows=[dict(path=name,sha256=build.digest(ROOT/name)) for name in names]
            (source/'BUILD-SOURCE.json').write_text(json.dumps(dict(files=rows)),encoding='utf-8')
            self.assertEqual(len(build.verify_recovery_source(source)),len(names))
            rows[0]['sha256']='outdated-source'
            (source/'BUILD-SOURCE.json').write_text(json.dumps(dict(files=rows)),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'源码不匹配'):build.verify_recovery_source(source)

    def test_verified_bundle_is_accepted_and_bad_inputs_are_rejected(self):
        original = {
            'DeadlockTranslate.exe': b'executable fixture', 'LockLingo.exe': b'GUI fixture',
            'VERSION': b'1.0.0-test.4\n',
            'ocr/tesseract.exe': b'ocr', 'ocr/libtesseract-5.dll': b'dll',
            '_internal/app/assets/brand/locklingo.ico': b'icon',
        }
        for variant in ('valid', 'missing-gui', 'changed', 'extra', 'private', 'traversal'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = dict(original)
                if variant == 'missing-gui':
                    del files['LockLingo.exe']
                if variant == 'private':
                    files['_internal/config.json'] = b'{}'
                rows = [dict(path=name, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                        for name, body in files.items()]
                if variant == 'changed':
                    files['ocr/tesseract.exe'] = b'tampered'
                if variant == 'extra':
                    files['unlisted.txt'] = b'extra'
                if variant == 'traversal':
                    files['../escape.txt'] = b'outside'
                archive = root / 'test.zip'
                with zipfile.ZipFile(archive, 'w') as bundle:
                    for name, body in files.items():
                        bundle.writestr('bundle/' + name, body)
                    bundle.writestr('bundle/manifest.json', json.dumps(rows))
                if variant == 'valid':
                    self.assertEqual(build.prepare_source(archive, root / 'out'), root / 'out/bundle')
                else:
                    with self.assertRaises(ValueError):
                        build.prepare_source(archive, root / 'out')
                self.assertFalse((root / 'escape.txt').exists())


if __name__ == '__main__':
    unittest.main()
