# PyInstaller recipe for the packaged backend (one folder, console subsystem: the desktop app
# starts it hidden, and its agents and Git inherit that hidden console instead of opening windows).
# Build: desktop\build.ps1 (it creates the build environment and runs this from backend\).
from pathlib import Path

backend = Path(SPECPATH).resolve().parents[0] / 'backend'
block_cipher = None

a = Analysis(
    [str(Path(SPECPATH) / 'backend_entry.py')],
    pathex=[str(backend)],
    datas=[(str(backend / 'mock_agent.py'), '.')],
    hiddenimports=['app.main', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
                   'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on'],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ControlCenterBackend', console=True,
          icon=str(Path(SPECPATH) / 'assets' / 'icon.ico'))
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name='ControlCenterBackend')
