import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_batch_files_use_windows_line_endings(self):
        for path in list(ROOT.glob("*.bat")) + list((ROOT / "scripts").glob("*.bat")):
            content = path.read_bytes()
            self.assertNotIn(b"\n", content.replace(b"\r\n", b""), str(path))
            self.assertTrue(content.startswith(b"@echo off\r\n"), str(path))

    @unittest.skipUnless(os.name == "nt", "cmd.exe is Windows-only")
    def test_batch_entry_preserves_success_and_failure_exit_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "中文 test project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "跑测试.bat", scripts / "跑测试.bat")
            for code in (0, 7):
                (scripts / "check.py").write_text(f"raise SystemExit({code})\n", encoding="utf-8")
                result = subprocess.run(["cmd", "/d", "/c", str(scripts / "跑测试.bat")],
                                        cwd=project, env=dict(os.environ, CI="1"),
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(b"Regression tests", result.stdout)

    def test_source_and_frozen_paths_do_not_follow_module_move(self):
        from unittest.mock import patch
        from app.paths import application_root
        from app.storage import pending, usage
        self.assertEqual(Path(application_root()), ROOT)
        self.assertEqual(Path(usage._app_dir()), ROOT)
        executable = str(ROOT / "test-dist" / "DeadlockTranslate.exe")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", executable):
            self.assertEqual(application_root(), str(ROOT / "test-dist"))
            self.assertEqual(usage._app_dir(), str(ROOT / "test-dist"))
            with patch.object(pending.PendingReview, "_load"), patch.object(
                    pending.PendingReview, "_stamp", return_value=None):
                self.assertEqual(Path(pending.PendingReview().path),
                                 ROOT / "test-dist" / "data" / "pending.json")

    def test_application_packages_do_not_depend_on_legacy_entry_or_gui(self):
        forbidden = {"deadlock_translate", "deadlock_ocr", "gui", "tkinter"}
        independent = ("config", "storage", "runtime", "translation", "vision", "presentation")
        for path in (p for name in independent for p in (ROOT / "app" / name).rglob("*.py")):
            if path.name in {"glossary_ops.py", "memory_ops.py"}:
                continue  # Existing UI operations depend on the application assembly.
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertNotIn(name.split(".")[0], forbidden, str(path))
                    self.assertNotIn(name, {"app.main", "app.recognition.ocr"}, str(path))

    def test_source_entry_runs_from_an_unrelated_working_directory(self):
        # Exercise the real boot path in a private installation.
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "中文 program"
            project.mkdir()
            for name in ("app", "scripts"):
                shutil.copytree(ROOT / name, project / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
            demo = Path(folder) / "input.txt"
            demo.write_text("10/14 10:00:00 [Team] Tester: gg\n", encoding="utf-8")
            entries = (([str(project / "scripts/run.py")], folder), (["-m", "app"], project))
            for entry, working in entries:
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", *entry, "--simulate", str(demo),
                     "--offline", "--headless", "--once", "--no-panel"],
                    cwd=working, env=dict(os.environ, DEEPSEEK_API_KEY="", PYTHONPATH=""),
                    capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[TRANSLATE]", result.stdout)
            self.assertFalse((Path(folder) / "data").exists())
            self.assertFalse((project / "app/data").exists())
            self.assertFalse((project / "scripts/data").exists())

    def test_config_import_has_no_environment_or_filesystem_side_effect(self):
        script = (
            "from pathlib import Path; import sys; "
            "from app.config.repository import ConfigRepository; "
            "from app.runtime.pipeline import TranslationPipeline; "
            "assert 'app.main' not in sys.modules; "
            "assert 'tkinter' not in sys.modules; "
            "assert list(Path('.').iterdir()) == []"
        )
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                    cwd=folder, env=env, capture_output=True, text=True,
                                    encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_headless_once_smoke_uses_disposable_project_not_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            # Copy source only: never copy data/, keys, logs, or game captures.
            for path in ROOT.glob("*.py"):
                shutil.copy2(path, project / path.name)
            shutil.copytree(ROOT / "app", project / "app",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(ROOT / "VERSION", project / "VERSION")
            demo = project / "input.txt"
            demo.write_text("10/08 21:40:00 [Team] Tester: gg\n"
                            "10/08 21:40:01 [Team] Tester: omw\n", encoding="utf-8")
            script = (
                "import sys, urllib.request; import app.main as dt; "
                "dt.single_instance_guard=lambda: (1, True); "
                "urllib.request.urlopen=lambda *a, **k: (_ for _ in ()).throw(AssertionError('Network disabled')); "
                "sys.argv=['deadlock_translate.py','--simulate','input.txt','--headless','--once','--offline']; "
                "sys.exit(dt.main())"
            )
            env = dict(os.environ, DEEPSEEK_API_KEY="", PYTHONPATH=str(project))
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                    cwd=project, env=env, capture_output=True, text=True,
                                    encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[done]", result.stdout)
            self.assertIn("[TRANSLATE]", result.stdout)
            self.assertIn("词汇库", result.stdout)
            self.assertNotIn("[error]", result.stdout)
