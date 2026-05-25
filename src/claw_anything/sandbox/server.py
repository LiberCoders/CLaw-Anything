"""Sandbox HTTP server — runs inside agent container.

Exposes file, shell, and browser operations over HTTP so that the host-side
dispatcher can drive the container without SSH or docker exec.
"""

from __future__ import annotations

import base64
import glob as _glob
import logging
import mimetypes
import subprocess
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger("sandbox")

app = FastAPI(title="claw-anything sandbox")

WORKSPACE_ROOT = Path("/workspace")

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ExecRequest(BaseModel):
    command: str
    timeout_seconds: int = 30


class FileReadRequest(BaseModel):
    path: str


class FileWriteRequest(BaseModel):
    path: str
    content: str


class FileWriteB64Request(BaseModel):
    path: str
    content_b64: str


class ScreenshotRequest(BaseModel):
    url: str


class GlobRequest(BaseModel):
    pattern: str
    max_files: int = 50


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/exec")
def exec_command(req: ExecRequest):
    try:
        proc = subprocess.run(
            req.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Timed out after {req.timeout_seconds}s",
        }


_TEXT_MIMES = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
}
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".js", ".ts", ".py", ".sh", ".bash",
    ".cfg", ".ini", ".toml", ".log", ".sql", ".r", ".rmd",
}


@app.post("/read")
def read_file(req: FileReadRequest):
    p = Path(req.path)
    if not p.exists():
        return {"error": f"File not found: {p}"}
    try:
        p.resolve().relative_to(WORKSPACE_ROOT)
    except ValueError:
        logger.warning("read outside workspace: %s", p)
    mime, _ = mimetypes.guess_type(str(p))
    ext = p.suffix.lower()
    # Known text mime OR known text extension → text; otherwise binary.
    # mime=None with unknown extension defaults to binary (safer).
    is_text = (
        mime in _TEXT_MIMES
        or (mime is not None and mime.startswith("text/"))
        or (mime is None and ext in _TEXT_EXTENSIONS)
    )
    if is_text:
        return {
            "content": p.read_text(encoding="utf-8", errors="replace"),
            "mime_type": mime or "text/plain",
            "encoding": "utf-8",
        }
    else:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return {
            "content": data,
            "mime_type": mime or "application/octet-stream",
            "encoding": "base64",
            "size_bytes": p.stat().st_size,
        }


@app.post("/write")
def write_file(req: FileWriteRequest):
    p = Path(req.path)
    try:
        p.resolve().relative_to(WORKSPACE_ROOT)
    except ValueError:
        logger.warning("write outside workspace: %s", p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content, encoding="utf-8")
    return {"written": str(p), "bytes": len(req.content)}


@app.post("/write_b64")
def write_file_b64(req: FileWriteB64Request):
    """Write a binary file from base64-encoded content."""
    p = Path(req.path)
    try:
        p.resolve().relative_to(WORKSPACE_ROOT)
    except ValueError:
        logger.warning("write_b64 outside workspace: %s", p)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(req.content_b64)
    p.write_bytes(raw)
    return {"written": str(p), "bytes": len(raw)}


@app.post("/glob")
def glob_files(req: GlobRequest):
    """List files matching a glob pattern (supports env snapshot collection)."""
    matches = sorted(_glob.glob(req.pattern, recursive=True))
    results = []
    for m in matches[: req.max_files]:
        p = Path(m)
        if p.is_file():
            mime, _ = mimetypes.guess_type(str(p))
            results.append({
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "mime_type": mime or "unknown",
            })
    return {"files": results}


@app.post("/screenshot")
def screenshot(req: ScreenshotRequest):
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError:
        return {
            "error": (
                "playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium"
            ),
            "url": req.url,
        }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(req.url, wait_until="networkidle", timeout=30_000)
            title = page.title()
            text = page.inner_text("body")[:2000]
            browser.close()
        return {"url": req.url, "title": title, "body_text": text}
    except Exception as exc:
        return {"error": str(exc), "url": req.url}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sandbox HTTP server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    cli_args = parser.parse_args()
    uvicorn.run(app, host=cli_args.host, port=cli_args.port)
