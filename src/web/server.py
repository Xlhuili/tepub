"""TEPUB Web — minimal FastAPI wrapper.

Single shared password (HTTP Basic). Single worker. No database.
Each job lives under ~/.tepub-web/jobs/<id>/ with metadata.json, log.txt,
the uploaded EPUB, and a work/ directory passed to `tepub --work-dir`.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

JOBS_ROOT = Path.home() / ".tepub-web" / "jobs"
TEMPLATES_DIR = Path(__file__).parent / "templates"
USERNAME = "family"
ENV_PASSWORD = "TEPUB_WEB_PASSWORD"
VALID_TASKS = {"translate", "audiobook", "both"}

security = HTTPBasic()
job_queue: asyncio.Queue[str] = asyncio.Queue()


def _password() -> str:
    pwd = os.environ.get(ENV_PASSWORD)
    if not pwd:
        raise RuntimeError(
            f"{ENV_PASSWORD} env var not set. "
            f"Set it before launching: export {ENV_PASSWORD}='your-shared-password'"
        )
    return pwd


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    user_ok = secrets.compare_digest(credentials.username, USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, _password())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def _read_meta(job_id: str) -> dict[str, Any]:
    return json.loads((_job_dir(job_id) / "metadata.json").read_text())


def _write_meta(job_id: str, **updates: Any) -> None:
    p = _job_dir(job_id) / "metadata.json"
    meta = json.loads(p.read_text()) if p.exists() else {}
    meta.update(updates)
    p.write_text(json.dumps(meta, indent=2))


def _list_jobs() -> list[dict[str, Any]]:
    if not JOBS_ROOT.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for d in sorted(JOBS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = d / "metadata.json"
        if d.is_dir() and meta_path.exists():
            try:
                jobs.append(json.loads(meta_path.read_text()))
            except Exception:
                continue
    return jobs


def _collect_artifacts(work_dir: Path) -> list[dict[str, Any]]:
    """Find produced files we expose for download."""
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for pattern in ("*_bilingual.epub", "*_translated.epub", "*.m4a"):
        for f in work_dir.rglob(pattern):
            if f in seen or not f.is_file():
                continue
            seen.add(f)
            items.append(
                {
                    "name": f.name,
                    "rel": str(f.relative_to(work_dir)),
                    "size": f.stat().st_size,
                    "kind": "file",
                }
            )
    # Web export — look for any index.html inside a web/ folder
    for index in work_dir.rglob("web/index.html"):
        web_dir = index.parent
        items.append(
            {
                "name": "web (folder)",
                "rel": str(web_dir.relative_to(work_dir)),
                "size": sum(f.stat().st_size for f in web_dir.rglob("*") if f.is_file()),
                "kind": "dir",
            }
        )
    return items


async def _run_subprocess(cmd: list[str], log_path: Path) -> None:
    """Run a subprocess, streaming stdout+stderr into log_path."""
    with log_path.open("ab") as logf:
        logf.write(f"\n$ {' '.join(cmd)}\n".encode())
        logf.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=logf,
            stderr=asyncio.subprocess.STDOUT,
        )
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"command exited with status {rc}: {cmd[0]} ...")


async def _run_job(job_id: str) -> None:
    meta = _read_meta(job_id)
    d = _job_dir(job_id)
    epub = d / meta["filename"]
    work = d / "work"
    work.mkdir(exist_ok=True)
    log_path = d / "log.txt"

    _write_meta(job_id, status="running", started_at=datetime.now().isoformat())

    try:
        task = meta["task"]
        tepub_bin = shutil.which("tepub") or "tepub"

        if task in ("translate", "both"):
            cmd = [
                tepub_bin,
                "--work-dir", str(work),
                "pipeline", str(epub),
                "--to", meta.get("target_language") or "Simplified Chinese",
            ]
            await _run_subprocess(cmd, log_path)

        if task in ("audiobook", "both"):
            cmd = [
                tepub_bin,
                "--work-dir", str(work),
                "audiobook", "generate", str(epub),
                "--tts-provider", meta.get("tts_provider") or "edge",
            ]
            voice = meta.get("voice")
            if voice:
                cmd += ["--voice", voice]
            await _run_subprocess(cmd, log_path)

        artifacts = _collect_artifacts(work)
        _write_meta(
            job_id,
            status="done",
            finished_at=datetime.now().isoformat(),
            artifacts=artifacts,
        )
    except Exception as exc:
        with log_path.open("a") as f:
            f.write(f"\n[ERROR] {exc}\n")
        _write_meta(
            job_id,
            status="failed",
            finished_at=datetime.now().isoformat(),
            error=str(exc),
        )


async def _worker_loop() -> None:
    while True:
        job_id = await job_queue.get()
        try:
            await _run_job(job_id)
        except Exception as exc:
            print(f"[tepub web] worker fatal: {exc}", file=sys.stderr)
        finally:
            job_queue.task_done()


def create_app() -> FastAPI:
    app = FastAPI(title="TEPUB Web", docs_url=None, redoc_url=None)

    @app.on_event("startup")
    async def _startup() -> None:
        _password()
        JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        asyncio.create_task(_worker_loop())
        # Re-queue jobs that were queued/running when the server died
        for meta in _list_jobs():
            if meta.get("status") in {"queued", "running"}:
                _write_meta(meta["id"], status="queued")
                await job_queue.put(meta["id"])

    @app.get("/", response_class=HTMLResponse)
    async def index(_: str = Depends(require_auth)) -> str:
        return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/jobs")
    async def api_jobs(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return _list_jobs()

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return _read_meta(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="job not found")

    @app.get("/api/jobs/{job_id}/log")
    async def api_log(job_id: str, _: str = Depends(require_auth)) -> dict[str, str]:
        log_path = _job_dir(job_id) / "log.txt"
        if not log_path.exists():
            return {"log": ""}
        size = log_path.stat().st_size
        max_bytes = 200_000
        with log_path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop partial line
            return {"log": f.read().decode("utf-8", errors="replace")}

    @app.get("/jobs/{job_id}/download/{rel:path}")
    async def download(
        job_id: str, rel: str, _: str = Depends(require_auth)
    ) -> FileResponse:
        work = (_job_dir(job_id) / "work").resolve()
        target = (work / rel).resolve()
        try:
            target.relative_to(work)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad path")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(target, filename=target.name)

    @app.post("/api/upload")
    async def upload(
        file: UploadFile = File(...),
        task: str = Form(...),
        target_language: str = Form("Simplified Chinese"),
        tts_provider: str = Form("edge"),
        voice: str = Form(""),
        _: str = Depends(require_auth),
    ) -> dict[str, str]:
        if not file.filename or not file.filename.lower().endswith(".epub"):
            raise HTTPException(status_code=400, detail="only .epub files accepted")
        if task not in VALID_TASKS:
            raise HTTPException(status_code=400, detail=f"task must be one of {VALID_TASKS}")
        if tts_provider not in {"edge", "openai"}:
            raise HTTPException(status_code=400, detail="tts_provider must be edge or openai")

        job_id = uuid.uuid4().hex[:12]
        d = _job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        epub_path = d / file.filename
        with epub_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)

        _write_meta(
            job_id,
            id=job_id,
            filename=file.filename,
            task=task,
            target_language=target_language,
            tts_provider=tts_provider,
            voice=voice or None,
            status="queued",
            created_at=datetime.now().isoformat(),
        )
        await job_queue.put(job_id)
        return {"job_id": job_id}

    @app.post("/api/jobs/{job_id}/delete")
    async def delete_job(job_id: str, _: str = Depends(require_auth)) -> dict[str, str]:
        meta = _read_meta(job_id)
        if meta.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="job is active")
        shutil.rmtree(_job_dir(job_id), ignore_errors=True)
        return {"ok": "deleted"}

    return app
