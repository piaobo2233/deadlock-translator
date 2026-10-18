"""Brand assets, resource paths and the pinned OCR runtime manifest."""
import hashlib
import json
import unittest
from pathlib import Path

from app import brand
from app.paths import resource_root

ROOT = Path(__file__).resolve().parents[1]
ICON_SHA256 = '14f1933681f917163f7e30b0a364a6e3eb6543f7552226ed6e1792a75bbcea68'


class BrandAssetTests(unittest.TestCase):
    def test_user_visible_names_are_centralized(self):
        self.assertEqual(brand.NAME_CN, '锁语')
        self.assertEqual(brand.NAME_EN, 'LockLingo')
        self.assertIn('锁语', brand.PRODUCT)
        self.assertIn('LockLingo', brand.PRODUCT)
        self.assertEqual(brand.WINDOW_TITLE, brand.PRODUCT)
        self.assertIn('异锁', brand.SUBTITLE)

    def test_source_resource_root_is_the_project_root(self):
        self.assertEqual(Path(resource_root()).resolve(), ROOT.resolve())

    def test_brand_files_are_present_and_the_icon_is_the_selected_one(self):
        icon = Path(brand.icon_path())
        self.assertTrue(icon.is_file(), icon)
        self.assertEqual(hashlib.sha256(icon.read_bytes()).hexdigest(), ICON_SHA256)
        self.assertTrue(Path(brand.mark_path()).is_file())
        self.assertTrue(Path(brand.mark_large_path()).is_file())
        image = brand.load_mark_image(32)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (32, 32))

    def test_missing_icon_degrades_instead_of_raising(self):
        from unittest.mock import patch
        with patch('app.brand.asset_dir', return_value=str(ROOT / '.temp' / 'missing-brand')):
            self.assertEqual(brand.icon_path(), '')
            self.assertFalse(brand.apply_window_icon(object()))
            self.assertIsNone(brand.load_mark_image(16))

    def test_window_icon_and_tray_image_use_the_same_artwork(self):
        try:
            import tkinter as tk
            probe = tk.Tk()
        except Exception as exc:
            self.skipTest(f"No display: {type(exc).__name__}")
        try:
            self.assertTrue(brand.apply_window_icon(probe))
        finally:
            probe.destroy()
        from app.platform.tray import make_icon_image
        images = {state: make_icon_image(state, 32).tobytes()
                  for state in ('ok', 'api', 'paused', 'error')}
        self.assertEqual(len(set(images.values())), 4)

    def test_docs_brand_package_is_kept_outside_the_runtime_assets(self):
        docs = ROOT / 'docs' / 'assets' / 'locklingo'
        for name in ('locklingo-master.png', 'locklingo-wordmark-light.png',
                     'locklingo-wordmark-dark.png', 'verification.json', 'README.md'):
            self.assertTrue((docs / name).is_file(), name)
        # 运行期只带必要的小图标，不把整包设计素材塞进程序目录。
        runtime_files = {path.name for path in (ROOT / 'app' / 'assets' / 'brand').iterdir()}
        self.assertNotIn('locklingo-master.png', runtime_files)
        self.assertNotIn('locklingo-wordmark-light.png', runtime_files)


class OcrRuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'ocr_runtime', ROOT / 'scripts' / 'ocr_runtime.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.manifest = self.module.load_manifest()

    def test_manifest_is_pinned_to_the_official_installer(self):
        manifest = self.manifest
        self.assertEqual(manifest['installer']['sha256'], self.module.INSTALLER_SHA256)
        self.assertEqual(manifest['installer']['name'], self.module.INSTALLER_NAME)
        self.assertTrue(manifest['installer']['url'].startswith('https://github.com/tesseract-ocr/'))
        self.assertIn('5.5.3', manifest['runtime'])
        paths = [row['path'] for row in manifest['files']]
        self.assertEqual(len(paths), len(set(paths)))
        for required in ('tesseract.exe', 'libtesseract-5.dll', 'tessdata/eng.traineddata',
                         'libleptonica-6.dll'):
            self.assertIn(required, paths)
        for row in manifest['files']:
            self.assertRegex(row['sha256'], r'^[0-9a-f]{64}$')
            self.assertGreater(row['bytes'], 0)

    def test_runtime_licenses_are_shipped_in_the_repository(self):
        licenses = ROOT / 'docs' / 'licenses' / 'tesseract'
        for name in ('LICENSE', 'AUTHORS', 'LEPTONICA-LICENSE.txt'):
            self.assertTrue((licenses / name).is_file(), name)
        self.assertIn('Apache License', (licenses / 'LICENSE').read_text(encoding='utf-8'))

    def test_staged_runtime_matches_the_manifest_when_present(self):
        staged = ROOT / '.temp' / 'ocr-runtime'
        if not staged.is_dir():
            self.skipTest('OCR runtime is prepared on demand (scripts/ocr_runtime.py prepare)')
        self.module.validate(staged, self.manifest)


class PortableBuildContentTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'portable_build', ROOT / 'scripts' / 'build_portable.py')
        self.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.build)

    def test_ocr_runtime_and_brand_assets_are_allowed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ocr' / 'tessdata').mkdir(parents=True)
            (root / '_internal' / 'app' / 'assets' / 'brand').mkdir(parents=True)
            self.build.check_contents(root)

    def test_user_data_and_credentials_are_still_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            with self.assertRaises(ValueError):
                self.build.check_contents(root)
            (root / 'data').rmdir()
            (root / 'ocr').mkdir()
            (root / 'ocr' / 'config.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                self.build.check_contents(root)

    def test_version_suffix_and_artifact_name_are_branded(self):
        source = (ROOT / 'scripts' / 'build_portable.py').read_text(encoding='utf-8')
        self.assertIn("LockLingo-{args.version}-windows-x64", source)
        self.assertIn('--icon', source)
        self.assertIn('app/assets', source)
        self.assertIn('ocr_runtime.copy_into', source)


if __name__ == '__main__':
    unittest.main()
