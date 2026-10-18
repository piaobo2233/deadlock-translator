"""Source, autostart and PyInstaller entry independent of working directory."""
from pathlib import Path
import sys

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def run():
    if getattr(sys, 'frozen', False):
        from app.platform.single_instance import retain_install_guard
        retain_install_guard()
    # The resident helper must keep stdout as its JSON protocol, without
    # starting a second translator, creating user data or installing a GUI.
    if sys.argv[1:2] == ['--ocr-worker']:
        if len(sys.argv) != 6:
            raise SystemExit('Invalid OCR helper arguments')
        from app.recognition.resident import worker
        worker(*sys.argv[2:])
        return 0
    from app.main import run as run_application
    return run_application()

if __name__ == '__main__':
    raise SystemExit(run())
