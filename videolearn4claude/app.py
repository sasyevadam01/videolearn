"""VideoLearn 4Claude: a tiny local web server + a clean app-style browser window.

Run `python -m videolearn4claude.app` (or use the "VideoLearn 4Claude" desktop shortcut).
Everything stays on this PC: the server only listens on 127.0.0.1 and closes
itself a couple of minutes after the window is closed.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import HOME, ensure_ffmpeg, gpu_possible, process, read_glossary

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui" / "index.html"
GLOSSARY = HOME / "glossary.txt"
HISTORY = HOME / "history.json"
PORTS = range(8765, 8786)
IDLE_SECONDS = 120

JOB: dict = {"running": False, "step": "", "frac": 0.0, "error": None, "out": None, "id": None,
             "video": None, "duration": None, "model": None, "gpu": False}
REG: dict[str, dict] = {}           # report id -> {"out": Path, "video": Path}
BEAT = [time.time() + 60]           # last time the window said "I'm here" (first visit: grace period)
PICK_LOCK = threading.Lock()


# ---------- helpers ----------
def _rid(out: Path) -> str:
    return hashlib.sha1(str(out.resolve()).encode()).hexdigest()[:10]


def register(out: Path) -> str:
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    rid = _rid(out)
    REG[rid] = {"out": out.resolve(), "video": Path(rep.get("video_path", ""))}
    return rid


def load_history() -> list[dict]:
    try:
        items = json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in items if (Path(i["out"]) / "report.json").exists()]


def add_history(out: Path, video: Path) -> None:
    items = [i for i in load_history() if Path(i["out"]) != out]
    items.insert(0, {"out": str(out), "video": video.name, "when": datetime.now().isoformat(timespec="minutes")})
    HOME.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(items[:30], ensure_ascii=False, indent=1), encoding="utf-8")


def video_duration(video: Path) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(video)], capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def pick_file() -> str:
    import tkinter as tk
    from tkinter import filedialog
    with PICK_LOCK:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askopenfilename(
                title="Choose a video",
                filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv"), ("All files", "*.*")])
        finally:
            root.destroy()


def run_job(video: Path, model: str, language: str) -> None:
    def prog(frac: float, step: str) -> None:
        JOB.update(frac=frac, step=step)

    try:
        prompt = read_glossary(GLOSSARY.read_text(encoding="utf-8")) if GLOSSARY.exists() else None
        report = process(video, None, model=model, language=None if language == "auto" else language,
                         prompt=prompt, progress=prog, log=lambda m: None)
        out = report.parent
        rid = register(out)
        add_history(out, video)
        JOB.update(running=False, step="done", frac=1.0, out=str(out), id=rid)
    except Exception as e:  # shown to the user, details go to the log file
        HOME.mkdir(parents=True, exist_ok=True)
        with open(HOME / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now()} {video}\n{traceback.format_exc()}")
        JOB.update(running=False, step="error", error=str(e) or e.__class__.__name__)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "videolearn4claude"

    def log_message(self, *a):  # silence
        pass

    # responses
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, path: Path) -> None:
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            a, b = m.groups()
            if a == "":
                start = max(0, size - int(b))
            else:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except ConnectionError:
            pass  # the player closed the connection (seeking): normal

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _report(self, rid: str | None) -> dict | None:
        return REG.get(rid or "")

    # GET
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, b"{}")
        u = urlparse(self.path)
        p, q = unquote(u.path), parse_qs(u.query)
        if p == "/":
            return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        if p in ("/icon.png", "/favicon.ico"):
            return self._file(UI.with_name("icon.png"))
        if p == "/api/ping":
            return self._json({"app": "videolearn4claude"})
        if p == "/api/state":
            BEAT[0] = time.time()
            return self._json({"job": JOB, "history": load_history()})
        if p == "/api/glossary":
            return self._json({"text": GLOSSARY.read_text(encoding="utf-8") if GLOSSARY.exists() else ""})
        if p == "/api/report":
            r = self._report(q.get("id", [None])[0])
            if not r:
                return self._json({"error": "Report not found.", "code": "report_missing"}, 404)
            data = json.loads((r["out"] / "report.json").read_text(encoding="utf-8"))
            edits = r["out"] / "edits.json"
            data["edits"] = json.loads(edits.read_text(encoding="utf-8")) if edits.exists() else {}
            data["report_path"] = str(r["out"] / "REPORT.md")
            data["has_video"] = r["video"].is_file()
            return self._json(data)
        m = re.match(r"/r/([0-9a-f]{10})/(.+)", p)
        if m:
            r = self._report(m.group(1))
            if not r:
                return self._send(404, b"")
            if m.group(2) == "video":
                target = r["video"]
            else:
                target = (r["out"] / m.group(2)).resolve()
                if not target.is_relative_to(r["out"]):
                    return self._send(403, b"")
            if not target.is_file():
                return self._send(404, b"")
            return self._file(target)
        self._send(404, b"")

    # POST (custom header: a web page from elsewhere cannot send it cross-origin)
    def do_POST(self) -> None:
        if not self._host_ok() or self.headers.get("X-S2C") != "1":
            return self._send(403, b"{}")
        p, b = urlparse(self.path).path, self._body()
        if p == "/api/pick":
            return self._json({"path": pick_file()})
        if p == "/api/start":
            if JOB["running"]:
                return self._json({"error": "A video is already being processed.", "code": "busy"}, 409)
            video = Path(str(b.get("path", "")).strip().strip('"')).expanduser()
            if not video.is_file():
                return self._json({"error": f"File not found: {video}", "code": "file_not_found"}, 400)
            model = "small" if b.get("model") == "small" else "large-v3-turbo"
            JOB.update(running=True, step="audio", frac=0.0, error=None, out=None, id=None,
                       video=video.name, duration=video_duration(video), model=model,
                       gpu=gpu_possible())
            threading.Thread(target=run_job, args=(video, model, str(b.get("language", "auto"))), daemon=True).start()
            return self._json({"ok": True})
        if p == "/api/open":
            out = Path(str(b.get("out", "")))
            if not (out / "report.json").is_file():
                return self._json({"error": "Report not found (folder moved or deleted?)", "code": "report_missing"}, 404)
            return self._json({"id": register(out)})
        if p == "/api/save":
            r = self._report(b.get("id"))
            if not r:
                return self._json({"error": "Report not found.", "code": "report_missing"}, 404)
            with open(r["out"] / "REPORT.md", "w", encoding="utf-8", newline="\n") as f:
                f.write(str(b.get("md", "")))
            (r["out"] / "edits.json").write_text(json.dumps(b.get("edits", {}), ensure_ascii=False), encoding="utf-8")
            return self._json({"ok": True, "at": datetime.now().strftime("%H:%M")})
        if p == "/api/glossary":
            GLOSSARY.write_text(str(b.get("text", "")).strip() + "\n", encoding="utf-8")
            return self._json({"ok": True})
        if p == "/api/reveal":
            r = self._report(b.get("id"))
            if r and hasattr(os, "startfile"):
                os.startfile(r["out"])  # type: ignore[attr-defined]
            return self._json({"ok": True})
        if p == "/api/quit":
            self._json({"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        self._send(404, b"{}")


def _is_ours(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == "videolearn4claude"
    except (OSError, ValueError):
        return False


def _open_window(url: str) -> None:
    """Open as a clean app window (no tabs/address bar) when Edge or Chrome is available."""
    if os.environ.get("VIDEOLEARN_NO_WINDOW"):                     # tests: serve only
        return
    candidates = [shutil.which("msedge"), shutil.which("chrome"),
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    for exe in candidates:
        if exe and Path(exe).is_file():
            subprocess.Popen([exe, f"--app={url}", "--window-size=1180,820"])
            return
    webbrowser.open(url)


def _monitor() -> None:
    while True:
        time.sleep(5)
        if not JOB["running"] and time.time() - BEAT[0] > IDLE_SECONDS:
            os._exit(0)


def _seed_glossary() -> None:
    if not GLOSSARY.exists():
        GLOSSARY.write_text((ROOT / "ui" / "default_glossary.txt").read_text(encoding="utf-8"), encoding="utf-8")


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    _seed_glossary()
    ensure_ffmpeg()
    if sys.stdout is None or sys.stderr is None:           # pythonw: no console
        log = open(HOME / "app.log", "a", encoding="utf-8")
        sys.stdout = sys.stderr = log
    try:                                                    # sharp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # type: ignore[attr-defined]
    except Exception:
        pass
    srv, port = None, None
    for port in PORTS:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            if _is_ours(port):                              # already running: just show its window
                _open_window(f"http://127.0.0.1:{port}/")
                return
    if srv is None:
        raise SystemExit("no free port between 8765 and 8785")
    threading.Thread(target=_monitor, daemon=True).start()
    _open_window(f"http://127.0.0.1:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
