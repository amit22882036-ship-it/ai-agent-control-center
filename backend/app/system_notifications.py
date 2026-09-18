"""In-memory notification preference, dashboard lease, and Windows delivery."""

import base64
import json
import logging
import os
from pathlib import Path
import subprocess
from threading import RLock, Timer
import time

logger = logging.getLogger(__name__)
DASHBOARD_LEASE_SECONDS = 6
DELIVERY_GRACE_SECONDS = 8

# This script is fixed. Titles and bodies are JSON data on stdin, never code.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$icon = New-Object System.Windows.Forms.NotifyIcon
try {
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Text = 'AI Agent Control Center'
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000, [string]$payload.title, [string]$payload.body,
        [System.Windows.Forms.ToolTipIcon]::Info)
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt 12) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
} finally {
    $icon.Visible = $false
    $icon.Dispose()
}
"""


def deliver_windows_notification(title: str, body: str) -> None:
    if os.name != "nt":
        return
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden",
         "-EncodedCommand", base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")],
        input=json.dumps({"title": title, "body": body}, ensure_ascii=True),
        text=True, capture_output=True, shell=False, timeout=20, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class SystemNotifications:
    def __init__(self, deliver=deliver_windows_notification, clock=time.monotonic, timer=Timer):
        self.enabled = False
        self.last_seen = None
        self._pending = {}
        self._lock = RLock()
        self._deliver = deliver
        self._clock = clock
        self._timer = timer

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            if not enabled:
                self.last_seen = None
                for timer in self._pending.values():
                    timer.cancel()
                self._pending.clear()

    def heartbeat(self) -> None:
        with self._lock:
            if self.enabled:
                self.last_seen = self._clock()

    def cancel(self, agent_id: str) -> None:
        with self._lock:
            timer = self._pending.pop(agent_id, None)
            if timer:
                timer.cancel()

    def transition(self, agent_id: str, status: str, task: str) -> None:
        if status not in ("waiting", "finished"):
            return
        with self._lock:
            if not self.enabled:
                return
            self.cancel(agent_id)
            title = "Agent needs your input" if status == "waiting" else "Agent finished"
            body = (task.strip() or f"Agent {agent_id}")[:180]
            # Always allow time for a fresh browser poll and heartbeat. A tab
            # closed just before completion cannot suppress delivery indefinitely.
            timer = self._timer(DELIVERY_GRACE_SECONDS,
                                lambda: self._send_if_needed(agent_id, timer, title, body))
            timer.daemon = True
            self._pending[agent_id] = timer
            timer.start()

    def _send_if_needed(self, agent_id, timer, title, body):
        with self._lock:
            if self._pending.get(agent_id) is not timer:
                return
            del self._pending[agent_id]
            if not self.enabled:
                return
            if self.last_seen is not None and self._clock() - self.last_seen < DASHBOARD_LEASE_SECONDS:
                return
        try:
            self._deliver(title, body)
        except Exception:
            logger.exception("Native notification delivery failed")


notifications = SystemNotifications()
