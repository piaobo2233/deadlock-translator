"""Interpret the real UTF-16 VBS launcher without starting a translator or network."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows Script Host')
class SourceLauncherTests(unittest.TestCase):
    def test_project_pythonw_is_chosen_before_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '中文 空格'
            (root / 'scripts').mkdir(parents=True)
            (root / 'scripts/run.py').write_text('# fixture\n', encoding='utf-8')
            runtime = root / '.venv/Scripts/pythonw.exe'
            runtime.parent.mkdir(parents=True)
            runtime.touch()  # Selection fixture only; the executable is never run.
            source = (ROOT / '启动.vbs').read_text(encoding='utf-16')
            lines = source.splitlines()
            calls = [index for index, line in enumerate(lines) if line.startswith('shell.Run ')]
            self.assertEqual(len(calls), 1)
            # Some Windows hosts discard CScript stdout; inspect a Unicode file instead.
            lines[calls[0]] = ('Dim selectedFile\n'
                              'Set selectedFile = fso.CreateTextFile(root & "\\selected.txt", True, True)\n'
                              'selectedFile.Write pythonw\nselectedFile.Close')
            launcher = root / '启动.vbs'
            launcher.write_text('\n'.join(lines), encoding='utf-16')
            cscript = Path(os.environ['SystemRoot']) / 'System32/cscript.exe'
            result = subprocess.run([str(cscript), '//nologo', '//U', str(launcher)], cwd=root,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-16-le', errors='replace'))
            self.assertEqual((root / 'selected.txt').read_text(encoding='utf-16'), str(runtime))


if __name__ == '__main__':
    unittest.main()
