# Run from source

For development, run the backend and the interface separately. The commands below are PowerShell. Run them in order: each block assumes the directory left by the previous one.

## 1. Prerequisites

- **Windows 10 or 11.** Agent launching and process supervision are Windows-only for now; on other platforms launches are refused with `Process supervision unavailable`.
- **Git**, on `PATH`, with `user.name` and `user.email` configured.
- **Python 3.11 or newer**, available as `python`.
- **Node.js 22.13+ or 24+**, with npm.
- **For real agents only:** an account that can use the provider CLI you choose. Mock mode needs no account.

Check what you have:

```powershell
git --version
python --version
node --version
npm --version
```

## 2. Clone the repository

Choose a parent folder, for example your home directory:

```powershell
cd $HOME
git clone https://github.com/amit22882036-ship-it/ai-agent-control-center.git
cd .\ai-agent-control-center
```

The rest of the setup runs from this repository root.

## 3. Create the Python virtual environment

```powershell
python -m venv .venv
```

The commands below call `.venv\Scripts\python.exe` directly, so you do not need to activate the environment.

## 4. Install backend dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

## 5. Install frontend dependencies

```powershell
cd .\frontend
npm ci
cd ..
```

## 6. Install and sign in to a provider CLI (real agents only)

The Control Center uses whatever account is signed in on **your** machine. Model usage is billed to, or counted against, your own plan. For Codex:

```powershell
npm install -g @openai/codex
codex login
codex login status
```

`codex login` opens a browser so you can sign in. The backend launches Codex through npm's global launcher at `%APPDATA%\npm\codex.cmd`, which is where `npm install -g` puts it by default; if you changed npm's global prefix, real agents report that Codex was not found.

Claude Code (`%USERPROFILE%\.local\bin\claude.exe` or `%APPDATA%\npm\claude.cmd`) and Gemini CLI (`%APPDATA%\npm\gemini.cmd`) are installed and signed in with their own official instructions, or from **Connections** in the rail: it shows what is missing and offers reviewed install, update, sign-in and sign-out operations that run only after you approve them (Gemini CLI's sign-in and Claude Code's native installer stay manual steps). Connections is also where you choose which providers agents may use. Their supported sign-in routes and billing checks are described in [providers](providers.md).

No Control Center configuration is required. Optional settings are listed in [configuration](configuration.md).

## 7. Start the backend (window 1)

Open a PowerShell window in the repository root and keep it running:

```powershell
cd .\backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

When Uvicorn prints `Application startup complete`, the API is ready. You can confirm it at <http://127.0.0.1:8000/health>.

## 8. Start the frontend (window 2)

Open a **second** PowerShell window in the repository root and keep it running:

```powershell
cd .\frontend
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

## 9. Open the interface

Go to <http://127.0.0.1:5173>.

| Port | What runs there | Used for |
| --- | --- | --- |
| `5173` | Vite dev server for the React interface | The page you open in the browser |
| `8000` | FastAPI backend | REST API, live events, local API docs at `/docs` |

In development the interface calls the backend at `127.0.0.1:8000`, so the backend must run on that port. Both servers listen only on `127.0.0.1`. The interface is **Lumen**; it is the only one (the earlier classic interface was removed once Lumen covered everything it did).

## 10. Register an existing Git project

Agents work on a registered Project, which must be an existing Git repository with at least one commit. For a first try, create a small sandbox repository in a third window:

```powershell
mkdir $HOME\control-center-sandbox
cd $HOME\control-center-sandbox
git init
Set-Content README.md "# Sandbox"
git add README.md
git commit -m "Initial commit"
```

In **New task**, choose **Add a project…** in the project list, enter a name and the repository's absolute path, for example `C:\Users\<you>\control-center-sandbox`, and register it.

If the folder isn't a Git repository yet, or has no commits, the dialog says so and offers **Initialize Git and create first commit…** (or **Create the first commit…**). It first shows exactly what it will write: the file count, any dependency folders or large files, and the Git identity that will make the commit. Nothing is written until you confirm.

Agents never edit this folder directly. Each Task works in its own snapshot under `%LOCALAPPDATA%\AI Agent Control Center\task-workspaces`, and changes come back only through integration.

## 11. Start a Mock agent

In **New task**, select your Project and the **Mock** tool, enter any short request and start. Mock agents simulate progress and output so you can try Stop, the output view and the Work Map. They do not run the request and use no model.

## 12. Start a real agent

In **New task**, choose a ready tool, optionally a model and effort, and keep **Read only** access for a first run. Leave **Let the lead bring in helpers** off unless you want the agent to be able to start helper agents: autonomous runs can use noticeably more model usage. Enter a request in plain language, for example:

```text
Summarize what this repository contains and suggest one small improvement.
```

Choose **Workspace** only when you want the agent to change files in its isolated Task workspace, and **Full Access** only when you accept what the page says it allows for that tool.

## Development launcher

To try the current code without building an installer, double-click `scripts\start-dev.cmd` (or run
`.\scripts\start-dev.ps1` from the repository root in PowerShell). It replaces steps 7 to 9:

- Two windows open, **Control Center Dev - backend** (port 8100) and **Control Center Dev - interface**
  (port 5180), then the browser opens http://127.0.0.1:5180. The interface shows a **Dev** badge and
  its window title starts with "Dev".
- Its data is separate from the installed app: the development database `data\control_center.sqlite3`
  (git-ignored) and Task workspaces and Trash under `%LOCALAPPDATA%\AI Agent Control Center (Dev)`. The
  installed app's data is never read or changed. Its ports do not clash with the installed app, but do
  not work on the same project in both at once.
- Real provider CLIs work as in the installed app, with your own sign-in and usage; Connections
  settings belong to each database, so confirm them once in the Dev version. The local-work rule and
  provider guards are the same code.
- After a change: the interface reloads by itself. For a backend change, close the backend window and
  run the launcher again (the backend does not reload by itself, because a reload stops running agents).
- Close both windows to stop. On first run the launcher creates the Python environment and installs
  the interface packages if they are missing.

## Stopping and restarting

Press `Ctrl+C` in each window, then run the commands from steps 7 and 8 again. Stopping the backend also stops any running agents. History stays in the database, but running work does not resume automatically.

If the backend window ever had environment variables set for an experiment, such as `CONTROL_CENTER_DB_PATH` for a test database or the delegation limits, the backend will use those settings until they are cleared. Before restarting the backend for normal use, check:

```powershell
Get-ChildItem Env:CONTROL_CENTER_*
```

Clear any you did not intend to keep, for example `Remove-Item Env:CONTROL_CENTER_DB_PATH`, or open a new PowerShell window. [Troubleshooting](configuration.md#troubleshooting) covers port conflicts and other common problems. The development backend and the desktop app use different databases (`data\control_center.sqlite3` and `%LOCALAPPDATA%\AI Agent Control Center\control_center.sqlite3`).

## Checks

Run the checks from the repository root:

```powershell
cd .\backend
..\.venv\Scripts\python.exe -B -m unittest discover -s . -p "test_*.py"
cd ..\frontend
node --test tests/*.test.mjs
npm run lint
npm run build
cd ..
git diff --check
```

The full backend suite takes about 30 minutes on Windows. It uses temporary databases, repositories and fake provider CLIs and never starts a real provider. For backend development you can add `--reload` to the Uvicorn command. Do not use it while agents are running, because a reload restarts the backend and stops them.

[AGENTS.md](../AGENTS.md) holds the repository rules that coding agents follow when working on this project.
