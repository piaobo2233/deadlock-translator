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
    def test_verified_bundle_is_accepted_and_bad_inputs_are_rejected(self):
        original = {
            'DeadlockTranslate.exe': b'executable fixture', 'VERSION': b'1.0.0-test.4\n',
            'ocr/tesseract.exe': b'ocr', 'ocr/libtesseract-5.dll': b'dll',
            '_internal/app/assets/brand/locklingo.ico': b'icon',
        }
        for variant in ('valid', 'changed', 'extra', 'private', 'traversal'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = dict(original)
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
