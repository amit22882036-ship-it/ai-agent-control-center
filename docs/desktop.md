# Windows desktop app

The desktop app is Electron around the existing backend. The backend is packaged with PyInstaller
(`ControlCenterBackend.exe`) and serves the interface itself, so the window, the API and the live
updates share one local origin on a free `127.0.0.1` port. Nothing else listens.

## Why Electron with a packaged Python backend

The backend's process supervision (Windows Job Objects), provider discovery and SQLite stay exactly
as they are; the shell only starts and stops one child process. Electron gives a stable Chromium
for the interface, a native folder picker, a tray and an NSIS installer from one build, without a
Rust toolchain. The cost is installer size (about 100 MB).

## Behaviour

- **Single instance**: a second launch focuses the existing window.
- **Startup**: a splash while the service starts; if it fails, a message names the log folder.
- **No console windows**: the backend runs with a hidden console that its agents and Git inherit.
- **Closing**: with agents working, the app asks whether to keep them working in the background
  (tray icon) or quit. Quitting asks the backend to shut down (agents stopped and recorded, the
  database closed). If the app itself ends, the backend sees its input close and shuts down too.
- **Links**: anything that is not the local interface opens in the default browser.

## Data

| What | Where |
| --- | --- |
| Database | `%LOCALAPPDATA%\AI Agent Control Center\control_center.sqlite3` |
| Task worktrees | `%LOCALAPPDATA%\AI Agent Control Center\task-workspaces` |
| Trash | `%LOCALAPPDATA%\AI Agent Control Center\Trash` |
| Logs | `%LOCALAPPDATA%\AI Agent Control Center\logs` |
| Demo mode (`--demo`) | `…\AI Agent Control Center\demo\` (separate database) |

Uninstalling removes the program, shortcuts and registry entry and keeps this data. The desktop
app does not read the development database in `data\`.

Provider CLIs (Claude Code, Codex, Gemini CLI) are not bundled: install and sign in to them as
usual; the app finds them in the same places as the development backend.

## Build

From the repository root in PowerShell: `.\desktop\build.ps1`. It builds the interface
(`npm run build:desktop`), a separate Python environment in `desktop\.venv` with PyInstaller, the
packaged backend, and the installer in `desktop\out\`. The installer is unsigned, so Windows
SmartScreen warns before the first run.

## Verified on this machine (2026-11-05)

Silent install and uninstall (`/S`), Start menu and desktop shortcuts, launch, process supervision
reported as `supervised`, Mock agents started and finished, data kept after reinstalling over the
previous version and relaunching, a clean shutdown with no backend left running.
