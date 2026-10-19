"""Source, autostart and PyInstaller entry independent of working directory."""
from pathlib import Path
import sys

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def run():
    if sys.argv[1:2] == ['--apply-update']:
        if not getattr(sys, 'frozen', False) or len(sys.argv) != 3:
            raise SystemExit('Invalid update helper arguments')
        from app.platform.update_install import run as apply_update
        return apply_update(sys.argv[2])
    if getattr(sys, 'frozen', False):
        from app.platform.update_install import mutex_exists, UPDATE_MUTEX
        if mutex_exists(UPDATE_MUTEX):
            if sys.argv[1:2] != ['--ocr-worker']:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, '锁语正在更新，请稍后重新打开。', '锁语更新', 0)
            return 1
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
