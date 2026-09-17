# AI Agent Control Center

A personal project for a local dashboard to monitor and control multiple AI agents running simultaneously. This initial scaffold contains only a minimal Python backend using FastAPI and Uvicorn.

## Start the backend

Install Python 3.10 or newer. From the `ai-agent-control-center` directory, run in Windows PowerShell:

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
