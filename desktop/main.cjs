// AI Agent Control Center for Windows. The window shows the interface that the bundled backend
// serves on a free local port; the backend runs as this app's child process and stops with it.
const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const net = require('node:net')
const path = require('node:path')

const PRODUCT = 'AI Agent Control Center'
// Per-user data, never inside the installation. CONTROL_CENTER_DESKTOP_DATA points it elsewhere (tests).
const DATA_DIR = process.env.CONTROL_CENTER_DESKTOP_DATA || path.join(process.env.LOCALAPPDATA || app.getPath('userData'), PRODUCT)
const LOG_DIR = path.join(DATA_DIR, 'logs')
const DEMO = process.argv.includes('--demo')
let backend = null
let port = null
let window = null
let tray = null
let quitting = false
let backendLog = null

if (!app.requestSingleInstanceLock({ demo: DEMO })) {
  app.quit()
} else {
  app.on('second-instance', () => showWindow())
}

function resources(...parts) {
  return app.isPackaged ? path.join(process.resourcesPath, ...parts) : path.join(__dirname, '..', ...parts)
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port: chosen } = server.address()
      server.close(() => resolve(chosen))
    })
  })
}

function backendCommand() {
  if (app.isPackaged) return { file: resources('backend', 'ControlCenterBackend.exe'), args: [], cwd: resources('backend') }
  // Development: the repository's own virtual environment and sources.
  const root = path.join(__dirname, '..')
  return { file: path.join(root, '.venv', 'Scripts', 'python.exe'), args: ['-B', path.join(__dirname, 'backend_entry.py')],
    cwd: path.join(root, 'backend') }
}

