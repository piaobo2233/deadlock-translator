"""Share one frozen payload between the GUI and pipe-capable CLI/OCR helper."""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

source = Path(os.environ['LOCKLINGO_BUILD_SOURCE'])
icon = str(source / 'app/assets/brand/locklingo.ico')
game_data = [(str(source / relative), str(Path(relative).parent))
             for relative in ('app/translation/game_terms.json', 'app/recognition/quick_chat.json')
             if (source / relative).is_file()]
analysis = Analysis(
    [str(source / 'scripts/run.py')], pathex=[str(source)],
    binaries=[],
    datas=[(str(source / 'VERSION'), '.'),
           (str(source / 'app/assets'), 'app/assets'),
           *game_data,
           (str(source / 'app/recognition/models'), 'app/recognition/models'),
           *collect_data_files('ttkbootstrap')],
    hiddenimports=['pystray._win32'], hookspath=[str(source.parent / 'hooks')],
    hooksconfig={}, runtime_hooks=[],
    excludes=['pandas', 'scipy', 'matplotlib', 'IPython', 'pytest'],
    noarchive=False, optimize=0,
)
pyz = PYZ(analysis.pure)
cli = EXE(pyz, analysis.scripts, [], exclude_binaries=True,
          name='DeadlockTranslate', console=True, strip=False, upx=False, icon=icon)
gui = EXE(pyz, analysis.scripts, [], exclude_binaries=True,
          name='LockLingo', console=False, strip=False, upx=False, icon=icon)
bundle = COLLECT(gui, cli, analysis.binaries, analysis.datas,
                 name='DeadlockTranslate', strip=False, upx=False)
