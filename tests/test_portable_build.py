"""Release privacy check allows dependency assets and rejects user state."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('portable_build',Path(__file__).resolve().parents[1]/'scripts/build_portable.py')
build=importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class PortableBuildTests(unittest.TestCase):
    def test_ocr_source_notice_matches_built_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'source';source.mkdir()
            bundle=root/'bundle';bundle.mkdir()
            manifest=source/'runtime.json';manifest.write_text('{}',encoding='utf-8')
            with patch.object(build,'ROOT',source), \
                 patch.object(build.ocr_runtime,'MANIFEST_PATH',manifest), \
                 patch.object(build.ocr_runtime,'load_manifest',return_value=dict(runtime='fixture',selection='fixture')), \
                 patch.object(build.ocr_runtime,'source_identity',return_value=dict(url='https://example.invalid/source',sha256='a'*64)):
                build.copy_ocr_licenses(bundle,version='9.8.7')
            notice=(bundle/'OCR-RUNTIME.txt').read_text(encoding='utf-8')
            self.assertIn('/releases/tag/v9.8.7',notice)
            self.assertIn('LockLingo-9.8.7-ocr-sources.zip',notice)
            self.assertNotIn('beta.1',notice)

    def test_dependency_data_is_allowed_but_user_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'_internal/cv2/data').mkdir(parents=True)
            build.check_contents(root)
            (root/'data').mkdir()
            with self.assertRaises(ValueError):build.check_contents(root)

    def test_nested_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'_internal/config.json').parent.mkdir()
            (root/'_internal/config.json').write_text('{}',encoding='utf-8')
            with self.assertRaises(ValueError):build.check_contents(root)


if __name__=='__main__':unittest.main()
