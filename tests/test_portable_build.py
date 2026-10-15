"""Release privacy check allows dependency assets and rejects user state."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('portable_build',Path(__file__).resolve().parents[1]/'scripts/build_portable.py')
build=importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class PortableBuildTests(unittest.TestCase):
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
