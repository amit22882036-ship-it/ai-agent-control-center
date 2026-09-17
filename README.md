# AI Agent Control Center

A local dashboard for starting, stopping, and monitoring multiple agents. The React + Vite frontend connects to a FastAPI backend running with Uvicorn, which manages agent subprocesses and captures their output.

## Features

- **Mock agents:** simulate a short workflow with progress messages. They do not execute the supplied task or change files.
- **Codex agents:** run tasks through the installed Codex CLI, using this repository's root as the working directory.
- **Codex sandbox modes:** `read-only` is the default; `workspace-write` allows Codex to modify files in this project. Choose the mode before starting a Codex agent.
- **Start and stop:** enter a task and select an agent type to start a run. Select an agent to view its details and stop it while it is running.
- **Status monitoring and live output:** agent cards and selected-agent details refresh every two seconds, showing `running`, `finished`, or `stopped` status and captured standard output/error.

Agent records and output are held in backend memory and are not persisted across backend restarts. `finished` means the process exited; it does not distinguish success from failure.

## Requirements

- Python 3.10 or newer.
- Node.js 22.13+ within the 22.x release line, or Node.js 24+, with npm, for the frontend tooling.
- For Codex agents, Windows with the npm-installed Codex CLI at `%APPDATA%\npm\codex.cmd`, configured and authenticated for use. The current backend supports Codex agents only through this Windows launcher. Mock agents do not require Codex.

## Start the backend

From the repository root, run in Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

On macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd backend
../.venv/bin/python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/health> to check the backend. It returns:

```json
{"status": "ok"}
```

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## Start the frontend

Keep the backend running. In a second terminal, starting from the repository root:

```sh
cd frontend
npm install
npm run dev -- --port 5173 --strictPort
```

Open <http://localhost:5173>. The frontend calls the backend at `http://127.0.0.1:8000`; the backend allows frontend requests from `localhost:5173` and `127.0.0.1:5173`.

## Use the dashboard

1. Select **Mock** or **Codex** under **Agent Type**.
2. For Codex, choose **Read only** or **Workspace write** under **Sandbox**.
3. Enter a task and click **Start Agent**. Repeat to run multiple agents.
4. Click an agent's name to view its status and live output.
5. Click **Stop** in the details panel to stop a running agent.
