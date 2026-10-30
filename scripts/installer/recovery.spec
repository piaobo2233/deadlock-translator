"""Independent one-file Python/Tk runtime; no main-program DLL dependency."""
import os
from pathlib import Path
source = Path(os.environ['LOCKLINGO_BUILD_SOURCE'])
a = Analysis([str(source/'scripts/recover.py')], pathex=[str(source)],
             binaries=[], datas=[], hiddenimports=[], hookspath=[],
             excludes=['cv2','numpy','PIL','ttkbootstrap','pystray','pytest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='LockLingoRecovery',
          console=False, upx=False, icon=str(source/'app/assets/brand/locklingo.ico'))
