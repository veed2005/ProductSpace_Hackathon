"""Helpers for end-to-end runs: an isolated Formline server, Chromium with the extension loaded, and
pairing through the real extension popup.

Used by scripts/e2e_golden_path.py and scripts/extension_smoke.py. Needs the dev dependency
`playwright` and its Chromium (`uv run playwright install chromium`). Chrome itself no longer loads
unpacked extensions from the command line, so these runs use Playwright's Chromium.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = ROOT / "extension"


class Server:
    def __init__(self, port: int = 8765, env: dict | None = None):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.data_dir = Path(tempfile.mkdtemp(prefix="formline-e2e-"))
        self.log_path = self.data_dir / "server.log"
        self.env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{(self.data_dir / 'formline.db').as_posix()}",
            "DATA_DIR": str(self.data_dir),
            "FORMLINE_DEV_ENDPOINTS": "true",
            "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_PHONE_NUMBER": "",
            "TWILIO_VALIDATE_SIGNATURES": "false",
            "PUBLIC_BASE_URL": self.url,
            "PYTHONIOENCODING": "utf-8",
            **(env or {}),
        }
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "Server":
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/health", timeout=1)
                return self
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited:\n" + self.log_path.read_text(encoding="utf-8"))
                time.sleep(0.2)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")

    def command(self, action: str, **args) -> dict:
        return self.post("/browser/dev/command", {"action": action, **args})

    def tail(self, n: int = 40) -> str:
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])


def launch_chromium(playwright, *, headless: bool = True):
    """Chromium with the Formline extension. Returns (context, extension_id)."""
    profile = tempfile.mkdtemp(prefix="formline-chrome-")
    context = playwright.chromium.launch_persistent_context(
        profile, channel="chromium", headless=headless, viewport={"width": 1280, "height": 860},
        args=[f"--disable-extensions-except={EXTENSION_DIR}", f"--load-extension={EXTENSION_DIR}"])
    worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
    extension_id = worker.url.split("/")[2]
    return context, extension_id


def pair(context, extension_id: str, server_url: str, *, phone: str, name: str, pin: str) -> None:
    """Pair through the popup UI exactly as a person would (dev server shows the code)."""
    popup = context.new_page()
    popup.goto(f"chrome-extension://{extension_id}/popup/popup.html")
    popup.locator("details summary").click()
    popup.fill("#server", server_url)
    popup.locator("#server").dispatch_event("change")
    popup.fill("#phone", phone)
    popup.click("#send-code")
    popup.wait_for_selector("#dev-code:not([hidden])")
    code = popup.inner_text("#dev-code").strip()[-6:]
    popup.fill("#code", code)
    popup.click("#confirm")
    popup.wait_for_selector("#profile-fields:not([hidden])")
    popup.fill("#pname", name)
    popup.fill("#pin", pin)
    popup.click("#confirm")
    popup.wait_for_selector("#paired:not([hidden])")
    popup.wait_for_selector(".status.connected", timeout=15000)
    popup.close()
