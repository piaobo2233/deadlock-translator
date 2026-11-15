"""Release privacy check allows dependency assets and rejects user state."""
import importlib.util
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.platform.update_install import program_files
from app.platform.updates import UpdateError

spec=importlib.util.spec_from_file_location('portable_build',Path(__file__).resolve().parents[1]/'scripts/build_portable.py')
build=importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class PortableBuildTests(unittest.TestCase):
    def build_fixture(self, root, *, unexpected=False):
        (root/'VERSION').write_text('1.2.1',encoding='utf-8')
        for folder in ('app', 'gui', 'scripts/installer/hooks'):
            (root/folder).mkdir(parents=True)
        for name in ('LICENSE', 'scripts/run.py', 'docs/BRAND.md', 'docs/PROJECT-NOTICE.md', 'docs/LICENSE-SCOPE.md',
                     'docs/THIRD-PARTY-NOTICES.md', 'docs/guides/首版测试与分享.md',
                     'docs/releases/20261114-license-migration-audit.md',
                     'app/assets/brand/locklingo.ico'):
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(name,encoding='utf-8')

        def freeze(command, **kwargs):
            bundle=Path(command[command.index('--distpath')+1])/'DeadlockTranslate'
            bundle.mkdir(parents=True)
            for name in ('LockLingo.exe', 'DeadlockTranslate.exe', '_internal/cv2/data/cascade.xml'):
                path=bundle/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
            if unexpected:
                (bundle/'unexpected.txt').write_bytes(b'not accepted by old updater')

        with ExitStack() as stack:
            for target, name, value in (
                (build, 'ROOT', root), (build, 'BRAND_ICON', root/'app/assets/brand/locklingo.ico'),
                (sys, 'argv', ['build_portable.py','--no-zip','--version','1.2.1']),
            ):
                stack.enter_context(patch.object(target,name,value))
            stack.enter_context(patch.object(build.release_gate,'validate',return_value={'distribution':'local_test_only'}))
            stack.enter_context(patch.object(build.release_gate,'bundled_game_files',return_value=[]))
            stack.enter_context(patch.object(build.ocr_runtime,'validate',return_value={}))
            stack.enter_context(patch.object(build.ocr_runtime,'copy_into'))
            stack.enter_context(patch.object(build,'copy_ocr_licenses'))
            stack.enter_context(patch.object(build,'copy_licenses'))
            stack.enter_context(patch.object(build.subprocess,'run',side_effect=freeze))
            build.main()
        return root/'dist/LockLingo-1.2.1-windows-x64'

    def test_built_notices_are_compatible_with_old_update_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle=self.build_fixture(Path(directory))
            program_files(bundle)
            self.assertFalse((bundle/'docs').exists())
            for name in ('BRAND.md', 'PROJECT-NOTICE.md', 'THIRD-PARTY-NOTICES.md'):
                self.assertEqual((bundle/'licenses/project'/name).read_text(encoding='utf-8'),'docs/'+name)
            self.assertFalse(any('license-migration-audit' in p.name for p in bundle.rglob('*')))

    def test_build_rejects_incompatible_manifest_before_publishing_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(UpdateError):
                self.build_fixture(root,unexpected=True)
            self.assertFalse((root/'dist/LockLingo-1.2.1-windows-x64').exists())

    def test_top_level_docs_reproduce_the_install_commit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'docs/BRAND.md';path.parent.mkdir();path.write_bytes(b'notice')
            (root/'manifest.json').write_text(json.dumps([{'path':'docs/BRAND.md','sha256':build.digest(path)}]))
            build.check_contents(root)
            with self.assertRaisesRegex(UpdateError,'无效路径'):
                program_files(root)

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
