"""Repository closure selection and hostile lock rejection without network or package execution."""
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_msys2_ocr as prepare
from scripts import complete_msys2_ocr as complete


class MsysOcrTests(unittest.TestCase):
    def test_source_evidence_keeps_recipe_bytes_and_all_regular_inputs(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:') as archive:
            for name, body in (('source/PKGBUILD', b'fixture recipe\r\n'), ('source/patch.diff', b'fixture patch')):
                entry = tarfile.TarInfo(name); entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
        self.assertEqual(dict(complete.members(stream.getvalue())),
                         {'source/PKGBUILD': b'fixture recipe\r\n', 'source/patch.diff': b'fixture patch'})
    def test_dependency_closure_uses_unambiguous_provider_and_terminates_cycles(self):
        packages = {'ocr': {'DEPENDS': ['runtime>=1']},
                    'gcc': {'PROVIDES': ['runtime=2'], 'DEPENDS': ['ocr']}}
        self.assertEqual(set(prepare.select_packages(packages, 'ocr')), {'ocr', 'gcc'})
        packages['other'] = {'PROVIDES': ['runtime=3']}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            prepare.select_packages(packages, 'ocr')
        with self.assertRaises(ValueError):
            prepare.select_packages({}, 'ocr')

    def test_repository_duplicate_names_are_rejected(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:') as archive:
            for name in ('a/desc', 'b/desc'):
                body = b'%NAME%\nsame\n\n%VERSION%\n1-1\n'
                entry = tarfile.TarInfo(name); entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            prepare.repository_packages(stream.getvalue())

    def test_all_lock_entries_are_validated_before_any_file_or_network_action(self):
        row = dict(name='package', filename='package.pkg.tar.zst', bytes=42,
                   sha256='a' * 64, url=prepare.REPOSITORY + 'package.pkg.tar.zst')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for changed in (dict(name='../escape'), dict(filename='../escape.pkg.tar.zst'),
                            dict(url='https://invalid.local/evil'), dict(bytes=-1), dict(sha256='wrong')):
                with self.subTest(changed=changed), patch.object(prepare, 'download_package') as download:
                    with self.assertRaises(ValueError):
                        prepare.stage(dict(schema_version=1, packages=[row, dict(row, **changed)]),
                                      root / 'cache', root / 'output', root / '7z', root / 'model')
                    download.assert_not_called()
                    self.assertFalse((root / 'output').exists())

    def test_cached_corruption_does_not_trigger_a_new_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'package.pkg.tar.zst'; path.write_bytes(b'bad')
            row = dict(name='fixture',filename=path.name,bytes=3,sha256='a'*64)
            with patch.object(prepare.urllib.request, 'urlopen') as network:
                with self.assertRaisesRegex(ValueError, 'Cached package'):
                    prepare.download_package(row, root)
                network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
