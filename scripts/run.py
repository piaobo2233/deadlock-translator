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
            if sys.argv[1:2] not in (['--ocr-worker'], ['--inspect-game-resources'], ['--watch-game']):
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, '锁语正在更新，请稍后重新打开。', '锁语更新', 0)
            return 1
        from app.platform.single_instance import retain_install_guard
        retain_install_guard()
        recovery = Path(sys.executable).parent / 'backups' / 'rollback'
        if mutex_exists(UPDATE_MUTEX) or any((recovery/name).exists() for name in ('pending.json','restore.json')):
            if sys.argv[1:2] not in (['--ocr-worker'], ['--inspect-game-resources'], ['--watch-game']):
                import ctypes
                ctypes.windll.user32.MessageBoxW(None,
                    '更新或恢复尚未完成。请使用开始菜单中的回退入口完成恢复后再打开锁语。', '锁语恢复', 0)
            return 1
    if sys.argv[1:2] == ['--inspect-game-resources']:
        if len(sys.argv) != 3:
            raise SystemExit('Invalid game resource inspection arguments')
        from app.game_data.service import inspection_worker
        return inspection_worker(sys.argv[2])
    # The resident helper must keep stdout as its JSON protocol, without
    # starting a second translator, creating user data or installing a GUI.
    if sys.argv[1:2] == ['--ocr-worker']:
        if len(sys.argv) != 6:
            raise SystemExit('Invalid OCR helper arguments')
        from app.recognition.resident import worker
        worker(*sys.argv[2:])
        return 0
    if sys.argv[1:2] == ['--watch-game']:
        if len(sys.argv) != 2:
            raise SystemExit('Usage: --watch-game')
        from app.platform.game_autostart import run as watch_game
        return watch_game()
    if sys.argv[1:2] == ['--prepare-game-resources']:
        if len(sys.argv) != 3:
            raise SystemExit('Usage: --prepare-game-resources GAME_DIRECTORY')
        import json
        from app.game_data.service import prepare
        from app.paths import application_root
        bundle = prepare(application_root(), sys.argv[2])
        print(json.dumps(dict(build_id=bundle['snapshot']['build_id'],
                              terms=len(bundle['terms']['terms']),
                              messages=len(bundle['quick_chat']['messages']), restart_required=True)))
        return 0
    from app.main import run as run_application
    return run_application()

if __name__ == '__main__':
    raise SystemExit(run())
