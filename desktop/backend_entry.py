"""The packaged backend's entry point. The desktop app starts it; people never run it directly.

The desktop app chooses the port and the per-user data locations and passes them in the
environment before this module imports the application. It ends the backend by writing
`shutdown` to its standard input, or by closing that input when the app itself ends, so the
backend's own shutdown (agents stopped and recorded, the database closed) always runs and no
backend is left behind.

The same executable also runs the Mock agent: the backend starts Mock work as
`<python> mock_agent.py`, and in the packaged app `<python>` is this executable."""
import os
from pathlib import Path
import runpy
import sys
import threading


def run_mock(script):
    runpy.run_path(script, run_name='__main__')


def main():
    if len(sys.argv) > 1 and Path(sys.argv[1]).name.lower() == 'mock_agent.py':
        run_mock(sys.argv[1])
        return
    if '-c' in sys.argv[1:]:
        # Python's `-c code` form: the process supervision self-test starts `<python> -I -S -c pass`
        # under a Job Object to prove containment works before any agent may launch.
        exec(compile(sys.argv[sys.argv.index('-c') + 1], '<command>', 'exec'), {'__name__': '__main__'})
        return
    port = int(os.environ.get('CONTROL_CENTER_PORT') or '8765')
    # Processes this executable starts again (the Mock agent, the supervision self-test) must start
    # as independent programs, not as part of this one (PyInstaller's documented reset switch).
    os.environ['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    import uvicorn
    from app.main import app
    # Open live-update streams (the window's event connection) never hold a shutdown open: after a
    # short grace period they are closed and the application's own shutdown runs.
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='info', access_log=False, lifespan='on',
                            timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)

    # The parent's pipe is read through a private duplicate, and this process's standard input
    # becomes the null device. Children inherit standard input; on Windows a child that touches a
    # pipe this process is reading from can block at startup, so they get the null device instead.
    parent = os.fdopen(os.dup(0), 'r', encoding='utf-8', errors='replace')
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    if os.name == 'nt':
        import ctypes
        import msvcrt
        ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))  # STD_INPUT_HANDLE

    def watch_parent():
        try:
            for line in parent:
                if line.strip() == 'shutdown':
                    break
        except (OSError, ValueError):
            pass
        server.should_exit = True

    threading.Thread(target=watch_parent, name='desktop-parent', daemon=True).start()
    server.run()


if __name__ == '__main__':
    main()