async function startBackend() {
  fs.mkdirSync(LOG_DIR, { recursive: true })
  port = await freePort()
  const data = DEMO ? path.join(DATA_DIR, 'demo') : DATA_DIR
  fs.mkdirSync(data, { recursive: true })
  const interfaceDir = app.isPackaged ? resources('interface') : path.join(__dirname, '..', 'frontend', 'dist-desktop')
  const env = {
    ...process.env,
    CONTROL_CENTER_PORT: String(port),
    CONTROL_CENTER_DB_PATH: path.join(data, 'control_center.sqlite3'),
    CONTROL_CENTER_WORKSPACE_ROOT: path.join(data, 'task-workspaces'),
    CONTROL_CENTER_TRASH_ROOT: path.join(data, 'Trash'),
    CONTROL_CENTER_STATIC_DIR: interfaceDir,
    CONTROL_CENTER_ALLOWED_ORIGINS: `http://127.0.0.1:${port}`,
    CONTROL_CENTER_DESKTOP: '1',
    PYTHONUNBUFFERED: '1',
  }
  if (!app.isPackaged) env.PYTHONPATH = path.join(__dirname, '..', 'backend')
  const command = backendCommand()
  backendLog = fs.createWriteStream(path.join(LOG_DIR, DEMO ? 'backend-demo.log' : 'backend.log'), { flags: 'a' })
  backendLog.write(`\n--- ${new Date().toISOString()} starting on 127.0.0.1:${port}\n`)
  // windowsHide gives the backend a hidden console; its agents and Git inherit it, so no window flashes.
  backend = spawn(command.file, command.args, { cwd: command.cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
  backend.stdout.pipe(backendLog, { end: false })
  backend.stderr.pipe(backendLog, { end: false })
  backend.on('exit', (code) => {
    backendLog?.write(`--- backend exited with ${code}\n`)
    backend = null
    if (!quitting) {
      dialog.showErrorBox(PRODUCT, `The Control Center service stopped unexpectedly (code ${code}). The log is in ${LOG_DIR}.`)
      app.exit(1)
    }
  })
  await waitForHealth()
}

async function waitForHealth(timeout = 45000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (!backend) throw new Error('The Control Center service ended while starting.')
    try {
      const response = await fetch(`http://127.0.0.1:${port}/health`)
      if (response.ok) return
    } catch { /* Not listening yet. */ }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error('The Control Center service did not start in time.')
}

// Demo mode: a separate database with synthetic repositories and Mock agents, so the app can be
// explored without a provider account and without touching real projects. Seeded once.
async function seedDemo() {
  const root = path.join(DATA_DIR, 'demo', 'repositories')
  const marker = path.join(DATA_DIR, 'demo', 'seeded')
  if (fs.existsSync(marker)) return
  const base = `http://127.0.0.1:${port}`
  const token = (await (await fetch(`${base}/security/csrf-token`)).json()).csrf_token
  const call = async (method, route, body) => {
    const response = await fetch(base + route, { method, headers: { 'Content-Type': 'application/json', 'X-Control-Center-CSRF': token },
      body: JSON.stringify(body) })
    if (!response.ok) throw new Error(`${route}: ${response.status}`)
    return response.json()
  }
  const git = (cwd, args) => require('node:child_process').execFileSync('git', args, { cwd, windowsHide: true, stdio: 'ignore' })
  const projects = {}
  for (const [name, file] of [['Release planning', 'src/release.py'], ['Payments service', 'app/ledger.py'], ['Docs site', 'docs/index.md']]) {
    const folder = path.join(root, name)
    fs.mkdirSync(path.dirname(path.join(folder, file)), { recursive: true })
    fs.writeFileSync(path.join(folder, 'README.md'), `# ${name}\n\nA demo repository created by AI Agent Control Center.\n`)
    fs.writeFileSync(path.join(folder, file), '# Demo file\n')
    git(folder, ['init', '-q'])
    git(folder, ['add', '.'])
    git(folder, ['-c', 'user.name=Demo', '-c', 'user.email=demo@localhost', '-c', 'commit.gpgSign=false', 'commit', '-q', '-m', 'Initial commit'])
    projects[name] = (await call('POST', '/projects', { name, root_path: folder })).project_id
  }
  const start = (task, project) => call('POST', '/agents/start', { task, agent_type: 'mock', project_id: project })
  const lead = await start('Build the CSV import pipeline\nParse, validate and import customer CSV files.', projects['Release planning'])
  for (const helper of ['Handle quoted fields and embedded newlines', 'JSON command-line interface', 'HTML summary report']) {
    await call('POST', `/agents/${lead.agent_id}/children/start`, { task: helper, agent_type: 'mock' })
  }
  await start('Reconcile the ledger nightly', projects['Payments service'])
  await start('Write the migration guide', projects['Docs site'])
  fs.writeFileSync(marker, new Date().toISOString())
}

async function runningAgents() {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/agents`)
    const body = await response.json()
    const agents = Array.isArray(body) ? body : body?.agents
    return Array.isArray(agents) ? agents.filter((agent) => agent.status === 'running').length : 0
  } catch {
    return 0
  }
}

function stopBackend() {
  return new Promise((resolve) => {
    if (!backend) { resolve(); return }
    const child = backend
    const timer = setTimeout(() => { try { child.kill() } catch { /* Already gone. */ } resolve() }, 20000)
    child.once('exit', () => { clearTimeout(timer); resolve() })
    try { child.stdin.write('shutdown\n'); child.stdin.end() } catch { child.kill() }
  })
}

function icon() {
  return nativeImage.createFromPath(app.isPackaged ? path.join(process.resourcesPath, 'icon.png') : path.join(__dirname, 'assets', 'icon.png'))
}

function showWindow() {
  if (!window) return
  if (window.isMinimized()) window.restore()
  window.show()
  window.focus()
}

function createTray() {
  if (tray) return
  tray = new Tray(icon().resize({ width: 16, height: 16 }))
  tray.setToolTip(`${PRODUCT} is running`)
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Control Center', click: showWindow },
    { type: 'separator' },
    { label: 'Quit and stop agents', click: () => { quitting = true; app.quit() } },
  ]))
  tray.on('click', showWindow)
}

function createWindow() {
  window = new BrowserWindow({
    width: 1440, height: 920, minWidth: 900, minHeight: 600, show: false, title: DEMO ? `${PRODUCT} (demo)` : PRODUCT,
    backgroundColor: '#EAEEF4', icon: icon(), autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false },
  })
  Menu.setApplicationMenu(null)
  window.once('ready-to-show', () => window.show())
  const origin = `http://127.0.0.1:${port}`
  // Only the local interface opens in the window; any other address opens in the default browser.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(origin)) return { action: 'allow' }
    if (/^https?:\/\//.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(origin)) { event.preventDefault(); if (/^https?:\/\//.test(url)) shell.openExternal(url) }
  })
  window.on('close', async (event) => {
    if (quitting) return
    event.preventDefault()
    const working = await runningAgents()
    if (!working) { quitting = true; app.quit(); return }
    const choice = await dialog.showMessageBox(window, {
      type: 'question', title: PRODUCT, noLink: true, defaultId: 0, cancelId: 2,
      message: `${working} ${working === 1 ? 'agent is' : 'agents are'} still working.`,
      detail: 'Keep them working in the background, or quit: quitting stops them safely, records where they were, and they can be resumed later.',
      buttons: ['Keep working in the background', 'Quit and stop agents', 'Cancel'],
    })
    if (choice.response === 0) { createTray(); window.hide() }
    else if (choice.response === 1) { quitting = true; app.quit() }
  })
  window.loadURL(`${origin}/#/work`)
}

ipcMain.handle('control-center:choose-folder', async () => {
  const result = await dialog.showOpenDialog(window, { title: 'Choose a project folder (a Git repository)', properties: ['openDirectory'] })
  return result.canceled ? null : result.filePaths[0] || null
})
ipcMain.handle('control-center:info', () => ({ demo: DEMO, version: app.getVersion(), dataDir: DATA_DIR, logs: LOG_DIR }))
ipcMain.handle('control-center:open-logs', () => shell.openPath(LOG_DIR))

app.on('before-quit', (event) => {
  if (!backend) return
  event.preventDefault()
  quitting = true
  stopBackend().then(() => app.exit(0))
})

app.whenReady().then(async () => {
  app.setAppUserModelId('com.aiagentcontrolcenter.desktop')
  const splash = new BrowserWindow({ width: 420, height: 260, frame: false, resizable: false, show: true, backgroundColor: '#EAEEF4',
    icon: icon(), webPreferences: { sandbox: true } })
  splash.loadFile(path.join(__dirname, 'splash.html'))
  try {
    await startBackend()
    if (DEMO) await seedDemo().catch((error) => backendLog?.write(`--- demo seeding failed: ${error.message}
`))
    createWindow()
    window.once('show', () => splash.destroy())
  } catch (error) {
    splash.destroy()
    dialog.showErrorBox(PRODUCT, `${error.message}\n\nThe log is in ${LOG_DIR}.`)
    quitting = true
    await stopBackend()
    app.exit(1)
  }
})

app.on('window-all-closed', () => { /* The close handler decides; the tray may keep the app running. */ })
