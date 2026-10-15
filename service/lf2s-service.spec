# Relocatable build: SPECPATH is this service directory, no workspace hardcoding.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
a = Analysis([str(root / 'service/entry.py')],
             pathex=[str(root), str(root / 'protocol')],
             datas=[(str(root / 'package.json'), '.'),
                    (str(root / 'simulation/profile.json'), 'simulation'),
                    (str(root / 'protocol/manifest.json'), 'protocol')],
             hiddenimports=collect_submodules('uvicorn'), excludes=['tkinter', 'pytest'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='lf2s-service',
          console=True, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='lf2s-service')
